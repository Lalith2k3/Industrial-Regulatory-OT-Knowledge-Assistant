"""PostgreSQL implementations of the DocumentRepository and RunLog interfaces."""

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, delete, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.ingestion.domain.errors import StorageError
from app.ingestion.domain.models import Chunk, DocumentFingerprint, DocumentRecord, RunStatus, StoredDocument
from app.ingestion.storage.schema import KnowledgeSchema

_INSERT_BATCH_ROWS = 500
_SPEC_HASH_KEY = "spec_sha256"  # kept inside documents.metadata


class PostgresDocumentRepository:
    def __init__(self, engine: Engine, schema: KnowledgeSchema):
        self._engine = engine
        self._schema = schema

    def ensure_schema(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            self._schema.metadata.create_all(conn)
            stored_dim = conn.execute(
                text("SELECT atttypmod FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'")
            ).scalar_one()
        if stored_dim != self._schema.embedding_dim:
            raise StorageError(
                f"chunks.embedding is vector({stored_dim}) but RAG_EMBEDDING_DIM is {self._schema.embedding_dim}; "
                "changing the dimension requires dropping and rebuilding the knowledge base"
            )

    def get(self, document_id: str) -> StoredDocument | None:
        documents = self._schema.documents
        with self._engine.connect() as conn:
            row = conn.execute(select(documents).where(documents.c.id == document_id)).mappings().first()
        if row is None:
            return None
        return StoredDocument(
            fingerprint=DocumentFingerprint(
                content_sha256=row["content_sha256"],
                spec_sha256=(row["metadata"] or {}).get(_SPEC_HASH_KEY, ""),
                parser_version=row["parser_version"],
                chunker_version=row["chunker_version"],
                embedding_model=row["embedding_model"],
            ),
            chunk_count=row["chunk_count"],
        )

    def document_ids(self) -> list[str]:
        with self._engine.connect() as conn:
            return list(conn.execute(select(self._schema.documents.c.id)).scalars())

    def reusable_embeddings(self, document_id: str, model: str) -> dict[str, tuple[str, list[float]]]:
        c = self._schema.chunks.c
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(c.id, c.content_sha256, c.embedding).where(
                    c.document_id == document_id, c.embedding_model == model
                )
            )
            return {r.id: (r.content_sha256, list(r.embedding)) for r in rows}

    def replace(self, record: DocumentRecord, chunks: Sequence[Chunk], embeddings: dict[str, list[float]]) -> None:
        """Atomically make `chunks` the document's content. Readers see the old or new version, never a mix."""
        source, fp = record.source, record.fingerprint
        if missing := [c.id for c in chunks if c.id not in embeddings]:
            raise StorageError(f"{source.id}: {len(missing)} chunks have no embedding")

        doc_row: dict[str, Any] = {
            "id": source.id,
            "title": source.title,
            "short_title": source.short_title,
            "publisher": source.publisher,
            "domain": source.domain.value,
            "doc_type": source.doc_type.value,
            "version": source.version,
            "effective_date": source.effective_date,
            "source_url": source.source_url,
            "local_path": record.local_path,
            "content_sha256": fp.content_sha256,
            "parser": record.parser,
            "parser_version": fp.parser_version,
            "chunker_version": fp.chunker_version,
            "embedding_model": fp.embedding_model,
            "chunk_count": len(chunks),
            "metadata": {**record.metadata, _SPEC_HASH_KEY: fp.spec_sha256},
            "ingested_at": datetime.now(UTC),
        }
        chunk_rows = [
            {
                **chunk.model_dump(mode="json", exclude={"document_id"}),
                "document_id": source.id,
                "domain": source.domain.value,
                "publisher": source.publisher,
                "doc_type": source.doc_type.value,
                "embedding_model": fp.embedding_model,
                "embedding": embeddings[chunk.id],
            }
            for chunk in chunks
        ]

        documents, chunk_table = self._schema.documents, self._schema.chunks
        with self._engine.begin() as conn:
            stmt = pg_insert(documents).values(doc_row)
            conn.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=_excluded(stmt, doc_row)))
            conn.execute(
                delete(chunk_table).where(
                    chunk_table.c.document_id == source.id, chunk_table.c.id.not_in([c.id for c in chunks])
                )
            )
            for batch in _batched(chunk_rows, _INSERT_BATCH_ROWS):
                stmt = pg_insert(chunk_table).values(batch)
                conn.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=_excluded(stmt, batch[0])))

    def delete(self, document_id: str) -> None:
        documents = self._schema.documents
        with self._engine.begin() as conn:
            conn.execute(delete(documents).where(documents.c.id == document_id))


class PostgresRunLog:
    def __init__(self, engine: Engine, schema: KnowledgeSchema):
        self._engine = engine
        self._runs = schema.runs

    def start(self, options: dict[str, Any]) -> str:
        run_id = uuid.uuid4().hex
        with self._engine.begin() as conn:
            conn.execute(
                insert(self._runs).values(
                    id=run_id, started_at=datetime.now(UTC), status=RunStatus.RUNNING.value, options=options
                )
            )
        return run_id

    def finish(self, run_id: str, status: RunStatus, report: dict[str, Any], error: str | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(self._runs)
                .where(self._runs.c.id == run_id)
                .values(finished_at=datetime.now(UTC), status=status.value, report=report, error=error)
            )


def _excluded(stmt: Any, row: dict[str, Any]) -> dict[str, Any]:
    """ON CONFLICT DO UPDATE: overwrite every column except the primary key."""
    return {k: stmt.excluded[k] for k in row if k != "id"}


def _batched(rows: Sequence[dict[str, Any]], size: int) -> Iterator[Sequence[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]
