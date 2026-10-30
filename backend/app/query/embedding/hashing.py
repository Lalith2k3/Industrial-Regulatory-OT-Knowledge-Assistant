from collections.abc import Sequence

from app.ingestion.embedding import LOCAL_HASH_MODEL, HashEmbedder


class HashQueryEmbedder:
    """Offline counterpart of the ingestion HashEmbedder (development and tests only).

    The hashing embedder has no query/document distinction, so questions are embedded exactly like
    chunks; vectors match only on shared words.
    """

    model = LOCAL_HASH_MODEL

    def __init__(self, dim: int):
        self.dim = dim
        self._embedder = HashEmbedder(dim)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embedder.embed_documents(texts)
