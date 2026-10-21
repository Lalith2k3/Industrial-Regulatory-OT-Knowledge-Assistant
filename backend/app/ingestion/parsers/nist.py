"""Parser for NIST Special Publications (built against SP 800-82r3).

The PDF bookmarks give the full section hierarchy with exact positions, so sections are cut at
bookmark destinations rather than guessed from fonts. Special handling, driven by source options:

- `exclude`: top-level sections that are not knowledge (References, Change Log).
- `acronyms`: parsed into a lookup dictionary for query expansion instead of being indexed.
- `glossary`: one Section per defined term.
- `controls`: overlay sections split into one Section per security control (AC-17, SC-7, ...).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from app.ingestion.errors import ParseError
from app.ingestion.models import Paragraph, ParsedDocument, Section, SourceSpec
from app.ingestion.parsers.pdf_layout import (
    Line,
    Margins,
    PageText,
    group_paragraphs,
    merge_rows,
    read_pages,
    typical_line_step,
)
from app.ingestion.text import build_vocabulary, join_lines, normalize

_NUMBERED = re.compile(r"^(?:Appendix\s+)?([A-Z](?:\.\d+)*|\d+(?:\.\d+)*)\.\s+(.*)$")
_CONTROL_HEAD = re.compile(r"^([A-Z]{2}-\d{1,2})\s+([A-Z][A-Z0-9 ,/&()'\-–]+)$")


@dataclass
class _Entry:
    level: int
    title: str
    page: int
    y: float
    key: str
    number: str | None
    path: list[str]
    top: str
    lines: list[tuple[Line, str | None]] = field(default_factory=list)
    has_children: bool = False


class NistSpParser:
    name = "nist_sp_pdf"
    version = "1.1.0"

    def parse(self, source: SourceSpec, path: Path) -> ParsedDocument:
        opts = source.options
        exclude = tuple(opts.get("exclude", []))
        acronyms_prefix = opts.get("acronyms")
        glossary_prefix = opts.get("glossary")
        controls_prefix = opts.get("controls")

        entries = _toc_entries(path)
        if not entries:
            raise ParseError(f"{source.id}: PDF has no bookmarks; the NIST parser needs them")
        pages = read_pages(path, Margins(header=opts.get("header", 60), footer=opts.get("footer", 735)), tables=True)
        for page in pages:
            merge_rows(page)
        step = typical_line_step(pages)
        vocab = build_vocabulary(ln.text for p in pages for ln in p.lines if not ln.is_table)
        _assign_lines(entries, pages)

        sections: list[Section] = []
        acronyms: dict[str, str] = {}
        expected: list[str] = []
        excluded: list[str] = []
        warnings: list[str] = []

        for entry in entries:
            if exclude and entry.top.startswith(exclude):
                if entry.level == 1:
                    excluded.append(entry.title)
                continue
            lines = _strip_heading(entry)
            if acronyms_prefix and entry.top.startswith(acronyms_prefix):
                for t in _term_list(lines):
                    acronyms.setdefault(t.term, join_lines(t.definition))
                continue
            if not entry.has_children:
                expected.append(entry.key)
            if not lines:
                continue

            citation = _citation(source.short_title, entry)
            if glossary_prefix and entry.top.startswith(glossary_prefix):
                seen_terms: set[str] = set()
                for t in _term_list(lines):
                    if _slug(t.term) in seen_terms:
                        warnings.append(f"duplicate glossary term {t.term!r} skipped")
                        continue
                    seen_terms.add(_slug(t.term))
                    definition = join_lines(t.definition, vocab)
                    sections.append(
                        Section(
                            key=f"glossary/{_slug(t.term)}",
                            kind="glossary_term",
                            heading=t.term,
                            path=[*entry.path, t.term],
                            citation=f'{citation} - "{t.term}"',
                            paragraphs=[Paragraph(text=f"{t.term}: {definition}", page=t.first.page, page_label=t.page_label)],
                            source_url=source.source_url,
                            metadata={"unit": entry.key, "term": t.term},
                        )
                    )
                continue

            if controls_prefix and entry.number and entry.number.startswith(controls_prefix) and entry.level >= 3:
                for control_id, name, part in _split_controls(lines):
                    blocks = group_paragraphs(part, step)
                    if not blocks:
                        continue
                    heading = f"{control_id} {name.title()}" if control_id else entry.title
                    sections.append(
                        Section(
                            key=f"{entry.key}/{control_id or 'intro'}",
                            kind="control" if control_id else "section",
                            heading=heading,
                            path=[*entry.path, heading] if control_id else entry.path,
                            citation=f"{citation}, {control_id} ({name.title()})" if control_id else citation,
                            paragraphs=[b.to_paragraph(vocab) for b in blocks],
                            source_url=source.source_url,
                            metadata={"unit": entry.key, "control_id": control_id, "control_family": entry.title},
                        )
                    )
                continue

            blocks = group_paragraphs(lines, step)
            sections.append(
                Section(
                    key=entry.key,
                    kind="section",
                    heading=entry.title,
                    path=entry.path,
                    citation=citation,
                    paragraphs=[b.to_paragraph(vocab) for b in blocks],
                    source_url=source.source_url,
                    metadata={"unit": entry.key, "section_number": entry.number},
                )
            )

        if acronyms_prefix and not acronyms:
            warnings.append("no acronyms parsed")
        return ParsedDocument(
            source_id=source.id,
            parser=self.name,
            parser_version=self.version,
            sections=sections,
            dictionaries={"acronyms": acronyms} if acronyms else {},
            expected_keys=expected,
            warnings=warnings,
            metadata={"excluded_sections": excluded, "toc_entries": len(entries)},
        )


def _toc_entries(path: Path) -> list[_Entry]:
    with pymupdf.open(path) as doc:
        toc = doc.get_toc(simple=False)
    entries: list[_Entry] = []
    stack: list[str] = []
    seen: dict[str, int] = {}
    for level, raw_title, page, dest in toc:
        title = normalize(raw_title)
        stack = [*stack[: level - 1], title]
        m = _NUMBERED.match(title)
        number = m.group(1) if m else None
        key = number or _slug(title)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            key = f"{key}-{seen[key]}"
        to = dest.get("to") if isinstance(dest, dict) else None
        entries.append(
            _Entry(level=level, title=title, page=page, y=float(to.y) if to is not None else 0.0, key=key,
                   number=number, path=list(stack), top=stack[0])
        )
    for parent, child in zip(entries, entries[1:]):
        parent.has_children = child.level > parent.level
    return entries


def _assign_lines(entries: list[_Entry], pages: list[PageText]) -> None:
    """Give every body line to the last bookmark whose destination precedes it."""
    order = sorted(range(len(entries)), key=lambda i: (entries[i].page, entries[i].y, i))
    cursor = -1
    for page in pages:
        for line in page.lines:
            while cursor + 1 < len(order):
                nxt = entries[order[cursor + 1]]
                if (nxt.page, nxt.y) <= (line.page, line.y0 + 2):
                    cursor += 1
                else:
                    break
            if cursor >= 0:
                entries[order[cursor]].lines.append((line, page.label))


def _strip_heading(entry: _Entry) -> list[tuple[Line, str | None]]:
    """Drop the heading line(s) that repeat the bookmark title."""
    title = _squash(entry.title)
    lines = list(entry.lines)
    while lines and not lines[0][0].is_table and _squash(lines[0][0].text) and _squash(lines[0][0].text) in title:
        lines.pop(0)
    return lines


@dataclass
class _Term:
    term: str
    first: Line
    page_label: str | None
    definition: list[str] = field(default_factory=list)


def _term_list(lines: list[tuple[Line, str | None]]) -> list[_Term]:
    """Bold term followed by its definition lines (acronym lists and glossaries).

    A bold line that directly follows another bold line continues a wrapped term.
    """
    terms: list[_Term] = []
    for line, label in lines:
        if line.is_table:
            continue
        if line.bold:
            if terms and not terms[-1].definition:
                terms[-1].term = f"{terms[-1].term} {line.text}"
            else:
                terms.append(_Term(term=line.text, first=line, page_label=label))
        elif terms:
            terms[-1].definition.append(line.text)
    return [t for t in terms if t.definition]


def _split_controls(lines: list[tuple[Line, str | None]]) -> list[tuple[str | None, str, list]]:
    parts: list[tuple[str | None, str, list]] = [(None, "", [])]
    for line, label in lines:
        m = _CONTROL_HEAD.match(line.text) if line.bold and not line.is_table else None
        if m:
            parts.append((m.group(1), m.group(2).strip(), []))
        else:
            parts[-1][2].append((line, label))
    return [p for p in parts if p[2]]


def _citation(short_title: str, entry: _Entry) -> str:
    if entry.number:
        label = "Appendix" if entry.number[0].isalpha() else "Section"
        rest = _NUMBERED.match(entry.title)
        name = rest.group(2) if rest else entry.title
        return f"{short_title}, {label} {entry.number} ({name})"
    return f"{short_title}, {entry.title}"


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
