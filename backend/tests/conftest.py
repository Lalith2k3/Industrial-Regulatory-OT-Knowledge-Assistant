from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings, get_settings
from app.ingestion import bootstrap
from app.ingestion.chunking import Chunker, ChunkPolicy
from app.ingestion.domain.models import DocType, Domain, EcfrFetchSpec, SectionKind, SourceSpec
from app.ingestion.parsing.ecfr import EcfrXmlParser
from app.query.domain.models import KnowledgeChunk

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def default_settings() -> Settings:
    """Code defaults only: ignores .env so unit tests do not depend on the developer's machine."""
    return Settings(_env_file=None)


@pytest.fixture(scope="session")
def references(default_settings: Settings) -> bootstrap.References:
    return bootstrap.build_references(default_settings)


@pytest.fixture(scope="session")
def ecfr_parser(default_settings: Settings) -> EcfrXmlParser:
    return EcfrXmlParser(site_url=default_settings.ecfr_base_url)


@pytest.fixture
def make_chunker(references: bootstrap.References) -> Callable[[ChunkPolicy], Chunker]:
    def make(policy: ChunkPolicy) -> Chunker:
        return Chunker({DocType.REGULATION: policy}, cfr_refs=references.cfr, control_refs=references.controls)

    return make


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def mini_source() -> SourceSpec:
    return SourceSpec(
        id="test-195",
        title="Transportation of Hazardous Liquids by Pipeline",
        short_title="49 CFR Part 195",
        publisher="PHMSA",
        domain=Domain.PIPELINE_SAFETY,
        doc_type=DocType.REGULATION,
        parser="ecfr_xml",
        path=Path("mini_part.xml"),
        version="test",
        fetch=EcfrFetchSpec(title=49, part=195, date=date(2026, 10, 5)),
    )


@pytest.fixture
def make_chunk() -> Callable[..., KnowledgeChunk]:
    """A KnowledgeChunk as the query side reads it, with overridable fields."""

    def make(chunk_id: str, **overrides: Any) -> KnowledgeChunk:
        fields: dict[str, Any] = {
            "id": chunk_id, "document_id": "ecfr-49-cfr-195", "document_title": "Transportation of Hazardous Liquids",
            "document_short_title": "49 CFR Part 195", "document_version": "eCFR 2026-10-05", "publisher": "PHMSA",
            "domain": Domain.PIPELINE_SAFETY, "doc_type": DocType.REGULATION, "kind": SectionKind.SECTION,
            "section_key": chunk_id, "citation": f"49 CFR {chunk_id}", "heading": f"§ {chunk_id}",
            "section_path": ["Subpart A", f"§ {chunk_id}"], "text": f"Text of {chunk_id}.", "token_count": 100,
            "source_url": f"https://www.ecfr.gov/section-{chunk_id}",
        }  # fmt: skip
        return KnowledgeChunk(**{**fields, **overrides})

    return make


@pytest.fixture(scope="session")
def real_settings() -> Settings:
    settings = get_settings()
    if not settings.sources_file.is_file():
        pytest.skip("data/sources.yaml not available")
    return settings
