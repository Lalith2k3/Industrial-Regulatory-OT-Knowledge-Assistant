from types import SimpleNamespace

import pytest
import voyageai.error as voyage_errors

from app.ingestion.embedding import EmbeddingCache, RateLimiter
from app.query.domain.errors import EmbeddingError
from app.query.embedding import VoyageQueryEmbedder


class FakeVoyage:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.calls: list[list[str]] = []

    def embed(self, texts, model, input_type, output_dimension, truncation):
        assert input_type == "query"  # chunks were embedded as "document"
        self.calls.append(list(texts))
        if self.failures:
            raise self.failures.pop(0)
        return SimpleNamespace(embeddings=[[float(len(t))] * output_dimension for t in texts], total_tokens=10)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _embedder(client, cache=None):
    clock = FakeClock()
    limiter = RateLimiter(1_000, 1_000_000, clock=clock, sleep=clock.sleep)
    embedder = VoyageQueryEmbedder(
        client, "voyage-4-large", 3, limiter, max_rate_limited_retries=2, max_transient_retries=2, cache=cache,
        sleep=clock.sleep,
    )  # fmt: skip
    return embedder, clock.slept


def test_queries_are_embedded_in_one_request_and_cached(tmp_path):
    cache_file = tmp_path / "query.jsonl"
    client = FakeVoyage()
    embedder, _ = _embedder(client, EmbeddingCache(cache_file))
    assert embedder.embed_queries(["ab", "abc"]) == [[2.0] * 3, [3.0] * 3]
    assert client.calls == [["ab", "abc"]]

    embedder, _ = _embedder(client, EmbeddingCache(cache_file))  # a new process reads the cache
    assert embedder.embed_queries(["abc", "abcd"]) == [[3.0] * 3, [4.0] * 3]
    assert client.calls[-1] == ["abcd"]


def test_transient_errors_are_retried_and_bad_requests_are_not():
    client = FakeVoyage(failures=[voyage_errors.ServiceUnavailableError("busy")])
    embedder, slept = _embedder(client)
    assert embedder.embed_queries(["q"]) == [[1.0] * 3]
    assert len(client.calls) == 2 and slept

    embedder, _ = _embedder(FakeVoyage(failures=[voyage_errors.AuthenticationError("invalid key")]))
    with pytest.raises(EmbeddingError, match="rejected"):
        embedder.embed_queries(["q"])


def test_persistent_rate_limiting_is_a_controlled_error():
    client = FakeVoyage(failures=[voyage_errors.RateLimitError("slow down")] * 3)
    embedder, _ = _embedder(client)
    with pytest.raises(EmbeddingError, match="rate limited"):
        embedder.embed_queries(["q"])
    assert len(client.calls) == 3
