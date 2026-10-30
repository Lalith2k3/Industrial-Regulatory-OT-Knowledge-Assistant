"""Structure-aware chunking.

Parsers already cut documents at their natural citation units (a CFR section, one FAQ, one
control). The chunker keeps a unit whole when it fits and otherwise packs its paragraphs into
chunks, preferring to break where a new top-level paragraph ("(c)") begins. Every chunk gets a
contextual header (document + section path) prepended for embedding only.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from app.ingestion.chunking.policy import ChunkPolicy
from app.ingestion.domain.models import Chunk, DocType, Paragraph, ParsedDocument, Section, SourceSpec
from app.ingestion.references import ReferenceExtractor
from app.ingestion.text import estimate_tokens, split_sentences, tail_tokens

# Bump whenever chunk output changes: stored documents with another version are re-processed.
CHUNKER_VERSION = "1.0.0"
_CHUNK_ID_LENGTH = 24


@dataclass
class _Unit:
    paragraph: Paragraph
    tokens: int
    overlap: bool = False


class Chunker:
    version = CHUNKER_VERSION

    def __init__(
        self,
        policies: Mapping[DocType, ChunkPolicy],
        cfr_refs: ReferenceExtractor,
        control_refs: ReferenceExtractor,
    ):
        self._policies = dict(policies)
        self._cfr_refs = cfr_refs
        self._control_refs = control_refs

    def policy_for(self, source: SourceSpec) -> ChunkPolicy:
        return self._policies[source.doc_type]

    def chunk(self, source: SourceSpec, parsed: ParsedDocument) -> list[Chunk]:
        policy = self.policy_for(source)
        chunks: list[Chunk] = []
        for section in parsed.sections:
            groups = _pack(section, policy)
            for part, units in enumerate(groups):
                chunks.append(self._build(source, section, units, part, len(groups), len(chunks)))
        return chunks

    def _build(
        self, source: SourceSpec, section: Section, units: list[_Unit], part: int, parts: int, ordinal: int
    ) -> Chunk:
        paragraphs = [u.paragraph for u in units]
        text = "\n\n".join(p.text for p in paragraphs)
        header = [f"{source.short_title} — {source.title}", " > ".join(section.path)]
        embed_text = "\n".join(h for h in header if h) + "\n\n" + text
        first_page, first_label, last_page, last_label = _page_span(paragraphs)

        cfr_refs = self._cfr_refs.extract(text, exclude=section.own_cfr_ref)
        control_refs = self._control_refs.extract(text)
        return Chunk(
            id=hashlib.sha256(f"{source.id}|{section.key}|{part}".encode()).hexdigest()[:_CHUNK_ID_LENGTH],
            document_id=source.id,
            ordinal=ordinal,
            kind=section.kind,
            section_key=section.key,
            citation=_citation(section, paragraphs, parts),
            heading=section.heading,
            section_path=section.path,
            text=text,
            embed_text=embed_text,
            token_count=estimate_tokens(text),
            page_start=first_page,
            page_end=last_page,
            page_label_start=first_label,
            page_label_end=last_label,
            source_url=section.source_url,
            cfr_refs=self._cfr_refs.extract(" ".join([*cfr_refs, *section.cfr_refs])),
            control_refs=self._control_refs.extract(" ".join([*control_refs, *section.control_refs])),
            metadata={**section.metadata, "part": part + 1, "parts": parts},
            content_sha256=hashlib.sha256(embed_text.encode()).hexdigest(),
        )


def _pack(section: Section, policy: ChunkPolicy) -> list[list[_Unit]]:
    units = [_Unit(p, estimate_tokens(p.text)) for p in section.paragraphs if p.text.strip()]
    if not units:
        return []
    if sum(u.tokens for u in units) <= policy.max_tokens:
        return [units]

    split: list[_Unit] = []
    for u in units:
        split.extend(_split_oversized(u, policy) if u.tokens > policy.max_tokens else [u])

    groups: list[list[_Unit]] = []
    current: list[_Unit] = []
    size = 0
    for u in split:
        if current and not all(c.overlap for c in current):
            full = size >= policy.target_tokens or size + u.tokens > policy.max_tokens
            natural_break = u.paragraph.starts_unit and size >= policy.target_tokens // 2
            if full or natural_break:
                groups.append(current)
                current = _overlap_seed(current, policy)
                size = sum(c.tokens for c in current)
        current.append(u)
        size += u.tokens
    if current and not all(c.overlap for c in current):
        groups.append(current)
    return groups


def _split_oversized(unit: _Unit, policy: ChunkPolicy) -> list[_Unit]:
    p = unit.paragraph
    pieces = _split_table(p.text, policy) if p.is_table else _split_prose(p.text, policy)
    return [
        _Unit(p.model_copy(update={"text": text, "starts_unit": p.starts_unit and i == 0}), estimate_tokens(text))
        for i, text in enumerate(pieces)
    ]


def _split_table(text: str, policy: ChunkPolicy) -> list[str]:
    """Split by rows; every piece repeats the caption and header row so it reads on its own."""
    lines = text.split("\n")
    n_header = 2 if lines[0].startswith("Table:") and len(lines) > 2 else 1
    header, rows = lines[:n_header], lines[n_header:]
    pieces: list[str] = []
    current: list[str] = []
    for row in rows:
        if current and estimate_tokens("\n".join([*header, *current, row])) > policy.target_tokens:
            pieces.append("\n".join(header + current))
            current = []
        current.append(row)
    if current:
        pieces.append("\n".join(header + current))
    return pieces


def _split_prose(text: str, policy: ChunkPolicy) -> list[str]:
    pieces: list[str] = []
    current = ""
    for sentence in split_sentences(text):
        candidate = f"{current} {sentence}".strip()
        if current and estimate_tokens(candidate) > policy.target_tokens:
            pieces.append(current)
            candidate = sentence
        current = candidate
    if current:
        pieces.append(current)
    return pieces


def _overlap_seed(previous: list[_Unit], policy: ChunkPolicy) -> list[_Unit]:
    if policy.overlap_tokens <= 0:
        return []
    last = previous[-1].paragraph
    if last.is_table:
        return []
    tail = tail_tokens(last.text, policy.overlap_tokens)
    if not tail:
        return []
    seed = last.model_copy(
        update={
            "text": tail,
            "starts_unit": False,
            "page": last.page_end or last.page,
            "page_label": last.page_label_end or last.page_label,
            "page_end": None,
            "page_label_end": None,
        }
    )
    return [_Unit(seed, estimate_tokens(tail), overlap=True)]


def _page_span(paragraphs: list[Paragraph]) -> tuple[int | None, str | None, int | None, str | None]:
    starts = [(p.page, p.page_label) for p in paragraphs if p.page is not None]
    ends = [(p.page_end or p.page, p.page_label_end or p.page_label) for p in paragraphs if p.page is not None]
    if not starts:
        return None, None, None, None
    first = min(starts, key=lambda x: x[0] or 0)
    last = max(ends, key=lambda x: x[0] or 0)
    return first[0], first[1], last[0], last[1]


def _citation(section: Section, paragraphs: list[Paragraph], parts: int) -> str:
    """A split regulation section is cited by the paragraphs it covers: 49 CFR 195.452(c)–(f)."""
    if parts == 1 or not section.paragraph_citations:
        return section.citation
    markers = [p.top_marker for p in paragraphs if p.top_marker]
    if not markers:
        return section.citation
    first, last = markers[0], markers[-1]
    return f"{section.citation}({first})" if first == last else f"{section.citation}({first})–({last})"
