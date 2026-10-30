"""PostgreSQL + pgvector candidate sources over the knowledge base written by ingestion.

Three signals, one per CandidateSource (ADR-0003, ADR-0006):
- vector:     cosine similarity of the question embedding (HNSW index)
- reference:  chunks that *are* or *cite* a CFR section / NIST control named in the question
- identifier: full-text phrase match (`tsv` column) on document ids named in the question

All three apply the same FR-07 filters and read chunks joined with their document. The schema is
owned by ingestion (app/ingestion/storage/schema.py); this module only reads it.
"""

from typing import Any

from sqlalchemy import ColumnElement, Engine, Select, Text, case, cast, func, literal_column, or_, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import SQLAlchemyError

from app.ingestion.storage.schema import TEXT_SEARCH_CONFIG, KnowledgeSchema
from app.query.domain.errors import RetrievalError
from app.query.domain.models import KnowledgeChunk, ScoredChunk, SearchFilters, SearchQuery

# HNSW keeps only `ef_search` candidates per scan; with filters that can leave fewer than `limit`
# rows. Iterative scans (pgvector >= 0.8) keep scanning until the filtered result is full.
_MIN_EF_SEARCH = 40  # pgvector's default
_TS_RANK_NORMALIZATION = 1  # divide by 1 + log(document length): long sections do not win by size alone
_OWN_SECTION, _CITES_SECTION = 2, 1  # reference priorities


class _PostgresChunkSource:
    name: str

    def __init__(self, engine: Engine, schema: KnowledgeSchema):
        self._engine = engine
        self._schema = schema

    def search(self, query: SearchQuery, limit: int) -> list[ScoredChunk]:
        stmt = self._statement(query, limit)
        if stmt is None:
            return []
        try:
            with self._engine.begin() as conn:
                self._prepare(conn, limit)
                rows = conn.execute(stmt).mappings().all()
        except SQLAlchemyError as exc:
            raise RetrievalError(f"{self.name} search failed: {exc}") from exc
        return [ScoredChunk(chunk=KnowledgeChunk.model_validate(dict(r)), score=float(r["score"])) for r in rows]

    def _statement(self, query: SearchQuery, limit: int) -> Select[Any] | None:
        raise NotImplementedError

    def _prepare(self, conn: Any, limit: int) -> None:
        """Per-transaction settings (SET LOCAL) before the search runs."""

    def _select(self, score: ColumnElement[Any], filters: SearchFilters) -> Select[Any]:
        c, d = self._schema.chunks.c, self._schema.documents.c
        return (
            select(
                c.id,
                c.document_id,
                d.title.label("document_title"),
                d.short_title.label("document_short_title"),
                d.version.label("document_version"),
                c.publisher,
                c.domain,
                c.doc_type,
                c.kind,
                c.section_key,
                c.citation,
                c.heading,
                c.section_path,
                c.text,
                c.token_count,
                c.page_start,
                c.page_end,
                c.page_label_start,
                c.page_label_end,
                func.coalesce(c.source_url, d.source_url).label("source_url"),
                c.metadata,
                score.label("score"),
            )
            .select_from(self._schema.chunks.join(self._schema.documents))
            .where(*self._filters(filters))
        )

    def _filters(self, filters: SearchFilters) -> list[ColumnElement[bool]]:
        c = self._schema.chunks.c
        clauses: list[ColumnElement[bool]] = []
        if filters.domains:
            clauses.append(c.domain.in_([d.value for d in filters.domains]))
        if filters.publishers:
            clauses.append(c.publisher.in_(filters.publishers))
        if filters.doc_types:
            clauses.append(c.doc_type.in_([t.value for t in filters.doc_types]))
        if filters.document_ids:
            clauses.append(c.document_id.in_(filters.document_ids))
        return clauses

    def _distance(self, query: SearchQuery) -> ColumnElement[float]:
        distance: ColumnElement[float] = self._schema.chunks.c.embedding.cosine_distance(query.embedding)
        return distance


class PostgresVectorSearch(_PostgresChunkSource):
    name = "vector"

    def _statement(self, query: SearchQuery, limit: int) -> Select[Any]:
        distance = self._distance(query)
        return self._select(1 - distance, query.filters).order_by(distance).limit(limit)

    def _prepare(self, conn: Any, limit: int) -> None:
        conn.execute(text(f"SET LOCAL hnsw.ef_search = {max(_MIN_EF_SEARCH, 2 * int(limit))}"))
        conn.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))


class PostgresIdentifierSearch(_PostgresChunkSource):
    """Full-text phrase match on document identifiers named in the question (PI-09-0005, ADB-11-04).

    Runs only when the question contains identifiers. Full-text search over every question word
    was measured and dropped: on natural-language questions it ranked boilerplate above the
    governing section and lowered MRR from 0.94 to 0.73 (ADR-0006).
    """

    name = "identifier"

    def _statement(self, query: SearchQuery, limit: int) -> Select[Any] | None:
        if not query.identifiers:
            return None
        config: ColumnElement[Any] = literal_column(f"'{TEXT_SEARCH_CONFIG}'::regconfig")
        any_identifier: ColumnElement[Any] = func.phraseto_tsquery(config, query.identifiers[0])
        for identifier in query.identifiers[1:]:
            any_identifier = any_identifier.op("||")(func.phraseto_tsquery(config, identifier))
        tsv = self._schema.chunks.c.tsv
        rank = func.ts_rank_cd(tsv, any_identifier, _TS_RANK_NORMALIZATION)
        return (
            self._select(rank, query.filters)
            .where(tsv.op("@@")(any_identifier))
            .order_by(rank.desc(), self._distance(query))
            .limit(limit)
        )


class PostgresReferenceSearch(_PostgresChunkSource):
    """Exact identifiers beat similarity: "195.452" must surface 49 CFR 195.452 itself.

    A chunk *is* the section when its section key is the reference (195.452, 195.412/guidance,
    F.7.18/SI-2); it *cites* the section when the reference is in cfr_refs / control_refs.
    Within each group, chunks closest to the question come first.
    """

    name = "reference"

    def _statement(self, query: SearchQuery, limit: int) -> Select[Any] | None:
        if not query.cfr_refs and not query.control_refs:
            return None
        c = self._schema.chunks.c
        own: list[ColumnElement[bool]] = []
        cites: list[ColumnElement[bool]] = []
        if query.cfr_refs:
            own += [c.section_key.in_(query.cfr_refs), *(c.section_key.like(f"{r}/%") for r in query.cfr_refs)]
            cites.append(c.cfr_refs.op("&&")(_text_array(query.cfr_refs)))
        if query.control_refs:
            own += [c.section_key.like(f"%/{r}") for r in query.control_refs]
            cites.append(c.control_refs.op("&&")(_text_array(query.control_refs)))
        priority = case((or_(*own), _OWN_SECTION), else_=_CITES_SECTION)
        return (
            self._select(priority, query.filters)
            .where(or_(*own, *cites))
            .order_by(priority.desc(), self._distance(query))
            .limit(limit)
        )


def _text_array(values: list[str]) -> ColumnElement[Any]:
    """A bound text[] parameter; the array columns are text[] and && needs matching types."""
    return cast(values, ARRAY(Text))
