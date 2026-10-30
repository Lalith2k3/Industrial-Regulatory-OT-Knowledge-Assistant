from types import SimpleNamespace

import pytest
import voyageai.error as voyage_errors

from app.ingestion.domain.errors import EmbeddingError
from app.ingestion.embedding import EmbeddingCache, RateLimiter, VoyageEmbedder, VoyageLimits
from app.ingestion.text import estimate_tokens


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeVoyage:
    """Counts tokens like the real API would (here: estimate x 1.2) and can fail on demand."""

    def __init__(self, failures=()):
        self.failures = list(failures)
        self.calls: list[list[str]] = []

    def embed(self, texts, model, input_type, output_dimension, truncation):
        assert input_type == "document" and truncation is False
        self.calls.append(list(texts))
        if self.failures:
            raise self.failures.pop(0)
        tokens = int(sum(estimate_tokens(t) for t in texts) * 1.2)
        return SimpleNamespace(embeddings=[[float(len(t))] * output_dimension for t in texts], total_tokens=tokens)


def _embedder(clock, client, cache=None, tpm=10_000, rpm=3):
    limits = VoyageLimits(
        requests_per_minute=rpm, tokens_per_minute=tpm, max_rate_limited_retries=8, max_transient_retries=6
    )
    return VoyageEmbedder(client, "voyage-4-large", 4, limits, cache=cache, clock=clock, sleep=clock.sleep)


def test_rate_limiter_waits_for_the_window_to_free_up():
    clock = FakeClock()
    limiter = RateLimiter(requests_per_minute=3, tokens_per_minute=10_000, clock=clock, sleep=clock.sleep)
    limiter.acquire(6_000)
    limiter.record(6_000)
    limiter.acquire(6_000)  # 12k > 10k: must wait a full minute
    assert clock.now == pytest.approx(60.0)


def test_rate_limiter_enforces_requests_per_minute():
    clock = FakeClock()
    limiter = RateLimiter(requests_per_minute=3, tokens_per_minute=1_000_000, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        limiter.acquire(10)
        limiter.record(10)
    limiter.acquire(10)
    assert clock.now == pytest.approx(60.0)


def test_requests_are_spaced_evenly():
    clock = FakeClock()
    limiter = RateLimiter(requests_per_minute=3, tokens_per_minute=1_000_000, clock=clock, sleep=clock.sleep)
    times = []
    for _ in range(3):
        limiter.acquire(10)
        limiter.record(10)
        times.append(clock.now)
    assert times == [0.0, 20.0, 40.0]


def test_refusals_shrink_the_budget_and_successes_restore_it():
    clock = FakeClock()
    limiter = RateLimiter(requests_per_minute=3, tokens_per_minute=10_000, clock=clock, sleep=clock.sleep)
    limiter.penalise()
    limiter.penalise()
    assert limiter.budget == pytest.approx(5_625)
    for _ in range(10):
        limiter.penalise()
    assert limiter.budget == pytest.approx(2_000)  # floor: 20 % of nominal
    for _ in range(100):
        limiter.record(1)
    assert limiter.budget == 10_000  # never above nominal


def test_free_tier_batches_use_at_most_half_the_minute_budget_and_are_paced():
    clock, client = FakeClock(), FakeVoyage()
    texts = [" ".join(["word"] * 500) + f" {i}" for i in range(60)]  # ~30k tokens in total
    vectors = _embedder(clock, client).embed_documents(texts)
    assert len(vectors) == 60 and vectors[0] == [float(len(texts[0]))] * 4
    assert all(sum(estimate_tokens(t) for t in batch) * 1.2 <= 5_000 for batch in client.calls)
    # ~36k real tokens at 10k/minute cannot finish in under 3 minutes
    assert clock.now >= 180


def test_exact_token_counts_come_from_the_voyage_tokenizer():
    class WithTokenizer(FakeVoyage):
        def tokenize(self, texts, model):
            return [SimpleNamespace(ids=[0] * 2_000) for _ in texts]  # pretend every text is 2,000 tokens

    client = WithTokenizer()
    _embedder(FakeClock(), client).embed_documents(["a", "b", "c", "d", "e"])
    # 5,000-token budget per request: two 2,000-token texts per batch
    assert [len(call) for call in client.calls] == [2, 2, 1]


def test_rate_limited_batch_is_split_in_half_after_backing_off():
    clock = FakeClock()
    client = FakeVoyage(failures=[voyage_errors.RateLimitError("reduced rate limits")])
    vectors = _embedder(clock, client).embed_documents(["one", "two", "three", "four"])
    assert len(vectors) == 4
    assert [len(call) for call in client.calls] == [4, 2, 2]


def test_rate_limit_refusal_waits_and_retries_instead_of_failing():
    clock = FakeClock()
    client = FakeVoyage(failures=[voyage_errors.RateLimitError("reduced rate limits of 3 RPM and 10K TPM")])
    vectors = _embedder(clock, client).embed_documents(["short text"])
    assert len(vectors) == 1 and len(client.calls) == 2
    assert clock.now >= 60


def test_bad_api_key_fails_fast():
    client = FakeVoyage(failures=[voyage_errors.AuthenticationError("invalid key")])
    with pytest.raises(EmbeddingError, match="rejected"):
        _embedder(FakeClock(), client).embed_documents(["text"])
    assert len(client.calls) == 1


def test_cache_survives_an_interrupted_run(tmp_path):
    path = tmp_path / "cache.jsonl"
    texts = [f"chunk number {i}" for i in range(5)]
    first = _embedder(FakeClock(), FakeVoyage(), cache=EmbeddingCache(path))
    first.embed_documents(texts[:3])  # "interrupted" after 3 chunks

    client = FakeVoyage()
    second = _embedder(FakeClock(), client, cache=EmbeddingCache(path))
    vectors = second.embed_documents(texts)
    assert len(vectors) == 5
    assert [t for call in client.calls for t in call] == texts[3:]  # only the missing ones are sent


def test_cache_ignores_a_truncated_last_line(tmp_path):
    path = tmp_path / "cache.jsonl"
    cache = EmbeddingCache(path)
    cache.add_many(["a"], [[1.0, 2.0]])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"sha": "abc", "vec')  # process killed mid-write
    assert EmbeddingCache(path).get("a") == [1.0, 2.0]
