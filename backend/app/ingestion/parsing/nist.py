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
from itertools import pairwise
from pathlib import Path
from typing import ClassVar

import pymupdf
from pydantic import Field

from app.ingestion.domain.errors import ParseError
from app.ingestion.domain.models import Paragraph, ParsedDocument, Section, SectionKind, SourceSpec
from app.ingestion.parsing.base import BaseParser, ParserOptions
from app.ingestion.parsing.pdf_layout import (
    Line,
    Margins,
    PageText,
    group_paragraphs,
    merge_rows,
    read_pages,
    typical_line_step,
)
from app.ingestion.text import build_vocabulary, join_lines, normalize, slugify, squash

_NUMBERED = re.compile(r"^(?:Appendix\s+)?([A-Z](?:\.\d+)*|\d+(?:\.\d+)*)\.\s+(.*)$")
_CONTROL_HEAD = re.compile(r"^([A-Z]{2}-\d{1,2})\s+([A-Z][A-Z0-9 ,/&()'\-–]+)$")
_BOOKMARK_TOLERANCE = 2.0  # points; a line this close below a bookmark destination belongs to it
_MIN_CONTROL_LEVEL = 3  # controls are listed under F.7.x family headings


class NistOptions(ParserOptions):
    header: float = 60.0  # lines starting above this y (points) are the running header
    footer: float = 735.0  # lines starting below this y are the footer (holds the printed page number)
    exclude: list[str] = Field(default_factory=list)  # top-level section title prefixes to skip
    acronyms: str | None = None  # top-level section parsed into the acronym dictionary
    glossary: str | None = None  # top-level section split into one Section per term
    controls: str | None = None  # section-number prefix of the control overlay, e.g. "F.7."


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


@dataclass
class _Term:
    term: str
    first: Line
    page_label: str | None
    definition: list[str] = field(default_factory=list)


@dataclass
class _Context:
    source: SourceSpec
    options: NistOptions
    line_step: float
    vocab: set[str]
    warnings: list[str] = field(default_factory=list)


class NistSpParser(BaseParser):
    name: ClassVar[str] = "nist_sp_pdf"
    version: ClassVar[str] = "1.1.0"
    Options = NistOptions

    def parse(self, source: SourceSpec, path: Path) -> ParsedDocument:
        options: NistOptions = self._options(source)
        entries = _toc_entries(path)
        if not entries:
            raise ParseError(f"{source.id}: PDF has no bookmarks; the NIST parser needs them")
        pages = [merge_rows(p) for p in read_pages(path, Margins(options.header, options.footer), tables=True)]
        _assign_lines(entries, pages)
        ctx = _Context(
            source=source,
            options=options,
            line_step=typical_line_step(pages),
            vocab=build_vocabulary(ln.text for p in pages for ln in p.lines if not ln.is_table),
        )

        sections: list[Section] = []
        acronyms: dict[str, str] = {}
        expected: list[str] = []
        excluded: list[str] = []
        for entry in entries:
            if options.exclude and entry.top.startswith(tuple(options.exclude)):
                if entry.level == 1:
                    excluded.append(entry.title)
                continue
            lines = _strip_heading(entry)
            if options.acronyms and entry.top.startswith(options.acronyms):
                for t in _term_list(lines):
                    acronyms.setdefault(t.term, join_lines(t.definition))
                continue
            if not entry.has_children:
                expected.append(entry.key)
            if lines:
                sections.extend(self._sections_for(entry, lines, ctx))

        if options.acronyms and not acronyms:
            ctx.warnings.append("no acronyms parsed")
        return ParsedDocument(
            source_id=source.id,
            parser=self.name,
            parser_version=self.version,
            sections=sections,
            paginated=True,
            dictionaries={"acronyms": acronyms} if acronyms else {},
            expected_keys=expected,
            warnings=ctx.warnings,
            metadata={"excluded_sections": excluded, "toc_entries": len(entries)},
        )

    def _sections_for(self, entry: _Entry, lines: list[tuple[Line, str | None]], ctx: _Context) -> list[Section]:
        if ctx.options.glossary and entry.top.startswith(ctx.options.glossary):
            return self._glossary_sections(entry, lines, ctx)
        if _is_control_family(entry, ctx.options.controls):
            return self._control_sections(entry, lines, ctx)
        return [self._plain_section(entry, lines, ctx)]

    def _plain_section(self, entry: _Entry, lines: list[tuple[Line, str | None]], ctx: _Context) -> Section:
        return Section(
            key=entry.key,
            kind=SectionKind.SECTION,
            heading=entry.title,
            path=entry.path,
            citation=_citation(ctx.source.short_title, entry),
            paragraphs=[b.to_paragraph(ctx.vocab) for b in group_paragraphs(lines, ctx.line_step)],
            source_url=ctx.source.source_url,
            metadata={"unit": entry.key, "section_number": entry.number},
        )

    def _glossary_sections(self, entry: _Entry, lines: list[tuple[Line, str | None]], ctx: _Context) -> list[Section]:
        citation = _citation(ctx.source.short_title, entry)
        sections: list[Section] = []
        seen: set[str] = set()
        for t in _term_list(lines):
            slug = slugify(t.term)
            if slug in seen:
                ctx.warnings.append(f"duplicate glossary term {t.term!r} skipped")
                continue
            seen.add(slug)
            definition = join_lines(t.definition, ctx.vocab)
            sections.append(
                Section(
                    key=f"glossary/{slug}",
                    kind=SectionKind.GLOSSARY_TERM,
                    heading=t.term,
                    path=[*entry.path, t.term],
                    citation=f'{citation} - "{t.term}"',
                    paragraphs=[Paragraph(text=f"{t.term}: {definition}", page=t.first.page, page_label=t.page_label)],
                    source_url=ctx.source.source_url,
                    metadata={"unit": entry.key, "term": t.term},
                )
            )
        return sections

    def _control_sections(self, entry: _Entry, lines: list[tuple[Line, str | None]], ctx: _Context) -> list[Section]:
        citation = _citation(ctx.source.short_title, entry)
        sections: list[Section] = []
        for control_id, name, part in _split_controls(lines):
            blocks = group_paragraphs(part, ctx.line_step)
            if not blocks:
                continue
            heading = f"{control_id} {name.title()}" if control_id else entry.title
            sections.append(
                Section(
                    key=f"{entry.key}/{control_id or 'intro'}",
                    kind=SectionKind.CONTROL if control_id else SectionKind.SECTION,
                    heading=heading,
                    path=[*entry.path, heading] if control_id else entry.path,
                    citation=f"{citation}, {control_id} ({name.title()})" if control_id else citation,
                    paragraphs=[b.to_paragraph(ctx.vocab) for b in blocks],
                    source_url=ctx.source.source_url,
                    metadata={"unit": entry.key, "control_id": control_id, "control_family": entry.title},
                    control_refs=[control_id] if control_id else [],
                )
            )
        return sections


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
        key = number or slugify(title)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            key = f"{key}-{seen[key]}"
        to = dest.get("to") if isinstance(dest, dict) else None
        entries.append(
            _Entry(
                level=level,
                title=title,
                page=page,
                y=float(to.y) if to is not None else 0.0,
                key=key,
                number=number,
                path=list(stack),
                top=stack[0],
            )
        )
    for parent, child in pairwise(entries):
        parent.has_children = child.level > parent.level
    return entries


def _is_control_family(entry: _Entry, controls_prefix: str | None) -> bool:
    """F.7.1 "ACCESS CONTROL - AC" and its siblings list the individual controls."""
    return bool(
        controls_prefix
        and entry.number
        and entry.number.startswith(controls_prefix)
        and entry.level >= _MIN_CONTROL_LEVEL
    )


def _assign_lines(entries: list[_Entry], pages: list[PageText]) -> None:
    """Give every body line to the last bookmark whose destination precedes it."""
    order = sorted(range(len(entries)), key=lambda i: (entries[i].page, entries[i].y, i))
    cursor = -1
    for page in pages:
        for line in page.lines:
            while cursor + 1 < len(order):
                nxt = entries[order[cursor + 1]]
                if (nxt.page, nxt.y) <= (line.page, line.y0 + _BOOKMARK_TOLERANCE):
                    cursor += 1
                else:
                    break
            if cursor >= 0:
                entries[order[cursor]].lines.append((line, page.label))


def _strip_heading(entry: _Entry) -> list[tuple[Line, str | None]]:
    """Drop the heading line(s) that repeat the bookmark title."""
    title = squash(entry.title)
    lines = list(entry.lines)
    while lines and not lines[0][0].is_table and squash(lines[0][0].text) and squash(lines[0][0].text) in title:
        lines.pop(0)
    return lines


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


def _split_controls(
    lines: list[tuple[Line, str | None]],
) -> list[tuple[str | None, str, list[tuple[Line, str | None]]]]:
    parts: list[tuple[str | None, str, list[tuple[Line, str | None]]]] = [(None, "", [])]
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
