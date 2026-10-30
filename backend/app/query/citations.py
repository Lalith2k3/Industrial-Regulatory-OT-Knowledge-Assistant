import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.query.domain.models import Citation, ContextSource, KnowledgeChunk

# "[3]", "[1, 4]", "[2,3,5]" - the forms the answer prompt asks for.
_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


@dataclass(frozen=True)
class ResolvedCitations:
    citations: list[Citation]  # in order of first mention
    unknown: list[int]  # numbers the answer used that match no source (logged, never shown)


class CitationResolver:
    """Maps the [n] markers in an answer back to the passages they refer to (FR-04, FR-05)."""

    def resolve(self, text: str, sources: Sequence[ContextSource]) -> ResolvedCitations:
        by_number = {s.number: s.chunk for s in sources}
        mentioned = dict.fromkeys(int(n) for group in _MARKER.findall(text) for n in group.split(","))
        return ResolvedCitations(
            citations=[_citation(n, by_number[n]) for n in mentioned if n in by_number],
            unknown=[n for n in mentioned if n not in by_number],
        )

    def describe(self, sources: Sequence[ContextSource]) -> list[Citation]:
        """Every source the answer may cite, so a client can show [n] while the answer streams."""
        return [_citation(s.number, s.chunk) for s in sources]


def _citation(number: int, chunk: KnowledgeChunk) -> Citation:
    return Citation(
        number=number,
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        document_title=chunk.document_title,
        document_version=chunk.document_version,
        citation=chunk.citation,
        heading=chunk.heading,
        kind=chunk.kind,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        page_label_start=chunk.page_label_start,
        page_label_end=chunk.page_label_end,
        url=source_link(chunk),
        text=chunk.text,
    )


def source_link(chunk: KnowledgeChunk) -> str | None:
    """Open the source at the right place: PDF viewers honour #page=N (1-based physical page)."""
    url = chunk.source_url
    if url and chunk.page_start and url.lower().endswith(".pdf"):
        return f"{url}#page={chunk.page_start}"
    return url
