"""PostgreSQL + pgvector persistence for the knowledge base.

Retrieval needs both halves of hybrid search, so each chunk stores an embedding (HNSW, cosine)
and a generated `tsv` column (GIN) for exact identifiers such as "195.452", "PI-74-0122", "AC-17".
Filter columns (domain, publisher, doc_type) are denormalised onto chunks for FR-07.
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Column,
    Computed,
    Date,
    DateTime,
    Engine,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    delete,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, insert

from app.ingestion.errors import IngestionError
from app.ingestion.models import Chunk, SourceSpec

_TSV_EXPR = "to_tsvector('english', coalesce(citation, '') || ' ' || coalesce(heading, '') || ' ' || text)"
_BATCH = 500


class KnowledgeStore:
    def __init__(self, database_url: str, embedding_dim: int, engine: Engine | None = None):
        self.engine = engine or create_engine(database_url, pool_pre_ping=True)
        self.dim = embedding_dim
        self.metadata = MetaData()
        self.documents = Table(
            "documents",
            self.metadata,
            Column("id", Text, primary_key=True),
            Column("title", Text, nullable=False),
            Column("short_title", Text, nullable=False),
            Column("publisher", Text, nullable=False),
            Column("domain", Text, nullable=False),
            Column("doc_type", Text, nullable=False),
            Column("version", Text, nullable=False),
            Column("effective_date", Date),
            Column("source_url", Text),
            Column("local_path", Text, nullable=False),
            Column("content_sha256", Text, nullable=False),
            Column("parser", Text, nullable=False),
            Column("parser_version", Text, nullable=False),
            Column("chunker_version", Text, nullable=False),
            Column("embedding_model", Text, nullable=False),
            Column("chunk_count", Integer, nullable=False),
            Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
            Column("ingested_at", DateTime(timezone=True), nullable=False),
        )
        self.chunks = Table(
            "chunks",
            self.metadata,
            Column("id", Text, primary_key=True),
            Column("document_id", Text, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
            Column("ordinal", Integer, nullable=False),
            Column("kind", Text, nullable=False),
            Column("domain", Text, nullable=False),
            Column("publisher", Text, nullable=False),
            Column("doc_type", Text, nullable=False),
            Column("section_key", Text, nullable=False),
            Column("citation", Text, nullable=False),
            Column("heading", Text, nullable=False),
            Column("section_path", ARRAY(Text), nullable=False),
            Column("text", Text, nullable=False),
            Column("embed_text", Text, nullable=False),
            Column("token_count", Integer, nullable=False),
            Column("page_start", Integer),
            Column("page_end", Integer),
            Column("page_label_start", Text),
            Column("page_label_end", Text),
            Column("source_url", Text),
            Column("cfr_refs", ARRAY(Text), nullable=False),
            Column("control_refs", ARRAY(Text), nullable=False),
            Column("metadata", JSONB, nullable=False),
            Column("content_sha256", Text, nullable=False),
            Column("embedding_model", Text, nullable=False),
            Column("embedding", Vector(embedding_dim), nullable=False),
            Column("tsv", TSVECTOR, Computed(_TSV_EXPR, persisted=True)),
            Index("ix_chunks_document_id", "document_id"),
            Index("ix_chunks_filters", "domain", "publisher", "doc_type"),
            Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
            Index("ix_chunks_cfr_refs", "cfr_refs", postgresql_using="gin"),
            Index("ix_chunks_control_refs", "control_refs", postgresql_using="gin"),
            Index(
                "ix_chunks_embedding_hnsw",
                "embedding",
                postgresql_using="hnsw",
                postgresql_with={"m": 16, "ef_construction": 64},
                postgresql_ops={"embedding": "vector_cosine_ops"},
            ),
        )
        self.runs = Table(
            "ingestion_runs",
            self.metadata,
            Column("id", Text, primary_key=True),
            Column("started_at", DateTime(timezone=True), nullable=False),
            Column("finished_at", DateTime(timezone=True)),
            Column("status", Text, nullable=False),
            Column("options", JSONB, nullable=False),
            Column("report", JSONB),
            Column("error", Text),
        )

    # -- schema ----------------------------------------------------------------------------

    def init_schema(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            self.metadata.create_all(conn)
            stored_dim = conn.execute(
                text(
                    "SELECT atttypmod FROM pg_attribute "
                    "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
                )
            ).scalar_one()
            if stored_dim != self.dim:
                raise IngestionError(
                    f"chunks.embedding is vector({stored_dim}) but RAG_EMBEDDING_DIM is {self.dim}; "
                    "changing the dimension requires dropping and rebuilding the knowledge base"
                )

    # -- reads -----------------------------------------------------------------------------

    def document_state(self, document_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(self.documents).where(self.documents.c.id == document_id)).mappings().first()
        return dict(row) if row else None

    def document_ids(self) -> list[str]:
        with self.engine.connect() as conn:
            return list(conn.execute(select(self.documents.c.id)).scalars())

    def reusable_embeddings(self, document_id: str, model: str) -> dict[str, tuple[str, list[float]]]:
        """chunk id -> (content hash, embedding) for chunks embedded with `model`."""
        c = self.chunks.c
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(c.id, c.content_sha256, c.embedding).where(c.document_id == document_id, c.embedding_model == model)
            )
            return {r.id: (r.content_sha256, list(r.embedding)) for r in rows}

    # -- writes ----------------------------------------------------------------------------

    def replace_document(
        self,
        source: SourceSpec,
        *,
        local_path: str,
        content_sha256: str,
        parser: str,
        parser_version: str,
        chunker_version: str,
        embedding_model: str,
        chunks: Sequence[Chunk],
        embeddings: dict[str, list[float]],
        metadata: dict[str, Any],
    ) -> None:
        """Atomically make `chunks` the document's content. Readers see the old or new version, never a mix."""
        missing = [c.id for c in chunks if c.id not in embeddings]
        if missing:
            raise IngestionError(f"{source.id}: {len(missing)} chunks have no embedding")

        doc_row = {
            "id": source.id,
            "title": source.title,
            "short_title": source.short_title,
            "publisher": source.publisher,
            "domain": source.domain.value,
            "doc_type": source.doc_type.value,
            "version": source.version,
            "effective_date": source.effective_date,
            "source_url": source.source_url,
            "local_path": local_path,
            "content_sha256": content_sha256,
            "parser": parser,
            "parser_version": parser_version,
            "chunker_version": chunker_version,
            "embedding_model": embedding_model,
            "chunk_count": len(chunks),
            "metadata": metadata,
            "ingested_at": datetime.now(UTC),
        }
        rows = [
            {
                **chunk.model_dump(mode="json", exclude={"document_id"}),
                "document_id": source.id,
                "domain": source.domain.value,
                "publisher": source.publisher,
                "doc_type": source.doc_type.value,
                "embedding_model": embedding_model,
                "embedding": embeddings[chunk.id],
            }
            for chunk in chunks
        ]

        with self.engine.begin() as conn:
            stmt = insert(self.documents).values(doc_row)
            conn.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={k: stmt.excluded[k] for k in doc_row if k != "id"}))
            conn.execute(
                delete(self.chunks).where(
                    self.chunks.c.document_id == source.id, self.chunks.c.id.not_in([c.id for c in chunks])
                )
            )
            for batch in _batched(rows, _BATCH):
                stmt = insert(self.chunks).values(batch)
                updatable = [k for k in batch[0] if k != "id"]
                conn.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={k: stmt.excluded[k] for k in updatable}))

    def delete_document(self, document_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(self.documents).where(self.documents.c.id == document_id))

    def start_run(self, options: dict[str, Any]) -> str:
        run_id = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(
                insert(self.runs).values(id=run_id, started_at=datetime.now(UTC), status="running", options=options)
            )
        return run_id

    def finish_run(self, run_id: str, status: str, report: dict[str, Any], error: str | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(self.runs)
                .where(self.runs.c.id == run_id)
                .values(finished_at=datetime.now(UTC), status=status, report=report, error=error)
            )


def _batched(rows: Sequence[dict], size: int) -> Iterable[Sequence[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]
