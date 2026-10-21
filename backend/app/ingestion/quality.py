"""Quality gates run on every document before anything is written to the knowledge base.

Errors stop the document (the previous version stays live); warnings are reported.
"""

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.ingestion.chunking import ChunkPolicy
from app.ingestion.models import Chunk, ParsedDocument, SourceSpec

MIN_COVERAGE = 0.9
TINY_CHUNK_TOKENS = 12


@dataclass
class QualityReport:
    source_id: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def check(source: SourceSpec, parsed: ParsedDocument, chunks: list[Chunk], policy: ChunkPolicy) -> QualityReport:
    report = QualityReport(source_id=source.id, warnings=list(parsed.warnings))

    if not parsed.sections:
        report.errors.append("parser produced no sections")
    if not chunks:
        report.errors.append("chunker produced no chunks")
        return report

    ids = Counter(c.id for c in chunks)
    if dupes := [i for i, n in ids.items() if n > 1]:
        report.errors.append(f"duplicate chunk ids: {dupes[:5]}")
    if empty := [c.section_key for c in chunks if not c.text.strip()]:
        report.errors.append(f"empty chunks: {empty[:5]}")

    if parsed.expected_keys:
        units = {s.metadata.get("unit") for s in parsed.sections}
        missing = [k for k in parsed.expected_keys if k not in units]
        coverage = 1 - len(missing) / len(parsed.expected_keys)
        report.stats["coverage"] = round(coverage, 4)
        if coverage < MIN_COVERAGE:
            report.errors.append(f"coverage {coverage:.1%} below {MIN_COVERAGE:.0%}; missing: {missing[:10]}")
        elif missing:
            report.warnings.append(f"{len(missing)} expected units without content: {missing[:10]}")

    if source.path.suffix.lower() == ".pdf":
        if unpaged := [c.section_key for c in chunks if c.page_start is None]:
            report.errors.append(f"{len(unpaged)} chunks without page numbers (citations need them): {unpaged[:5]}")

    tokens = sorted(c.token_count for c in chunks)
    oversized = [c.section_key for c in chunks if c.token_count > policy.max_tokens * 1.25]
    tiny = [c.section_key for c in chunks if c.token_count < TINY_CHUNK_TOKENS]
    if oversized:
        report.warnings.append(f"{len(oversized)} chunks above {int(policy.max_tokens * 1.25)} tokens: {oversized[:5]}")
    if tiny:
        report.warnings.append(f"{len(tiny)} chunks under {TINY_CHUNK_TOKENS} tokens: {tiny[:5]}")

    report.stats.update(
        {
            "sections": len(parsed.sections),
            "chunks": len(chunks),
            "kinds": dict(Counter(c.kind for c in chunks)),
            "tokens": {
                "total": sum(tokens),
                "min": tokens[0],
                "median": int(statistics.median(tokens)),
                "p95": tokens[int(len(tokens) * 0.95)],
                "max": tokens[-1],
            },
            "with_cfr_refs": sum(1 for c in chunks if c.cfr_refs),
            "with_control_refs": sum(1 for c in chunks if c.control_refs),
        }
    )
    return report
