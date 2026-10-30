import hashlib
import math
import re
from collections.abc import Sequence

LOCAL_HASH_MODEL = "local-hash"


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
