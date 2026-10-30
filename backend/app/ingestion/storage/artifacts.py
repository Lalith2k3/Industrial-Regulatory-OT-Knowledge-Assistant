import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.ingestion.domain.models import Chunk, ParsedDocument


class FileArtifactSink:
    """Writes data/processed/<source>/{sections.json, chunks.jsonl, quality.json, <dictionary>.json}."""

    def __init__(self, base_dir: Path):
        self._base_dir = base_dir

    def write(self, parsed: ParsedDocument, chunks: Sequence[Chunk], quality: dict[str, Any]) -> None:
        out = self._base_dir / parsed.source_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "sections.json").write_text(parsed.model_dump_json(indent=2), encoding="utf-8")
        with (out / "chunks.jsonl").open("w", encoding="utf-8") as fh:
            for chunk in chunks:
                fh.write(chunk.model_dump_json() + "\n")
        (out / "quality.json").write_text(json.dumps(quality, indent=2), encoding="utf-8")
        for name, table in parsed.dictionaries.items():
            (out / f"{name}.json").write_text(json.dumps(table, indent=2, ensure_ascii=False), encoding="utf-8")
