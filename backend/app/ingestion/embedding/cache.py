import hashlib
import json
from collections.abc import Sequence
from pathlib import Path


class EmbeddingCache:
    """Append-only JSONL cache: sha256(text) -> vector, one file per model and dimension.

    Every accepted batch is appended immediately, so an interrupted run never pays for the same
    text twice; a line cut short by a crash is skipped on the next load.
    """

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
                        continue

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
