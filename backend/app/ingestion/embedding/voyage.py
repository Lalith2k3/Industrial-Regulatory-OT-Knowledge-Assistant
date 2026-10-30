"""Voyage AI document embedder.

Documents are embedded with input_type="document"; the query side must use the same model and
dimension with input_type="query". The embedder paces itself to the configured provider limits,
sizes batches from exact token counts, backs off and splits batches on rate-limit refusals, and
saves every accepted batch to the cache at once.
"""

import math
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
import voyageai.error as voyage_errors
from voyageai.client import Client as VoyageClient

from app.ingestion.domain.errors import EmbeddingError
from app.ingestion.embedding.cache import EmbeddingCache
from app.ingestion.embedding.rate_limit import RateLimiter
from app.ingestion.text import estimate_tokens

log = structlog.get_logger("ingestion.embedding")

_TRANSIENT_ERRORS = (
    voyage_errors.ServiceUnavailableError,
    voyage_errors.ServerError,
    voyage_errors.APIConnectionError,
    voyage_errors.Timeout,
    voyage_errors.TryAgain,
)


@dataclass(frozen=True)
class VoyageLimits:
    requests_per_minute: int
    tokens_per_minute: int
    max_rate_limited_retries: int
    max_transient_retries: int
    batch_size: int = 64


class VoyageEmbedder:
    MAX_BATCH_TEXTS = 1000  # API cap per request
    MAX_BATCH_TOKENS = 100_000  # API cap for voyage-4-large is 120k per request
    # A request uses at most this share of the per-minute budget: Voyage refuses requests that use
    # a full minute's worth even at 60 s intervals (observed on a 10k TPM account).
    BATCH_SHARE_OF_BUDGET = 0.5
    MAX_BACKOFF_SECONDS = 300.0
    MAX_TRANSIENT_BACKOFF_SECONDS = 60.0
    # Used only if Voyage's local tokenizer cannot be loaded; the observed ratio to our estimate is ~1.15.
    FALLBACK_TOKEN_FACTOR = 1.3

    def __init__(
        self,
        client: Any,
        model: str,
        dim: int,
        limits: VoyageLimits,
        *,
        cache: EmbeddingCache | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.model = model
        self.dim = dim
        self.tokens_used = 0
        self._client = client
        self._limits = limits
        self._batch_size = min(limits.batch_size, self.MAX_BATCH_TEXTS)
        self._cache = cache
        self._sleep = sleep
        self._limiter = RateLimiter(limits.requests_per_minute, limits.tokens_per_minute, clock=clock, sleep=sleep)
        self._warned_tokenizer = False

    @classmethod
    def from_api_key(cls, api_key: str, timeout_seconds: float, **kwargs: Any) -> "VoyageEmbedder":
        # Retries are handled here (with rate-limit awareness), so the SDK's own retries are off.
        return cls(VoyageClient(api_key=api_key, max_retries=0, timeout=timeout_seconds), **kwargs)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        results: list[list[float] | None] = [self._cache.get(t) if self._cache else None for t in texts]
        todo = [i for i, v in enumerate(results) if v is None]
        if self._cache and len(todo) < len(texts):
            log.info("embedding.cache_hits", reused=len(texts) - len(todo), to_embed=len(todo))

        if todo:
            pending = [texts[i] for i in todo]
            counts = self.count_tokens(pending)
            done = 0
            for start, end in self._batches(counts):
                for vector in self._embed_batch(pending[start:end], counts[start:end]):
                    results[todo[done]] = vector
                    done += 1
                log.info(
                    "embedding.progress",
                    done=done,
                    total=len(todo),
                    eta_minutes=round(sum(counts[done:]) / (0.9 * self._limiter.tpm), 1),
                )
        return [v for v in results if v is not None]

    def count_tokens(self, texts: Sequence[str]) -> list[int]:
        """Exact counts from Voyage's tokenizer (runs locally, no API usage); estimate if unavailable."""
        try:
            return [len(enc.ids) for enc in self._client.tokenize(list(texts), model=self.model)]
        except Exception as exc:  # tokenizer download blocked, test double without a tokenizer, ...
            if not self._warned_tokenizer:
                log.warning(
                    "embedding.tokenizer_unavailable", error=str(exc), fallback_factor=self.FALLBACK_TOKEN_FACTOR
                )
                self._warned_tokenizer = True
            return [math.ceil(estimate_tokens(t) * self.FALLBACK_TOKEN_FACTOR) for t in texts]

    def _batches(self, counts: Sequence[int]) -> Iterator[tuple[int, int]]:
        """(start, end) ranges fitting the per-request budget; evaluated lazily, so each batch is
        sized from the limiter's current (adaptive) budget."""
        start = 0
        while start < len(counts):
            budget = min(self.MAX_BATCH_TOKENS, int(self._limiter.budget * self.BATCH_SHARE_OF_BUDGET))
            end, tokens = start, 0
            while end < len(counts) and end - start < self._batch_size:
                if end > start and tokens + counts[end] > budget:
                    break
                tokens += counts[end]
                end += 1
            yield start, end
            start = end

    def _embed_batch(self, batch: Sequence[str], counts: Sequence[int]) -> list[list[float]]:
        """Embed one batch. On a rate-limit refusal, back off and retry the batch in two halves."""
        rate_limited = transient = 0
        while True:
            self._limiter.acquire(sum(counts))
            try:
                result = self._client.embed(
                    list(batch),
                    model=self.model,
                    input_type="document",
                    output_dimension=self.dim,
                    truncation=False,  # fail loudly rather than silently embed half a chunk
                )
            except voyage_errors.RateLimitError as exc:
                rate_limited += 1
                if rate_limited > self._limits.max_rate_limited_retries:
                    raise EmbeddingError(f"still rate limited after {rate_limited - 1} back-offs: {exc}") from exc
                wait = min(self.MAX_BACKOFF_SECONDS, 60.0 * 2 ** (rate_limited - 1))
                self._limiter.penalise(wait)
                log.warning("embedding.rate_limited", texts=len(batch), tokens=sum(counts), wait_seconds=wait)
                if len(batch) > 1:
                    mid = len(batch) // 2
                    return self._embed_batch(batch[:mid], counts[:mid]) + self._embed_batch(batch[mid:], counts[mid:])
                continue
            except _TRANSIENT_ERRORS as exc:
                transient += 1
                if transient > self._limits.max_transient_retries:
                    raise EmbeddingError(f"Voyage unavailable after {transient - 1} retries: {exc}") from exc
                wait = min(self.MAX_TRANSIENT_BACKOFF_SECONDS, 2.0**transient)
                log.warning("embedding.transient_error", attempt=transient, wait_seconds=wait, error=str(exc))
                self._sleep(wait)
                continue
            except voyage_errors.VoyageError as exc:  # bad key, invalid request: retrying will not help
                raise EmbeddingError(f"Voyage rejected the request: {exc}") from exc

            if len(result.embeddings) != len(batch):
                raise EmbeddingError(f"expected {len(batch)} embeddings, got {len(result.embeddings)}")
            self._limiter.record(result.total_tokens)
            self.tokens_used += result.total_tokens
            if self._cache:
                self._cache.add_many(batch, result.embeddings)  # saved at once, even for half of a split batch
            return list(result.embeddings)
