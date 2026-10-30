"""Query orchestration: rewrite follow-up -> embed -> retrieve -> build context -> generate -> cite -> log.

The service only coordinates. Every capability it uses is passed in by bootstrap.py, so it can be
unit-tested with in-memory fakes. Failures surface as QueryError subclasses (NFR-04) and are
written to the query log together with whatever was retrieved before the failure (US-08).
"""

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from app.query.citations import CitationResolver
from app.query.context import ContextBuilder
from app.query.domain.interfaces import (
    AnswerGenerator,
    AnswerListener,
    QueryEmbedder,
    QueryLog,
    QueryRewriter,
    Retriever,
)
from app.query.domain.models import (
    Answer,
    AnswerStage,
    AnswerStatus,
    Citation,
    ContextSource,
    QueryLogEntry,
    QueryRequest,
    RetrievalTrace,
    RetrievedChunk,
    SearchFilters,
    SearchQuery,
    Timings,
)
from app.query.retrieval.analysis import QueryAnalyzer

log = structlog.get_logger("query")

NO_EVIDENCE_ANSWER = (
    "The approved knowledge base has no passages relevant to this question, so it cannot be answered "
    "from the approved sources. Try rephrasing the question or removing source filters."
)


class SilentListener:
    """AnswerListener that ignores everything (non-streaming callers)."""

    def on_stage(self, stage: AnswerStage) -> None:
        pass

    def on_sources(self, sources: Sequence[Citation]) -> None:
        pass

    def on_text(self, delta: str) -> None:
        pass

    def on_restart(self) -> None:
        pass


@dataclass(frozen=True)
class SearchOutcome:
    query: SearchQuery
    chunks: list[RetrievedChunk]
    embedding_ms: int
    retrieval_ms: int


class Searcher:
    """Embed -> analyse -> retrieve. Shared by the RAG service, the `search` command and evaluation."""

    def __init__(self, embedder: QueryEmbedder, analyzer: QueryAnalyzer, retriever: Retriever):
        self._embedder = embedder
        self._analyzer = analyzer
        self._retriever = retriever

    @property
    def embedding_model(self) -> str:
        return self._embedder.model

    def search(self, text: str, filters: SearchFilters) -> SearchOutcome:
        return self.search_many([text], filters)[0]

    def search_many(self, texts: Sequence[str], filters: SearchFilters) -> list[SearchOutcome]:
        """Embeds all texts in one request (the free Voyage tier allows 3 requests per minute)."""
        started = time.perf_counter()
        vectors = self._embedder.embed_queries(texts)
        embedding_ms = _ms_since(started)
        outcomes = []
        for text, vector in zip(texts, vectors, strict=True):
            started = time.perf_counter()
            query = self._analyzer.analyze(text, vector, filters)
            chunks = self._retriever.retrieve(query)
            outcomes.append(SearchOutcome(query, chunks, embedding_ms, _ms_since(started)))
        return outcomes


class RagService:
    def __init__(
        self,
        *,
        rewriter: QueryRewriter,
        searcher: Searcher,
        context_builder: ContextBuilder,
        generator: AnswerGenerator,
        citations: CitationResolver,
        query_log: QueryLog,
    ):
        self._rewriter = rewriter
        self._searcher = searcher
        self._context_builder = context_builder
        self._generator = generator
        self._citations = citations
        self._query_log = query_log

    def answer(self, request: QueryRequest, listener: AnswerListener | None = None) -> Answer:
        """Answer one question. A listener receives progress, the citable sources and the answer text."""
        listener = listener or SilentListener()
        query_id = uuid.uuid4().hex
        started = time.perf_counter()
        bound = log.bind(query_id=query_id)
        timings = Timings()
        models = {"embedding": self._searcher.embedding_model}
        standalone: str | None = None
        outcome: SearchOutcome | None = None
        sources: list[ContextSource] = []
        try:
            standalone = self._standalone_question(request, timings, models, listener)
            listener.on_stage(AnswerStage.RETRIEVING)
            outcome = self._searcher.search(standalone, request.filters)
            timings.embedding_ms, timings.retrieval_ms = outcome.embedding_ms, outcome.retrieval_ms
            sources = self._context_builder.build(outcome.chunks)
            answer, usage = self._answer(query_id, request, standalone, sources, timings, models, listener)
        except Exception as exc:
            timings.total_ms = _ms_since(started)
            bound.error("query.failed", error=str(exc), timings=timings.model_dump())
            entry = _log_entry(query_id, request, standalone, outcome, sources, timings, models, AnswerStatus.FAILED)
            self._record(entry.model_copy(update={"error": str(exc)}))
            raise

        timings.total_ms = _ms_since(started)
        answer = answer.model_copy(update={"timings": timings.model_copy()})
        bound.info(
            "query.answered",
            status=answer.status.value,
            sources=len(sources),
            cited=len(answer.citations),
            timings=timings.model_dump(),
        )
        entry = _log_entry(query_id, request, standalone, outcome, sources, timings, models, answer.status)
        cited = [c.chunk_id for c in answer.citations]
        self._record(entry.model_copy(update={"answer": answer.text, "cited_chunk_ids": cited, "usage": usage}))
        return answer

    def _standalone_question(
        self, request: QueryRequest, timings: Timings, models: dict[str, str], listener: AnswerListener
    ) -> str:
        if not request.history:
            return request.question
        listener.on_stage(AnswerStage.REWRITING)
        started = time.perf_counter()
        standalone = self._rewriter.rewrite(request.question, request.history)
        timings.rewrite_ms = _ms_since(started)
        models["rewrite"] = self._rewriter.model
        return standalone

    def _answer(
        self,
        query_id: str,
        request: QueryRequest,
        standalone: str,
        sources: list[ContextSource],
        timings: Timings,
        models: dict[str, str],
        listener: AnswerListener,
    ) -> tuple[Answer, dict[str, int]]:
        if not sources:  # nothing to ground an answer in: say so without asking the model (FR-06)
            return Answer(
                query_id=query_id,
                question=request.question,
                standalone_question=standalone,
                status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                text=NO_EVIDENCE_ANSWER,
            ), {}

        listener.on_sources(self._citations.describe(sources))
        listener.on_stage(AnswerStage.GENERATING)
        started = time.perf_counter()
        generated = self._generator.generate(request.question, sources, request.history, listener)
        timings.generation_ms = _ms_since(started)
        models["generation"] = generated.model

        resolved = self._citations.resolve(generated.text, sources)
        if resolved.unknown:
            log.warning("answer.unknown_citations", query_id=query_id, numbers=resolved.unknown)
        if generated.sufficient_evidence and not resolved.citations:
            log.warning("answer.uncited", query_id=query_id)  # NFR-02: visible in logs, kept for review
        return Answer(
            query_id=query_id,
            question=request.question,
            standalone_question=standalone,
            status=AnswerStatus.ANSWERED if generated.sufficient_evidence else AnswerStatus.INSUFFICIENT_EVIDENCE,
            text=generated.text,
            citations=resolved.citations,
        ), generated.usage

    def _record(self, entry: QueryLogEntry) -> None:
        """A failing query log must never cost the user their answer."""
        try:
            self._query_log.record(entry)
        except Exception as exc:
            log.error("query_log.failed", query_id=entry.id, error=str(exc))


def _log_entry(
    query_id: str,
    request: QueryRequest,
    standalone: str | None,
    outcome: SearchOutcome | None,
    sources: list[ContextSource],
    timings: Timings,
    models: dict[str, str],
    status: AnswerStatus,
) -> QueryLogEntry:
    in_context = {s.chunk.id for s in sources}
    retrieved = [
        RetrievalTrace(
            chunk_id=r.chunk.id,
            citation=r.chunk.citation,
            score=r.score,
            ranks=r.ranks,
            in_context=r.chunk.id in in_context,
        )
        for r in (outcome.chunks if outcome else [])
    ]
    return QueryLogEntry(
        id=query_id,
        created_at=datetime.now(UTC),
        question=request.question,
        standalone_question=standalone,
        history_turns=len(request.history),
        filters=request.filters,
        status=status,
        retrieved=retrieved,
        timings=timings.model_copy(),
        models=dict(models),
    )


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
