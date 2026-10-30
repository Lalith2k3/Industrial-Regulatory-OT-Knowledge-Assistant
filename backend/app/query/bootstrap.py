"""Composition root for the query side: the only place that reads Settings and chooses concrete
implementations. Everything else receives its collaborators through constructors."""

from dataclasses import dataclass

import anthropic
from sqlalchemy import Engine, create_engine

from app.core.config import Settings
from app.ingestion.embedding import LOCAL_HASH_MODEL, EmbeddingCache, RateLimiter
from app.ingestion.references import CfrReferenceExtractor, ControlReferenceExtractor
from app.ingestion.storage.schema import build_schema
from app.query.citations import CitationResolver
from app.query.context import ContextBuilder
from app.query.conversations import ConversationService
from app.query.domain.errors import ConfigurationError
from app.query.domain.interfaces import AnswerGenerator, DocumentCatalog, QueryEmbedder, QueryRewriter
from app.query.embedding import HashQueryEmbedder, VoyageQueryEmbedder
from app.query.generation import (
    LOCAL_EXTRACTIVE_MODEL,
    ClaudeAnswerGenerator,
    ClaudeQueryRewriter,
    ExtractiveAnswerGenerator,
    FallbackRewriter,
    PreviousQuestionRewriter,
)
from app.query.retrieval import (
    HybridRetriever,
    PostgresIdentifierSearch,
    PostgresReferenceSearch,
    PostgresVectorSearch,
    QueryAnalyzer,
    WeightedSource,
)
from app.query.service import RagService, Searcher
from app.query.storage import (
    PostgresConversationRepository,
    PostgresDocumentCatalog,
    PostgresQueryLog,
    build_query_schema,
    create_query_tables,
)

# Reciprocal-rank-fusion weights per signal, measured with `python -m app.query evaluate-retrieval`
# against data/evaluation/questions.json (ADR-0006).
VECTOR_WEIGHT = 1.0
REFERENCE_WEIGHT = 1.0
IDENTIFIER_WEIGHT = 1.0


def build_engine(settings: Settings) -> Engine:
    if settings.database_url is None:
        raise ConfigurationError("RAG_DATABASE_URL is not set (see .env.example)")
    return create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)


def build_query_embedder(settings: Settings) -> QueryEmbedder:
    if settings.embedding_model == LOCAL_HASH_MODEL:
        return HashQueryEmbedder(settings.embedding_dim)
    if settings.voyage_api_key is None:
        raise ConfigurationError(
            f"embedding model {settings.embedding_model!r} needs RAG_VOYAGE_API_KEY (or VOYAGE_API_KEY) "
            "to embed questions"
        )
    return VoyageQueryEmbedder.from_api_key(
        settings.voyage_api_key.get_secret_value(),
        timeout_seconds=settings.query_embedding_timeout_seconds,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        limiter=RateLimiter(settings.embedding_requests_per_minute, settings.embedding_tokens_per_minute),
        max_rate_limited_retries=settings.embedding_max_rate_limited_retries,
        max_transient_retries=settings.embedding_max_transient_retries,
        cache=EmbeddingCache(settings.query_embedding_cache_file),
    )


def build_retriever(settings: Settings, engine: Engine) -> HybridRetriever:
    schema = build_schema(settings.embedding_dim)
    return HybridRetriever(
        [
            WeightedSource(PostgresVectorSearch(engine, schema), VECTOR_WEIGHT),
            WeightedSource(PostgresReferenceSearch(engine, schema), REFERENCE_WEIGHT),
            WeightedSource(PostgresIdentifierSearch(engine, schema), IDENTIFIER_WEIGHT),
        ],
        candidates=settings.retrieval_candidates,
        top_k=settings.retrieval_top_k,
    )


def build_searcher(settings: Settings, engine: Engine) -> Searcher:
    analyzer = QueryAnalyzer(CfrReferenceExtractor(settings.cfr_reference_parts), ControlReferenceExtractor())
    return Searcher(build_query_embedder(settings), analyzer, build_retriever(settings, engine))


def build_rewriter(settings: Settings) -> QueryRewriter:
    """Claude rewrites follow-ups when a key is configured; otherwise (and if Claude fails) the
    previous question is prefixed, which needs no model."""
    if settings.anthropic_api_key is None:
        return PreviousQuestionRewriter()
    claude = ClaudeQueryRewriter(
        _anthropic_client(settings), settings.rewrite_model, max_history_turns=settings.history_max_turns
    )
    return FallbackRewriter(claude, PreviousQuestionRewriter())


def build_generator(settings: Settings) -> AnswerGenerator:
    if settings.answer_model == LOCAL_EXTRACTIVE_MODEL:
        return ExtractiveAnswerGenerator()
    return ClaudeAnswerGenerator(
        _anthropic_client(settings),
        settings.answer_model,
        effort=settings.answer_effort,
        max_tokens=settings.answer_max_tokens,
        max_history_turns=settings.history_max_turns,
    )


def build_rag_service(settings: Settings, engine: Engine | None = None) -> RagService:
    engine = engine or build_engine(settings)
    schema = build_query_schema()
    create_query_tables(engine, schema)
    return RagService(
        rewriter=build_rewriter(settings),
        searcher=build_searcher(settings, engine),
        context_builder=ContextBuilder(settings.context_max_tokens),
        generator=build_generator(settings),
        citations=CitationResolver(),
        query_log=PostgresQueryLog(engine, schema),
    )


@dataclass(frozen=True)
class ApiServices:
    """What the HTTP API needs, built once at start-up."""

    conversations: ConversationService
    catalog: DocumentCatalog
    answer_model: str
    embedding_model: str

    @property
    def offline_answers(self) -> bool:
        return self.answer_model == LOCAL_EXTRACTIVE_MODEL


def build_api_services(settings: Settings) -> ApiServices:
    engine = build_engine(settings)
    rag = build_rag_service(settings, engine)  # also creates the query-side tables
    return ApiServices(
        conversations=ConversationService(PostgresConversationRepository(engine, build_query_schema()), rag),
        catalog=PostgresDocumentCatalog(engine, build_schema(settings.embedding_dim)),
        answer_model=settings.answer_model,
        embedding_model=settings.embedding_model,
    )


def _anthropic_client(settings: Settings) -> anthropic.Anthropic:
    if settings.anthropic_api_key is None:
        raise ConfigurationError("answer generation needs RAG_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY)")
    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
