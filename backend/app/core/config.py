from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Runtime configuration. Every field can be overridden with a RAG_* environment variable."""

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=PROJECT_ROOT / ".env",
        extra="ignore",
    )

    data_dir: Path = PROJECT_ROOT / "data"
    sources_file: Path = PROJECT_ROOT / "data" / "sources.yaml"
    processed_dir: Path = PROJECT_ROOT / "data" / "processed"

    database_url: str = "postgresql+psycopg://rag:rag@localhost:5433/rag"

    voyage_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("RAG_VOYAGE_API_KEY", "VOYAGE_API_KEY"),
    )
    # "voyage-4-large" for real runs; "local-hash" is a deterministic offline embedder for development only.
    embedding_model: str = "voyage-4-large"
    embedding_dim: int = 1024
    embedding_batch_size: int = 64
    # Voyage limits for an account WITHOUT a payment method (free tokens still apply).
    # After adding one, raise to e.g. 2000 requests / 3_000_000 tokens (voyage-4-large, tier 1).
    embedding_requests_per_minute: int = 3
    embedding_tokens_per_minute: int = 10_000

    ecfr_base_url: str = "https://www.ecfr.gov"
    http_user_agent: str = "RAG_Project-ingestion/0.1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
