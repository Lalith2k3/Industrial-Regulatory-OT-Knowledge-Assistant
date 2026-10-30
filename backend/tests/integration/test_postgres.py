"""End-to-end ingestion against a real PostgreSQL + pgvector (marker: db).

Set RAG_TEST_DATABASE_URL to a server you can create databases on, e.g.
postgresql+psycopg://rag:rag@localhost:5433/rag. A throwaway database is created and dropped.
"""

import shutil

import pytest
import yaml
from sqlalchemy import text

from app.ingestion import bootstrap
from app.ingestion.domain.errors import StorageError
from app.ingestion.domain.models import DocumentStatus
from app.ingestion.embedding import HashEmbedder
from app.ingestion.pipeline import IngestionPipeline, KnowledgeStore, RunOptions
from app.ingestion.quality import QualityGate
from app.ingestion.sources.registry import load_registry
from app.ingestion.storage import FileArtifactSink, PostgresDocumentRepository, PostgresRunLog, build_schema

pytestmark = pytest.mark.db
DIM = 64


class CountingEmbedder(HashEmbedder):
    def __init__(self, dim):
        super().__init__(dim)
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += len(texts)
        return super().embed_documents(texts)


@pytest.fixture
def workspace(tmp_path, fixtures_dir, engine, default_settings, references):
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

    schema = build_schema(DIM)
    embedder = CountingEmbedder(DIM)
    pipeline = IngestionPipeline(
        data_dir=data,
        parsers=bootstrap.build_parsers(default_settings, references),
        chunker=bootstrap.build_chunker(references),
        quality_gate=QualityGate(),
        artifacts=FileArtifactSink(tmp_path / "processed"),
        store=KnowledgeStore(PostgresDocumentRepository(engine, schema), PostgresRunLog(engine, schema), embedder),
    )

    def run():
        return pipeline.run(load_registry(sources), RunOptions())

    return run, embedder, entry, sources, data


def test_ingest_is_idempotent_reuses_embeddings_and_supports_hybrid_search(workspace, engine):
    run, embedder, entry, sources, data = workspace

    first = run()
    assert [r.status for r in first] == [DocumentStatus.INGESTED]
    chunk_count = first[0].chunks
    assert embedder.calls == chunk_count

    assert [r.status for r in run()] == [DocumentStatus.UNCHANGED]
    assert embedder.calls == chunk_count

    # Edit one section: only its chunk is re-embedded.
    xml = data / "mini_part.xml"
    xml.write_text(
        xml.read_text(encoding="utf-8").replace("May 29, 2001.", "May 29, 2001, or later."), encoding="utf-8"
    )
    third = run()
    assert third[0].status == DocumentStatus.INGESTED and third[0].embedded == 1
    assert third[0].reused == chunk_count - 1

    with engine.connect() as conn:
        lexical = (
            conn.execute(
                text("SELECT citation FROM chunks WHERE tsv @@ websearch_to_tsquery('english', :q)"), {"q": "195.452"}
            )
            .scalars()
            .all()
        )
        assert "49 CFR 195.452" in lexical
        query_vec = embedder.embed_documents(["Abandoned means permanently removed from service."])[0]
        nearest = conn.execute(
            text("SELECT section_key FROM chunks ORDER BY embedding <=> CAST(:v AS vector) LIMIT 1"),
            {"v": str(query_vec)},
        ).scalar_one()
        assert nearest == "195.2/def/abandoned"
        assert conn.execute(text("SELECT DISTINCT domain FROM chunks")).scalars().all() == ["pipeline_safety"]
        runs = conn.execute(text("SELECT status FROM ingestion_runs ORDER BY started_at")).scalars().all()
        assert runs == ["succeeded", "succeeded", "succeeded"]

    # Disabling the source in the registry removes it from the knowledge base.
    sources.write_text(yaml.safe_dump({"sources": [{**entry, "enabled": False}]}), encoding="utf-8")
    assert [(r.source_id, r.status) for r in run()] == [("test-195", DocumentStatus.PRUNED)]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM chunks")).scalar_one() == 0


def test_dimension_mismatch_is_a_controlled_error(engine):
    PostgresDocumentRepository(engine, build_schema(DIM)).ensure_schema()
    with pytest.raises(StorageError, match="requires dropping and rebuilding"):
        PostgresDocumentRepository(engine, build_schema(DIM * 2)).ensure_schema()
