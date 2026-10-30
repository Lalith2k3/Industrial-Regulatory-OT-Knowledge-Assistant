"""Runtime configuration.

Everything that differs between environments (paths, credentials, endpoints, provider limits,
model names) lives here and can be overridden with a RAG_* environment variable or the project's
.env file. Algorithmic constants with a domain meaning (chunk sizes, layout thresholds) stay next
to the code that uses them, named and documented.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=PROJECT_ROOT / ".env",
        extra="ignore",
    )

    # -- paths -----------------------------------------------------------------------------
    data_dir: Path = PROJECT_ROOT / "data"
    sources_file: Path = PROJECT_ROOT / "data" / "sources.yaml"
    processed_dir: Path = PROJECT_ROOT / "data" / "processed"

    # -- database (no default: credentials never live in source code) ----------------------
    database_url: SecretStr | None = None

    # -- embeddings ------------------------------------------------------------------------
    voyage_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("RAG_VOYAGE_API_KEY", "VOYAGE_API_KEY"),
    )
    # "voyage-4-large" for real runs; "local-hash" is a deterministic offline embedder for development only.
    embedding_model: str = "voyage-4-large"
    embedding_dim: int = 1024
    embedding_batch_size: int = 64
    # Provider limits. Defaults match a Voyage account WITHOUT a payment method (free tokens still
    # apply); with one, voyage-4-large allows 2000 requests / 3,000,000 tokens per minute.
    embedding_requests_per_minute: int = 3
    embedding_tokens_per_minute: int = 10_000
    embedding_request_timeout_seconds: float = 120.0
    embedding_max_rate_limited_retries: int = 8
    embedding_max_transient_retries: int = 6

    # -- eCFR ------------------------------------------------------------------------------
    ecfr_base_url: str = "https://www.ecfr.gov"
    http_user_agent: str = "RAG_Project-ingestion/0.1"
    http_timeout_seconds: float = 60.0

    # -- cross-references ------------------------------------------------------------------
    # CFR parts whose section numbers are recognised in free text ("195.452"). Restricting to the
    # pipeline-safety parts (49 CFR 190-199) avoids reading decimals such as "12.5" as citations.
    cfr_reference_parts: list[int] = Field(default_factory=lambda: list(range(190, 200)))

    # -- query: retrieval ------------------------------------------------------------------
    query_embedding_timeout_seconds: float = 30.0
    retrieval_candidates: int = 30  # per retrieval signal, before fusion
    retrieval_top_k: int = 10  # fused results logged and offered to the context builder
    context_max_tokens: int = 6_000  # passages the answer model reads (estimated tokens)
    evaluation_file: Path = PROJECT_ROOT / "data" / "evaluation" / "questions.json"

    # -- query: answer generation (Claude) -------------------------------------------------
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("RAG_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    answer_model: str = "claude-opus-5-5"
    # Thinking depth for answers. Higher is more thorough and slower (NFR-03 targets about 5 s).
    answer_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    answer_max_tokens: int = 16_000
    rewrite_model: str = "claude-haiku-5-5"  # standalone questions for follow-ups (FR-08)
    history_max_turns: int = 6  # earlier conversation turns sent with a question
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2

    # -- HTTP API --------------------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    frontend_dist: Path = PROJECT_ROOT / "frontend" / "dist"  # served at / when built

    @property
    def embedding_cache_file(self) -> Path:
        return self.processed_dir / ".embedding-cache" / f"{self.embedding_model}-{self.embedding_dim}.jsonl"

    @property
    def query_embedding_cache_file(self) -> Path:
        """Questions are embedded with input_type="query", so they never share the document cache."""
        return self.processed_dir / ".embedding-cache" / f"{self.embedding_model}-{self.embedding_dim}-query.jsonl"


@lru_cache
def get_settings() -> Settings:
    return Settings()
