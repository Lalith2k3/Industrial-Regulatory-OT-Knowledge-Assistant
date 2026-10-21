"""Structure-aware chunking.

Parsers already cut documents at their natural citation units (a CFR section, one FAQ, one
control). The chunker keeps a unit whole when it fits and otherwise packs its paragraphs into
chunks, preferring to break where a new top-level paragraph ("(c)") begins. Every chunk gets a
contextual header (document + section path) prepended for embedding only.
"""

import hashlib
from dataclasses import dataclass

from app.ingestion.models import Chunk, DocType, Paragraph, ParsedDocument, Section, SourceSpec
from app.ingestion.references import extract_cfr_refs, extract_control_refs
from app.ingestion.text import estimate_tokens, split_sentences, tail_tokens

CHUNKER_VERSION = "1.0.0"


@dataclass(frozen=True)
class ChunkPolicy:
    target_tokens: int  # pack paragraphs up to about this size
    max_tokens: int  # a whole section up to this size stays one chunk
    overlap_tokens: int = 0  # trailing context carried into the next chunk of the same section


POLICIES: dict[DocType, ChunkPolicy] = {
    # Regulations: sections are the citation unit; split only long ones, at (a)/(b) boundaries.
    DocType.REGULATION: ChunkPolicy(target_tokens=450, max_tokens=800),
    DocType.ENFORCEMENT_GUIDANCE: ChunkPolicy(target_tokens=400, max_tokens=700),
    DocType.FAQ: ChunkPolicy(target_tokens=600, max_tokens=1000),
    # Narrative prose: smaller chunks with a little overlap, never crossing a section boundary.
    DocType.STANDARD: ChunkPolicy(target_tokens=400, max_tokens=650, overlap_tokens=60),
}


@dataclass
class _Unit:
    paragraph: Paragraph
    tokens: int
    overlap: bool = False


def chunk_document(source: SourceSpec, parsed: ParsedDocument, policy: ChunkPolicy | None = None) -> list[Chunk]:
    policy = policy or POLICIES[source.doc_type]
    chunks: list[Chunk] = []
    for section in parsed.sections:
        groups = _pack(section, policy)
        for part, units in enumerate(groups):
            chunks.append(_build_chunk(source, section, units, part, len(groups), len(chunks)))
    return chunks


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
    if p.is_table:
        lines = p.text.split("\n")
        n_header = 2 if lines[0].startswith("Table:") and len(lines) > 2 else 1
        header, rows = lines[:n_header], lines[n_header:]
        pieces, current = [], []
        for row in rows:
            if current and estimate_tokens("\n".join(header + current + [row])) > policy.target_tokens:
                pieces.append("\n".join(header + current))
                current = []
            current.append(row)
        if current:
            pieces.append("\n".join(header + current))
    else:
        pieces, current = [], ""
        for sentence in split_sentences(p.text):
            candidate = f"{current} {sentence}".strip()
            if current and estimate_tokens(candidate) > policy.target_tokens:
                pieces.append(current)
                candidate = sentence
            current = candidate
        if current:
            pieces.append(current)
    return [
        _Unit(p.model_copy(update={"text": text, "starts_unit": p.starts_unit and i == 0}), estimate_tokens(text))
        for i, text in enumerate(pieces)
    ]


def _overlap_seed(previous: list[_Unit], policy: ChunkPolicy) -> list[_Unit]:
    if policy.overlap_tokens <= 0:
        return []
    last = previous[-1].paragraph
    if last.is_table:
        return []
    tail = tail_tokens(last.text, policy.overlap_tokens)
    if not tail:
        return []
    page = last.page_end or last.page
    label = last.page_label_end or last.page_label
    seed = last.model_copy(
        update={"text": tail, "starts_unit": False, "page": page, "page_label": label, "page_end": None, "page_label_end": None}
    )
    return [_Unit(seed, estimate_tokens(tail), overlap=True)]


def _build_chunk(source: SourceSpec, section: Section, units: list[_Unit], part: int, parts: int, ordinal: int) -> Chunk:
    paragraphs = [u.paragraph for u in units]
    text = "\n\n".join(p.text for p in paragraphs)
    header = [f"{source.short_title} — {source.title}", " > ".join(section.path)]
    embed_text = "\n".join(h for h in header if h) + "\n\n" + text

    starts = [(p.page, p.page_label) for p in paragraphs if p.page is not None]
    ends = [(p.page_end or p.page, p.page_label_end or p.page_label) for p in paragraphs if p.page is not None]
    first = min(starts, key=lambda x: x[0]) if starts else (None, None)
    last = max(ends, key=lambda x: x[0]) if ends else (None, None)

    own_section = section.metadata.get("section")
    cfr_refs = set(extract_cfr_refs(text, exclude=own_section))
    if section.metadata.get("cfr_section"):
        cfr_refs.add(section.metadata["cfr_section"])
    control_refs = set(extract_control_refs(text))
    if section.metadata.get("control_id"):
        control_refs.add(section.metadata["control_id"])

    return Chunk(
        id=hashlib.sha256(f"{source.id}|{section.key}|{part}".encode()).hexdigest()[:24],
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
        page_start=first[0],
        page_end=last[0],
        page_label_start=first[1],
        page_label_end=last[1],
        source_url=section.source_url,
        cfr_refs=extract_cfr_refs(" ".join(sorted(cfr_refs))),
        control_refs=extract_control_refs(" ".join(sorted(control_refs))),
        metadata={**section.metadata, "part": part + 1, "parts": parts},
        content_sha256=hashlib.sha256(embed_text.encode()).hexdigest(),
    )


def _citation(section: Section, paragraphs: list[Paragraph], parts: int) -> str:
    """A split regulation section is cited by the paragraphs it covers: 49 CFR 195.452(c)–(f)."""
    if parts == 1 or section.kind != "section":
        return section.citation
    markers = [p.top_marker for p in paragraphs if p.top_marker]
    if not markers:
        return section.citation
    first, last = markers[0], markers[-1]
    return f"{section.citation}({first})" if first == last else f"{section.citation}({first})–({last})"
