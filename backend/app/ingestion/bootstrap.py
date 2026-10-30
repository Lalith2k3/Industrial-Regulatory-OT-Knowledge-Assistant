"""Composition root: the only place that reads Settings and chooses concrete implementations.

Everything else receives its collaborators through constructors (dependency inversion), so
swapping Postgres, the embedding provider or a parser is a change here, not in the pipeline.
"""

from dataclasses import dataclass

from sqlalchemy import Engine, create_engine

from app.core.config import Settings
from app.ingestion.chunking import DEFAULT_POLICIES, Chunker
from app.ingestion.domain.errors import ConfigurationError
from app.ingestion.domain.interfaces import Embedder
from app.ingestion.embedding import LOCAL_HASH_MODEL, EmbeddingCache, HashEmbedder, VoyageEmbedder, VoyageLimits
from app.ingestion.parsing import ParserRegistry
from app.ingestion.parsing.ecfr import EcfrXmlParser
from app.ingestion.parsing.enforcement_guidance import EnforcementGuidanceParser
from app.ingestion.parsing.faq import FaqPdfParser
from app.ingestion.parsing.nist import NistSpParser
from app.ingestion.pipeline import IngestionPipeline, KnowledgeStore
from app.ingestion.quality import QualityGate
from app.ingestion.references import CfrReferenceExtractor, ControlReferenceExtractor
from app.ingestion.sources.ecfr_client import EcfrClient
from app.ingestion.storage import (
    FileArtifactSink,
    KnowledgeSchema,
    PostgresDocumentRepository,
    PostgresRunLog,
    build_schema,
)


@dataclass(frozen=True)
class References:
    cfr: CfrReferenceExtractor
    controls: ControlReferenceExtractor


def build_references(settings: Settings) -> References:
    return References(CfrReferenceExtractor(settings.cfr_reference_parts), ControlReferenceExtractor())


def build_parsers(settings: Settings, references: References) -> ParserRegistry:
    """Register one parser per document family (sources.yaml `parser:` names)."""
    return ParserRegistry(
        [
            EcfrXmlParser(site_url=settings.ecfr_base_url),
            EnforcementGuidanceParser(),
            FaqPdfParser(cfr_refs=references.cfr),
            NistSpParser(),
        ]
    )


def build_chunker(references: References) -> Chunker:
    return Chunker(DEFAULT_POLICIES, cfr_refs=references.cfr, control_refs=references.controls)


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_model == LOCAL_HASH_MODEL:
        return HashEmbedder(settings.embedding_dim)
    if settings.voyage_api_key is None:
        raise ConfigurationError(
            f"embedding model {settings.embedding_model!r} needs RAG_VOYAGE_API_KEY (or VOYAGE_API_KEY); "
            f"set RAG_EMBEDDING_MODEL={LOCAL_HASH_MODEL} for an offline development run"
        )
    return VoyageEmbedder.from_api_key(
        settings.voyage_api_key.get_secret_value(),
        timeout_seconds=settings.embedding_request_timeout_seconds,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        limits=VoyageLimits(
            requests_per_minute=settings.embedding_requests_per_minute,
            tokens_per_minute=settings.embedding_tokens_per_minute,
            max_rate_limited_retries=settings.embedding_max_rate_limited_retries,
            max_transient_retries=settings.embedding_max_transient_retries,
            batch_size=settings.embedding_batch_size,
        ),
        cache=EmbeddingCache(settings.embedding_cache_file),
    )


def build_document_repository(settings: Settings) -> PostgresDocumentRepository:
    engine, schema = _database(settings)
    return PostgresDocumentRepository(engine, schema)


def build_knowledge_store(settings: Settings) -> KnowledgeStore:
    engine, schema = _database(settings)
    return KnowledgeStore(
        documents=PostgresDocumentRepository(engine, schema),
        runs=PostgresRunLog(engine, schema),
        embedder=build_embedder(settings),
    )


def build_pipeline(settings: Settings, *, dry_run: bool = False) -> IngestionPipeline:
    references = build_references(settings)
    return IngestionPipeline(
        data_dir=settings.data_dir,
        parsers=build_parsers(settings, references),
        chunker=build_chunker(references),
        quality_gate=QualityGate(),
        artifacts=FileArtifactSink(settings.processed_dir),
        store=None if dry_run else build_knowledge_store(settings),
    )


def build_ecfr_client(settings: Settings) -> EcfrClient:
    return EcfrClient(settings.ecfr_base_url, settings.http_user_agent, settings.http_timeout_seconds)


def _database(settings: Settings) -> tuple[Engine, KnowledgeSchema]:
    if settings.database_url is None:
        raise ConfigurationError("RAG_DATABASE_URL is not set (see .env.example)")
    engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    return engine, build_schema(settings.embedding_dim)
