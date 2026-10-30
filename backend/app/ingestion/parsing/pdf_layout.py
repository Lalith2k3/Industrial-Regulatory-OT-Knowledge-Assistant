"""Layout-level PDF extraction shared by the PDF parsers (PyMuPDF).

Produces body lines in reading order with font information, after removing running headers and
footers by position. Printed page numbers found in the header/footer zone are kept as page labels,
because the PDF page index and the printed number differ (NIST SP 800-82r3 page "142" is PDF page 159).
"""

import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from pymupdf.table import Table

from app.ingestion.domain.errors import ParseError
from app.ingestion.domain.models import Paragraph
from app.ingestion.text import join_lines, normalize

_PAGE_NUMBER = re.compile(r"(?:page\s+)?([0-9]{1,4}|[ivxlc]{1,7})(?:\s*/\s*\d+)?", re.I)
_SENTENCE_END = re.compile(r"[.:;?!)\]\"']$")
_BULLET = re.compile(r"^(•|-|–|\d{1,2}\.|[a-z]\.|\([a-z0-9]{1,3}\))\s")

DEFAULT_LINE_STEP = 14.0  # baseline distance of 12pt body text; used only if a document has no measurable lines
PARAGRAPH_GAP_FACTOR = 1.12  # a vertical gap this much larger than the normal line step starts a new paragraph
SAME_ROW_TOLERANCE = 1.5  # points; fragments closer than this vertically are on the same row
_TABLE_FOLD_MAX_CHARS = 12  # a lone short cell under a table row ("(1)") is folded into the row above


@dataclass(frozen=True)
class Line:
    page: int  # 1-based
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float
    bold: bool
    italic: bool
    is_table: bool = False


@dataclass
class PageText:
    number: int
    label: str | None
    lines: list[Line] = field(default_factory=list)


@dataclass(frozen=True)
class Margins:
    """Lines whose top edge is above `header` or below `footer` (in points) are page furniture."""

    header: float
    footer: float


def read_pages(path: Path, margins: Margins, *, tables: bool = False) -> list[PageText]:
    try:
        doc = pymupdf.open(path)
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise ParseError(f"cannot open PDF {path}: {exc}") from exc
    with doc:
        return [_read_page(page, margins, tables) for page in doc.pages()]


def _read_page(page: pymupdf.Page, margins: Margins, tables: bool) -> PageText:
    number = page.number + 1
    label: str | None = None
    body: list[Line] = []
    boxes: list[pymupdf.Rect] = []

    if tables:
        for table in page.find_tables().tables:
            rows = _table_rows(table)
            if len(rows) < 2:
                continue
            box = pymupdf.Rect(table.bbox)
            boxes.append(box)
            body.append(Line(number, box.x0, box.y0, box.x1, box.y1, "\n".join(rows), 10.0, False, False, True))

    for line in _page_lines(page, number):
        if line.y0 < margins.header or line.y0 > margins.footer:
            m = _PAGE_NUMBER.fullmatch(line.text)
            if label is None and m:
                label = m.group(1)
            continue
        if any(box.contains(pymupdf.Point((line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2)) for box in boxes):
            continue
        body.append(line)

    body.sort(key=lambda ln: (round(ln.y0), ln.x0))
    return PageText(number=number, label=label, lines=body)


def _page_lines(page: pymupdf.Page, number: int) -> Iterable[Line]:
    for block in page.get_text("dict")["blocks"]:
        for raw in block.get("lines", []):
            spans = [s for s in raw["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = normalize("".join(s["text"] for s in raw["spans"]))
            if not text:
                continue
            chars = sum(len(s["text"]) for s in spans)
            bold_chars = sum(len(s["text"]) for s in spans if s["flags"] & 16 or "Bold" in s["font"])
            italic_chars = sum(len(s["text"]) for s in spans if s["flags"] & 2 or "Italic" in s["font"])
            x0, y0, x1, y1 = raw["bbox"]
            yield Line(
                page=number,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                text=text,
                size=round(max(s["size"] for s in spans), 1),
                bold=bold_chars * 2 > chars,
                italic=italic_chars * 2 > chars,
            )


def _table_rows(table: Table) -> list[str]:
    """Serialise a detected table row by row, keeping column positions.

    Positions matter: in "AC-18 | Wireless Access | Select | Select | Select" each "Select" belongs
    to a different baseline column (LOW / MOD / HIGH). Merged-cell placeholders (None) and columns
    that are empty throughout are dropped; empty cells render as "-". A row with a single short
    value (an enhancement number wrapped below its row) is folded into the row above.
    """
    raw = [[normalize((c or "").replace("\n", " ")) if c is not None else None for c in row] for row in table.extract()]
    if not raw:
        return []
    keep = [j for j in range(len(raw[0])) if any(j < len(r) and r[j] for r in raw)]
    grid: list[list[str]] = []
    for row in raw:
        cells = [(row[j] or "") if j < len(row) else "" for j in keep]
        filled = [j for j, c in enumerate(cells) if c]
        if not filled:
            continue
        if grid and len(filled) == 1 and len(cells[filled[0]]) <= _TABLE_FOLD_MAX_CHARS:
            j = filled[0]
            grid[-1][j] = f"{grid[-1][j]} {cells[j]}".strip()
            continue
        grid.append(cells)
    rows: list[str] = []
    for cells in grid:
        joined = " | ".join(c or "-" for c in cells)
        if not rows or rows[-1] != joined:
            rows.append(joined)
    return rows


def merge_rows(page: PageText, max_gap_factor: float = 3.0) -> PageText:
    """Join text fragments that sit on the same row with the same weight ("AU-1 " + "AUDIT AND ...").

    Not for form-like layouts, where a label and its value share a row on purpose.
    """
    merged: list[Line] = []
    for line in page.lines:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and not prev.is_table
            and not line.is_table
            and abs(line.y0 - prev.y0) < SAME_ROW_TOLERANCE
            and prev.bold == line.bold
            and 0 <= line.x0 - prev.x1 < max_gap_factor * line.size
        ):
            merged[-1] = Line(
                page=prev.page,
                x0=prev.x0,
                y0=min(prev.y0, line.y0),
                x1=line.x1,
                y1=max(prev.y1, line.y1),
                text=f"{prev.text} {line.text}",
                size=max(prev.size, line.size),
                bold=prev.bold,
                italic=prev.italic and line.italic,
            )
        else:
            merged.append(line)
    return PageText(number=page.number, label=page.label, lines=merged)


def typical_line_step(pages: list[PageText]) -> float:
    """Median baseline distance between consecutive lines of body text."""
    steps = []
    for page in pages:
        for a, b in zip(page.lines, page.lines[1:], strict=False):
            dy = b.y0 - a.y0
            if not a.is_table and not b.is_table and abs(a.size - b.size) < 0.5 and 0 < dy < 2 * a.size:
                steps.append(dy)
    return statistics.median(steps) if steps else DEFAULT_LINE_STEP


@dataclass
class Block:
    lines: list[Line]
    label_start: str | None
    label_end: str | None

    @property
    def first(self) -> Line:
        return self.lines[0]

    @property
    def is_table(self) -> bool:
        return self.first.is_table

    def to_paragraph(self, vocab: set[str] | None) -> Paragraph:
        text = self.first.text if self.is_table else join_lines((ln.text for ln in self.lines), vocab)
        last = self.lines[-1]
        return Paragraph(
            text=text,
            page=self.first.page,
            page_label=self.label_start,
            page_end=last.page if last.page != self.first.page else None,
            page_label_end=self.label_end if last.page != self.first.page else None,
            is_table=self.is_table,
        )


def group_paragraphs(
    lines: list[tuple[Line, str | None]],
    line_step: float,
    *,
    gap_factor: float = PARAGRAPH_GAP_FACTOR,
) -> list[Block]:
    """Group (line, page_label) pairs into paragraph blocks.

    A new block starts on a larger-than-normal vertical gap, a change of bold/size (headings),
    a bullet or list marker, a table, or a page break after a sentence end.
    """
    blocks: list[Block] = []
    for line, label in lines:
        if blocks and not _starts_new_block(blocks[-1].lines[-1], line, line_step, gap_factor):
            blocks[-1].lines.append(line)
            blocks[-1].label_end = label
        else:
            blocks.append(Block(lines=[line], label_start=label, label_end=label))
    return blocks


def _starts_new_block(prev: Line, line: Line, line_step: float, gap_factor: float) -> bool:
    if prev.is_table or line.is_table:
        return True
    if prev.bold != line.bold or abs(prev.size - line.size) > 1:
        return True
    if _BULLET.match(line.text):
        return True
    if line.page != prev.page:
        return bool(_SENTENCE_END.search(prev.text)) or not line.text[:1].islower()
    dy = line.y0 - prev.y0
    return dy < -1 or dy > line_step * gap_factor
