"""Quality gates run on every document before anything is written to the knowledge base.

Errors stop the document (the previous version stays live); warnings are reported.
"""

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.ingestion.chunking import ChunkPolicy
from app.ingestion.domain.models import Chunk, ParsedDocument


@dataclass
class QualityReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {"errors": self.errors, "warnings": self.warnings, "stats": self.stats}


@dataclass(frozen=True)
class QualityGate:
    min_coverage: float = 0.9  # share of expected units (TOC entries, sections) that must have content
    tiny_chunk_tokens: int = 12  # reported, not rejected: short definitions are legitimately tiny
    oversize_factor: float = 1.25  # chunks above policy.max_tokens x this are reported

    def check(self, parsed: ParsedDocument, chunks: list[Chunk], policy: ChunkPolicy) -> QualityReport:
        report = QualityReport(warnings=list(parsed.warnings))
        if not parsed.sections:
            report.errors.append("parser produced no sections")
        if not chunks:
            report.errors.append("chunker produced no chunks")
            return report

        self._check_integrity(chunks, report)
        self._check_coverage(parsed, report)
        if parsed.paginated:
            self._check_pages(chunks, report)
        self._check_sizes(chunks, policy, report)
        report.stats.update(_stats(parsed, chunks))
        return report

    @staticmethod
    def _check_integrity(chunks: list[Chunk], report: QualityReport) -> None:
        if dupes := [i for i, n in Counter(c.id for c in chunks).items() if n > 1]:
            report.errors.append(f"duplicate chunk ids: {dupes[:5]}")
        if empty := [c.section_key for c in chunks if not c.text.strip()]:
            report.errors.append(f"empty chunks: {empty[:5]}")

    def _check_coverage(self, parsed: ParsedDocument, report: QualityReport) -> None:
        if not parsed.expected_keys:
            return
        units = {s.metadata.get("unit") for s in parsed.sections}
        missing = [k for k in parsed.expected_keys if k not in units]
        coverage = 1 - len(missing) / len(parsed.expected_keys)
        report.stats["coverage"] = round(coverage, 4)
        if coverage < self.min_coverage:
            report.errors.append(f"coverage {coverage:.1%} below {self.min_coverage:.0%}; missing: {missing[:10]}")
        elif missing:
            report.warnings.append(f"{len(missing)} expected units without content: {missing[:10]}")

    @staticmethod
    def _check_pages(chunks: list[Chunk], report: QualityReport) -> None:
        if unpaged := [c.section_key for c in chunks if c.page_start is None]:
            report.errors.append(f"{len(unpaged)} chunks without page numbers (citations need them): {unpaged[:5]}")

    def _check_sizes(self, chunks: list[Chunk], policy: ChunkPolicy, report: QualityReport) -> None:
        limit = int(policy.max_tokens * self.oversize_factor)
        if oversized := [c.section_key for c in chunks if c.token_count > limit]:
            report.warnings.append(f"{len(oversized)} chunks above {limit} tokens: {oversized[:5]}")
        if tiny := [c.section_key for c in chunks if c.token_count < self.tiny_chunk_tokens]:
            report.warnings.append(f"{len(tiny)} chunks under {self.tiny_chunk_tokens} tokens: {tiny[:5]}")


def _stats(parsed: ParsedDocument, chunks: list[Chunk]) -> dict[str, Any]:
    tokens = sorted(c.token_count for c in chunks)
    return {
        "sections": len(parsed.sections),
        "chunks": len(chunks),
        "kinds": dict(Counter(c.kind.value for c in chunks)),
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
