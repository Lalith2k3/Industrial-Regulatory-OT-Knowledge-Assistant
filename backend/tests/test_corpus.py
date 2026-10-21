"""Regression tests against the real approved documents (marker: corpus; NIST takes ~25 s to parse)."""

import json
import re
from functools import cache

import pytest

from app.core.config import get_settings
from app.ingestion import quality
from app.ingestion.chunking import POLICIES, chunk_document
from app.ingestion.parsers import get_parser
from app.ingestion.registry import load_registry

pytestmark = pytest.mark.corpus


@cache
def _load(source_id: str):
    settings = get_settings()
    spec = next(s for s in load_registry(settings.sources_file) if s.id == source_id)
    path = settings.data_dir / spec.path
    if not path.is_file():
        pytest.skip(f"{path} not available")
    parsed = get_parser(spec.parser).parse(spec, path)
    chunks = chunk_document(spec, parsed)
    return spec, parsed, chunks


@pytest.mark.parametrize(
    "source_id",
    ["ecfr-49-cfr-192", "ecfr-49-cfr-195", "phmsa-om-enforcement-guidance-195", "phmsa-oq-faq", "nist-sp-800-82r3"],
)
def test_every_approved_source_passes_quality_gates(source_id):
    spec, parsed, chunks = _load(source_id)
    report = quality.check(spec, parsed, chunks, POLICIES[spec.doc_type])
    assert report.ok, report.errors
    assert report.stats.get("coverage", 1.0) >= 0.96


def test_evaluation_set_citations_exist_in_current_chunks(real_settings):
    """Parser changes must not silently invalidate the gold set in data/evaluation/questions.json."""
    path = real_settings.data_dir / "evaluation" / "questions.json"
    questions = json.loads(path.read_text(encoding="utf-8"))["questions"]
    missing = []
    for q in questions:
        for src in q["expected_sources"]:
            _, _, chunks = _load(src["document_id"])
            if not any(c.section_key == src["section_key"] and c.citation == src["citation"] for c in chunks):
                missing.append((q["id"], src["section_key"], src["citation"]))
    assert not missing


def test_registry_marks_2015_cfr_pdfs_as_superseded(real_settings):
    specs = {s.id: s for s in load_registry(real_settings.sources_file)}
    assert specs["phmsa-cfr-195-2015-pdf"].superseded_by == "ecfr-49-cfr-195"
    assert not specs["phmsa-cfr-195-2015-pdf"].enabled


def test_part_195_sections_and_definitions():
    _, parsed, chunks = _load("ecfr-49-cfr-195")
    keys = {s.key for s in parsed.sections}
    assert {"195.304", "195.402", "195.452", "195.2/def/abandoned", "195.450/def/high-consequence-area"} <= keys
    test_pressure = next(c for c in chunks if c.section_key == "195.304")
    assert "125 percent" in test_pressure.text
    assert test_pressure.source_url.endswith("/title-49/section-195.304")
    long_parts = [c for c in chunks if c.section_key == "195.452"]
    assert len(long_parts) > 5 and all(re.fullmatch(r"49 CFR 195\.452\([a-z]\)(–\([a-z]\))?", c.citation) for c in long_parts)


def test_oq_faq_has_all_questions_in_order_with_cited_sections():
    _, parsed, chunks = _load("phmsa-oq-faq")
    faqs = [s for s in parsed.sections if s.kind == "faq"]
    assert [s.metadata["faq_number"] for s in faqs] == list(range(1, 41))
    q2 = next(s for s in faqs if s.metadata["faq_number"] == 2)
    assert q2.metadata["topic"] == "Contractor Qualification"
    assert q2.metadata["cited_sections"] == ["192.805", "195.505"]
    assert all(c.page_start for c in chunks)


def test_enforcement_guidance_skips_stale_code_text_and_links_to_regulation():
    _, parsed, chunks = _load("phmsa-om-enforcement-guidance-195")
    assert parsed.metadata["records"] == 27
    fields = {s.metadata.get("field") for s in parsed.sections}
    assert "existing_code" not in fields and "guidance" in fields
    interp = next(s for s in parsed.sections if s.metadata.get("interpretation_id") == "PI-83-0101")
    assert interp.metadata["code_section"] == "195.402(a)"
    assert all("195.402" in c.cfr_refs for c in chunks if c.metadata.get("code_section", "").startswith("195.402"))


def test_nist_structure_controls_glossary_and_acronyms():
    _, parsed, chunks = _load("nist-sp-800-82r3")
    assert parsed.dictionaries["acronyms"]["PHMSA"] == "Pipeline and Hazardous Materials Safety Administration"
    controls = {s.metadata["control_id"] for s in parsed.sections if s.kind == "control"}
    assert {"AC-17", "AU-2", "SC-7", "SI-4"} <= controls and len(controls) > 200
    assert any(s.key == "glossary/control-loop" for s in parsed.sections)
    # excluded sections never become chunks, and running headers are stripped
    assert not any(c.section_path[0].startswith(("References", "Appendix A", "Appendix G")) for c in chunks)
    assert not any("Guide to Operational Technology (OT) Security September 2023" in c.text for c in chunks)
    scada = next(c for c in chunks if c.section_key == "2.3.2")
    assert (scada.page_start, scada.page_label_start) == (29, "12")  # PDF page vs printed page
