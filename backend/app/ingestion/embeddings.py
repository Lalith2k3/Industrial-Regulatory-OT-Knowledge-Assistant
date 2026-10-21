"""Embedding providers.

Documents are embedded with input_type="document"; the query side must use the same model and
dimension with input_type="query".

Voyage accounts without a payment method are limited to 3 requests and 10,000 tokens per minute
(the 200M free tokens apply either way). VoyageEmbedder therefore paces itself to the configured
limits, sizes batches to fit them, waits out rate-limit refusals instead of failing, and writes
every finished batch to an on-disk cache so an interrupted run never pays for the same text twice.
"""

import hashlib
import json
import math
import re
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

import structlog
import voyageai
import voyageai.error as voyage_errors

from app.core.config import Settings
from app.ingestion.errors import EmbeddingError
from app.ingestion.text import estimate_tokens

LOCAL_HASH_MODEL = "local-hash"
log = structlog.get_logger("ingestion.embeddings")


class Embedder(Protocol):
    model: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


class RateLimiter:
    """Sliding 60-second window over requests and tokens, with even spacing and an adaptive budget.

    Providers enforce limits more strictly than the nominal numbers suggest (observed on a Voyage
    3 RPM / 10K TPM account: about 5K tokens per minute got through). So requests are spaced at
    least 60/rpm seconds apart, and the working token budget adapts: each refusal lowers it by 25 %
    (never below 20 % of nominal), each accepted request raises it by 5 % (never above nominal).
    """

    WINDOW = 60.0

    def __init__(
        self,
        requests_per_minute: int,
        tokens_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rpm = requests_per_minute
        self.tpm = tokens_per_minute
        self.budget = float(tokens_per_minute)
        self.min_interval = self.WINDOW / requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._events: deque[tuple[float, int]] = deque()
        self._last_request = -math.inf

    def acquire(self, tokens: int) -> None:
        """Block until a request of `tokens` fits the window, the budget and the spacing."""
        while True:
            now = self._clock()
            while self._events and now - self._events[0][0] >= self.WINDOW:
                self._events.popleft()
            used = sum(t for _, t in self._events)
            fits = not self._events or (len(self._events) < self.rpm and used + tokens <= self.budget)
            spacing = self._last_request + self.min_interval - now
            if fits and spacing <= 0:
                self._last_request = now
                return
            wait = spacing if fits else max(spacing, self._events[0][0] + self.WINDOW - now)
            self._sleep(max(0.5, wait))

    def record(self, tokens: int) -> None:
        self._events.append((self._clock(), tokens))
        self.budget = min(float(self.tpm), self.budget * 1.05)

    def penalise(self, seconds: float = WINDOW) -> None:
        """After a 429: block all requests for `seconds` and lower the working budget."""
        self._events.clear()
        self._events.append((self._clock() + seconds - self.WINDOW, self.tpm))
        self.budget = max(0.2 * self.tpm, self.budget * 0.75)


class EmbeddingCache:
    """Append-only JSONL cache: sha256(text) -> vector, one file per model and dimension."""

    def __init__(self, path: Path):
        self.path = path
        self._vectors: dict[str, list[float]] = {}
        if path.is_file():
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                        self._vectors[row["sha"]] = row["vector"]
                    except (json.JSONDecodeError, KeyError):
                        continue  # a line cut short by an interrupted run

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, text: str) -> list[float] | None:
        return self._vectors.get(self.key(text))

    def add_many(self, texts: Sequence[str], vectors: Sequence[list[float]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for text, vector in zip(texts, vectors, strict=True):
                sha = self.key(text)
                self._vectors[sha] = vector
                fh.write(json.dumps({"sha": sha, "vector": vector}) + "\n")


class VoyageEmbedder:
    MAX_BATCH_TEXTS = 1000
    MAX_BATCH_TOKENS = 100_000  # API cap for voyage-4-large is 120k per request
    # A request uses at most this share of the per-minute token budget. Voyage enforces its limit
    # more strictly than "one full minute's worth per minute" (observed: ~9k-token requests at
    # 60 s intervals were refused indefinitely on a 10k TPM account), so stay well below it.
    BATCH_SHARE_OF_TPM = 0.5
    MAX_RATE_LIMITED = 8  # backoff 1, 2, 4, 5, 5... minutes: about half an hour before giving up
    MAX_TRANSIENT = 6
    FALLBACK_TOKEN_FACTOR = 1.3  # if Voyage's tokenizer cannot be loaded; observed ratio is about 1.15

    def __init__(
        self,
        api_key: str,
        model: str,
        dim: int,
        *,
        requests_per_minute: int,
        tokens_per_minute: int,
        batch_size: int = 64,
        cache: EmbeddingCache | None = None,
        client=None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.model = model
        self.dim = dim
        self.batch_size = min(batch_size, self.MAX_BATCH_TEXTS)
        self.tokens_used = 0
        self._cache = cache
        self._sleep = sleep
        self._limiter = RateLimiter(requests_per_minute, tokens_per_minute, sleep=sleep)
        self._client = client or voyageai.Client(api_key=api_key, max_retries=0, timeout=120)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        results: list[list[float] | None] = [self._cache.get(t) if self._cache else None for t in texts]
        todo = [i for i, v in enumerate(results) if v is None]
        if self._cache and len(todo) < len(texts):
            log.info("embedding.cache_hits", reused=len(texts) - len(todo), to_embed=len(todo))
        if not todo:
            return [v for v in results if v is not None]

        pending = [texts[i] for i in todo]
        counts = self.count_tokens(pending)
        done = 0
        for start, end in self._batches(counts):
            vectors = self._embed_batch(pending[start:end], counts[start:end])
            for vector in vectors:
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
        except Exception:  # tokenizer download blocked, fake client in tests, ...
            return [math.ceil(estimate_tokens(t) * self.FALLBACK_TOKEN_FACTOR) for t in texts]

    def _batches(self, counts: Sequence[int]):
        """(start, end) index ranges whose token total fits the per-request budget.

        Evaluated lazily, so each batch is sized from the limiter's current (adaptive) budget.
        """
        start = 0
        while start < len(counts):
            budget = min(self.MAX_BATCH_TOKENS, int(self._limiter.budget * self.BATCH_SHARE_OF_TPM))
            end, tokens = start, 0
            while end < len(counts) and end - start < self.batch_size:
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
                if rate_limited > self.MAX_RATE_LIMITED:
                    raise EmbeddingError(f"still rate limited after {self.MAX_RATE_LIMITED} back-offs: {exc}") from exc
                wait = min(300, 60 * 2 ** (rate_limited - 1))
                self._limiter.penalise(wait)
                log.warning("embedding.rate_limited", texts=len(batch), tokens=sum(counts), wait_seconds=wait)
                if len(batch) > 1:
                    mid = len(batch) // 2
                    return self._embed_batch(batch[:mid], counts[:mid]) + self._embed_batch(batch[mid:], counts[mid:])
                continue
            except (
                voyage_errors.ServiceUnavailableError,
                voyage_errors.ServerError,
                voyage_errors.APIConnectionError,
                voyage_errors.Timeout,
                voyage_errors.TryAgain,
            ) as exc:
                transient += 1
                if transient > self.MAX_TRANSIENT:
                    raise EmbeddingError(f"Voyage unavailable after {self.MAX_TRANSIENT} attempts: {exc}") from exc
                wait = min(60, 2**transient)
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
            return result.embeddings


class HashEmbedder:
    """Deterministic bag-of-words hashing embedder.

    For offline development and tests only: it lets the full pipeline (including the database)
    run without an API key, but its vectors carry no semantic meaning.
    """

    model = LOCAL_HASH_MODEL

    def __init__(self, dim: int):
        self.dim = dim

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in re.findall(r"\w+", text.lower()):
            h = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_model == LOCAL_HASH_MODEL:
        return HashEmbedder(settings.embedding_dim)
    if settings.voyage_api_key is None:
        raise EmbeddingError(
            f"embedding model {settings.embedding_model!r} needs RAG_VOYAGE_API_KEY (or VOYAGE_API_KEY); "
            f"set RAG_EMBEDDING_MODEL={LOCAL_HASH_MODEL} for an offline development run"
        )
    cache_file = settings.processed_dir / ".embedding-cache" / f"{settings.embedding_model}-{settings.embedding_dim}.jsonl"
    return VoyageEmbedder(
        api_key=settings.voyage_api_key.get_secret_value(),
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        requests_per_minute=settings.embedding_requests_per_minute,
        tokens_per_minute=settings.embedding_tokens_per_minute,
        batch_size=settings.embedding_batch_size,
        cache=EmbeddingCache(cache_file),
    )
