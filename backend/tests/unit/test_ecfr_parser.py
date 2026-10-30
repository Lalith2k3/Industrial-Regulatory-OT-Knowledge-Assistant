import pytest

from app.ingestion.domain.errors import ParseError
from app.ingestion.domain.models import SectionKind
from app.ingestion.parsing.ecfr import EcfrXmlParser


@pytest.fixture
def parsed(ecfr_parser, mini_source, fixtures_dir):
    return ecfr_parser.parse(mini_source, fixtures_dir / "mini_part.xml")


def by_key(parsed, key):
    return next(s for s in parsed.sections if s.key == key)


def test_definitions_become_one_section_per_term(parsed):
    defs = [s for s in parsed.sections if s.kind == "definition"]
    assert [s.metadata["term"] for s in defs] == ["Abandoned", "Gathering line", "Low-stress pipeline"]
    low_stress = by_key(parsed, "195.2/def/low-stress-pipeline")
    # numbered sub-paragraphs stay with their term
    assert [p.text[:3] for p in low_stress.paragraphs] == ["Low", "(1)", "(2)"]
    assert low_stress.citation == "49 CFR 195.2"
    assert low_stress.path[-1] == "Low-stress pipeline"


def test_section_paths_include_subpart_and_subject_group(parsed):
    section = by_key(parsed, "195.452")
    assert section.path == [
        "Subpart F—Operation and Maintenance",
        "Pipeline Integrity Management",
        "§ 195.452 Pipeline integrity management in high consequence areas.",
    ]
    assert section.source_url == "https://www.ecfr.gov/on/2026-10-05/title-49/section-195.452"


def test_top_level_markers_survive_nesting_and_roman_numerals(parsed):
    section = by_key(parsed, "195.452")
    markers = [(p.text[:4], p.top_marker, p.starts_unit) for p in section.paragraphs]
    assert markers == [
        ("(a) ", "a", True),
        ("(1) ", "a", False),
        ("(2) ", "a", False),
        ("(b) ", "b", True),
        ("(i) ", "b", False),  # roman numeral after a list intro, not paragraph (i)
        ("(ii)", "b", False),
        ("(c) ", "c", True),
    ]


def test_reserved_paragraphs_and_sections_are_dropped(parsed):
    section = by_key(parsed, "195.3")
    assert not any("[Reserved]" in p.text for p in section.paragraphs)
    # (c) is still recognised as top-level after the dropped (b)
    assert [p.top_marker for p in section.paragraphs if p.starts_unit] == ["a", "c"]
    assert "195.4" not in {s.key for s in parsed.sections}
    assert parsed.metadata["reserved_sections"] == ["195.4", "Appendix C"]


def test_tables_are_serialised_row_by_row(parsed):
    table = next(p for p in by_key(parsed, "195.3").paragraphs if p.is_table)
    assert table.text.splitlines() == [
        "Source | Referenced in",
        "API Spec 5L | §§ 195.106; 195.112.",
        "API Std 1104 | §§ 195.214(a); 195.222.",
    ]


def test_amendment_links_are_not_content_but_history_is_metadata(parsed):
    section = by_key(parsed, "195.3")
    assert not any("Link to an amendment" in p.text for p in section.paragraphs)
    assert section.metadata["amendments"] == ["Amdt. 195-99, 80 FR 168, Jan. 5, 2015"]


def test_appendix_split_at_headings(parsed):
    parts = [s for s in parsed.sections if s.kind == "appendix"]
    assert [s.heading for s in parts] == [
        "Appendix A to Part 195—Delineation Between Federal and State Jurisdiction",
        "Examples",
    ]
    assert all(s.citation == "49 CFR Part 195, Appendix A" for s in parts)


def test_expected_keys_cover_sections_and_appendices(parsed):
    assert parsed.expected_keys == ["195.2", "195.3", "195.452", "Appendix A"]
    units = {s.metadata["unit"] for s in parsed.sections}
    assert set(parsed.expected_keys) <= units


def test_sections_carry_structural_references_for_the_chunker(parsed):
    section = by_key(parsed, "195.452")
    assert section.own_cfr_ref == "195.452" and section.paragraph_citations
    appendix = next(s for s in parsed.sections if s.kind == SectionKind.APPENDIX)
    assert appendix.own_cfr_ref is None and not appendix.paragraph_citations


def test_citations_and_links_follow_the_cfr_title_and_site(mini_source, fixtures_dir):
    other_title = mini_source.model_copy(update={"fetch": mini_source.fetch.model_copy(update={"title": 40})})
    parsed = EcfrXmlParser(site_url="https://mirror.example").parse(other_title, fixtures_dir / "mini_part.xml")
    section = by_key(parsed, "195.452")
    assert section.citation == "40 CFR 195.452"
    assert section.source_url == "https://mirror.example/on/2026-10-05/title-40/section-195.452"


def test_wrong_part_is_rejected(ecfr_parser, mini_source, fixtures_dir):
    wrong = mini_source.model_copy(update={"fetch": mini_source.fetch.model_copy(update={"part": 192})})
    with pytest.raises(ParseError, match="registry expects 192"):
        ecfr_parser.parse(wrong, fixtures_dir / "mini_part.xml")


def test_unknown_option_is_rejected_before_parsing(ecfr_parser, mini_source):
    with pytest.raises(ParseError, match="invalid options"):
        ecfr_parser.validate_options(mini_source.model_copy(update={"options": {"cfr_titel": 49}}))
