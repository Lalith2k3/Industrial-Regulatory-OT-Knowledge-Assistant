"""Voyage AI query embedder.

Chunks were embedded with input_type="document"; questions must use the same model and dimension
with input_type="query". A question is one short request, so there is no batch sizing here, only
the shared rate limiter (the free tier allows 3 requests per minute), a small retry policy and an
on-disk cache so that repeated questions (evaluation runs, retries) cost nothing.
"""

import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

import structlog
import voyageai.error as voyage_errors
from voyageai.client import Client as VoyageClient

from app.ingestion.embedding import EmbeddingCache, RateLimiter
from app.ingestion.text import estimate_tokens
from app.query.domain.errors import EmbeddingError

log = structlog.get_logger("query.embedding")

_TRANSIENT_ERRORS = (
    voyage_errors.ServiceUnavailableError,
    voyage_errors.ServerError,
    voyage_errors.APIConnectionError,
    voyage_errors.Timeout,
    voyage_errors.TryAgain,
)


class VoyageQueryEmbedder:
    MAX_BATCH_TEXTS = 128  # evaluation runs embed every question in one request
    # Our estimate undercounts Voyage's tokenizer by ~15%; the limiter only needs a safe upper bound.
    TOKEN_FACTOR = 1.3
    MAX_TRANSIENT_BACKOFF_SECONDS = 30.0

    def __init__(
        self,
        client: Any,
        model: str,
        dim: int,
        limiter: RateLimiter,
        *,
        max_rate_limited_retries: int,
        max_transient_retries: int,
        cache: EmbeddingCache | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.model = model
        self.dim = dim
        self._client = client
        self._limiter = limiter
        self._max_rate_limited_retries = max_rate_limited_retries
        self._max_transient_retries = max_transient_retries
        self._cache = cache
        self._sleep = sleep
        self._lock = threading.Lock()  # the limiter and the cache are not thread-safe

    @classmethod
    def from_api_key(cls, api_key: str, timeout_seconds: float, **kwargs: Any) -> "VoyageQueryEmbedder":
        # Retries are handled here (with rate-limit awareness), so the SDK's own retries are off.
        return cls(VoyageClient(api_key=api_key, max_retries=0, timeout=timeout_seconds), **kwargs)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        with self._lock:
            results: list[list[float] | None] = [self._cache.get(t) if self._cache else None for t in texts]
            todo = [i for i, v in enumerate(results) if v is None]
            for start in range(0, len(todo), self.MAX_BATCH_TEXTS):
                batch = todo[start : start + self.MAX_BATCH_TEXTS]
                for i, vector in zip(batch, self._embed([texts[i] for i in batch]), strict=True):
                    results[i] = vector
            return [v for v in results if v is not None]

    def _embed(self, batch: list[str]) -> list[list[float]]:
        tokens = int(sum(estimate_tokens(t) for t in batch) * self.TOKEN_FACTOR) + 1
        rate_limited = transient = 0
        while True:
            self._limiter.acquire(tokens)
            try:
                result = self._client.embed(
                    batch,
                    model=self.model,
                    input_type="query",
                    output_dimension=self.dim,
                    truncation=True,  # a question longer than the model context is not worth failing over
                )
            except voyage_errors.RateLimitError as exc:
                rate_limited += 1
                if rate_limited > self._max_rate_limited_retries:
                    raise EmbeddingError(f"still rate limited after {rate_limited - 1} retries") from exc
                self._limiter.penalise()
                log.warning("query_embedding.rate_limited", attempt=rate_limited)
                continue
            except _TRANSIENT_ERRORS as exc:
                transient += 1
                if transient > self._max_transient_retries:
                    raise EmbeddingError(f"Voyage unavailable after {transient - 1} retries: {exc}") from exc
                wait = min(self.MAX_TRANSIENT_BACKOFF_SECONDS, 2.0**transient)
                log.warning("query_embedding.transient_error", attempt=transient, wait_seconds=wait, error=str(exc))
                self._sleep(wait)
                continue
            except voyage_errors.VoyageError as exc:  # bad key, invalid request: retrying will not help
                raise EmbeddingError(f"Voyage rejected the query embedding request: {exc}") from exc

            if len(result.embeddings) != len(batch):
                raise EmbeddingError(f"expected {len(batch)} embeddings, got {len(result.embeddings)}")
            self._limiter.record(result.total_tokens)
            vectors = [list(v) for v in result.embeddings]
            if self._cache:
                self._cache.add_many(batch, vectors)
            return vectors
