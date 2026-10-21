from datetime import date
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.ingestion.models import DocType, Domain, EcfrFetchSpec, SourceSpec

FIXTURES = Path(__file__).parent / "fixtures"


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


@pytest.fixture(scope="session")
def real_settings():
    settings = get_settings()
    if not settings.sources_file.is_file():
        pytest.skip("data/sources.yaml not available")
    return settings
