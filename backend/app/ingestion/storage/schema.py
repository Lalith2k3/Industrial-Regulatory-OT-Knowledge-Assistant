"""Knowledge base schema (PostgreSQL + pgvector).

Retrieval needs both halves of hybrid search, so each chunk stores an embedding (HNSW, cosine)
and a generated `tsv` column (GIN) for exact identifiers such as "195.452", "PI-74-0122", "AC-17".
Filter columns (domain, publisher, doc_type) are denormalised onto chunks for FR-07.
Changing anything here changes the database: rebuild the knowledge base afterwards.
"""

from dataclasses import dataclass

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Column,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

TEXT_SEARCH_CONFIG = "english"
HNSW_M = 16  # graph connectivity; pgvector's default
HNSW_EF_CONSTRUCTION = 64  # build-time candidate list; pgvector's default


@dataclass(frozen=True)
class KnowledgeSchema:
    metadata: MetaData
    documents: Table
    chunks: Table
    runs: Table
    embedding_dim: int


def build_schema(embedding_dim: int) -> KnowledgeSchema:
    metadata = MetaData()
    documents = Table(
        "documents",
        metadata,
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
    tsv = f"to_tsvector('{TEXT_SEARCH_CONFIG}', coalesce(citation, '') || ' ' || coalesce(heading, '') || ' ' || text)"
    chunks = Table(
        "chunks",
        metadata,
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
        Column("tsv", TSVECTOR, Computed(tsv, persisted=True)),
        Index("ix_chunks_document_id", "document_id"),
        Index("ix_chunks_filters", "domain", "publisher", "doc_type"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_cfr_refs", "cfr_refs", postgresql_using="gin"),
        Index("ix_chunks_control_refs", "control_refs", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": HNSW_M, "ef_construction": HNSW_EF_CONSTRUCTION},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
    runs = Table(
        "ingestion_runs",
        metadata,
        Column("id", Text, primary_key=True),
        Column("started_at", DateTime(timezone=True), nullable=False),
        Column("finished_at", DateTime(timezone=True)),
        Column("status", Text, nullable=False),
        Column("options", JSONB, nullable=False),
        Column("report", JSONB),
        Column("error", Text),
    )
    return KnowledgeSchema(metadata, documents, chunks, runs, embedding_dim)
