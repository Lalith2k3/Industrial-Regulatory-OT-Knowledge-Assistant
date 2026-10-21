"""End-to-end ingestion against a real PostgreSQL + pgvector (marker: db).

Set RAG_TEST_DATABASE_URL to a server you can create databases on, e.g.
postgresql+psycopg://rag:rag@localhost:5433/rag. A throwaway database is created and dropped.
"""

import os
import shutil
import uuid

import pytest
import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.ingestion.embeddings import HashEmbedder
from app.ingestion.pipeline import RunOptions, run_ingestion
from app.ingestion.store import KnowledgeStore

pytestmark = pytest.mark.db
DIM = 64


@pytest.fixture
def database_url():
    base = os.environ.get("RAG_TEST_DATABASE_URL")
    if not base:
        pytest.skip("RAG_TEST_DATABASE_URL not set")
    url = make_url(base)
    name = f"rag_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def workspace(tmp_path, fixtures_dir, database_url):
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(fixtures_dir / "mini_part.xml", data / "mini_part.xml")
    entry = {
        "id": "test-195",
        "title": "Transportation of Hazardous Liquids by Pipeline",
        "short_title": "49 CFR Part 195",
        "publisher": "PHMSA",
        "domain": "pipeline_safety",
        "doc_type": "regulation",
        "parser": "ecfr_xml",
        "path": "mini_part.xml",
        "version": "test",
        "fetch": {"title": 49, "part": 195, "date": "2026-10-05"},
    }
    sources = data / "sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": [entry]}), encoding="utf-8")
    settings = Settings(
        data_dir=data,
        sources_file=sources,
        processed_dir=tmp_path / "processed",
        database_url=database_url,
        embedding_model="local-hash",
        embedding_dim=DIM,
    )
    store = KnowledgeStore(database_url, DIM)
    yield settings, store, entry, sources
    store.engine.dispose()


class CountingEmbedder(HashEmbedder):
    def __init__(self, dim):
        super().__init__(dim)
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += len(texts)
        return super().embed_documents(texts)


def test_ingest_is_idempotent_reuses_embeddings_and_supports_hybrid_search(workspace):
    settings, store, entry, sources = workspace
    embedder = CountingEmbedder(DIM)

    first = run_ingestion(settings, RunOptions(), store=store, embedder=embedder)
    assert [r.status for r in first] == ["ingested"]
    chunk_count = first[0].chunks
    assert embedder.calls == chunk_count

    second = run_ingestion(settings, RunOptions(), store=store, embedder=embedder)
    assert [r.status for r in second] == ["unchanged"]
    assert embedder.calls == chunk_count

    # Edit one section: only its chunk is re-embedded.
    xml = settings.data_dir / "mini_part.xml"
    xml.write_text(xml.read_text(encoding="utf-8").replace("May 29, 2001.", "May 29, 2001, or later."), encoding="utf-8")
    third = run_ingestion(settings, RunOptions(), store=store, embedder=embedder)
    assert third[0].status == "ingested" and third[0].embedded == 1 and third[0].reused == chunk_count - 1

    with store.engine.connect() as conn:
        lexical = conn.execute(
            text("SELECT citation FROM chunks WHERE tsv @@ websearch_to_tsquery('english', :q)"),
            {"q": "195.452"},
        ).scalars().all()
        assert "49 CFR 195.452" in lexical
        query_vec = embedder.embed_documents(["Abandoned means permanently removed from service."])[0]
        nearest = conn.execute(
            text("SELECT section_key FROM chunks ORDER BY embedding <=> CAST(:v AS vector) LIMIT 1"),
            {"v": str(query_vec)},
        ).scalar_one()
        assert nearest == "195.2/def/abandoned"
        domains = conn.execute(text("SELECT DISTINCT domain FROM chunks")).scalars().all()
        assert domains == ["pipeline_safety"]
        runs = conn.execute(text("SELECT status FROM ingestion_runs ORDER BY started_at")).scalars().all()
        assert runs == ["succeeded", "succeeded", "succeeded"]

    # Disabling the source in the registry removes it from the knowledge base.
    sources.write_text(yaml.safe_dump({"sources": [{**entry, "enabled": False}]}), encoding="utf-8")
    pruned = run_ingestion(settings, RunOptions(), store=store, embedder=embedder)
    assert [(r.source_id, r.status) for r in pruned] == [("test-195", "pruned")]
    with store.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM chunks")).scalar_one() == 0


def test_dimension_mismatch_is_a_controlled_error(workspace):
    settings, store, _, _ = workspace
    store.init_schema()
    other = KnowledgeStore(settings.database_url, DIM * 2, engine=store.engine)
    with pytest.raises(Exception, match="requires dropping and rebuilding"):
        other.init_schema()
