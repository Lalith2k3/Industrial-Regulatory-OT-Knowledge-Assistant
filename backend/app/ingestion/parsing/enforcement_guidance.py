"""Parser for PHMSA enforcement guidance PDFs (e.g. O&M Enforcement Guidance, Part 195 Subpart F).

Each code section is a two-column form: bold labels on the left ("Code Section", "Guidance
Information", "Interpretation Summaries", ...) and their content on the right. Labels are
reassembled from their wrapped lines, content is assigned to the label above it, and each
substantive field becomes a Section. "Existing Code Language" quotes the regulation as it stood
when the guidance was issued; it is deliberately not indexed, because the current regulation text
comes from eCFR and stale copies would compete with it in retrieval.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from app.ingestion.domain.models import Paragraph, ParsedDocument, Section, SectionKind, SourceSpec
from app.ingestion.parsing.base import BaseParser, ParserOptions
from app.ingestion.parsing.pdf_layout import Line, Margins, PageText, group_paragraphs, read_pages, typical_line_step
from app.ingestion.text import build_vocabulary, join_lines, normalize

LabeledLine = tuple[Line, str | None]  # a body line and the printed label of its page

# (full label, field id), compared with the lower-cased, whitespace-collapsed label text.
_FIELDS: list[tuple[str, str]] = [
    ("enforcement guidance", "record"),
    ("revision date", "revision_date"),
    ("code section", "code_section"),
    ("section title", "section_title"),
    ("existing code language", "existing_code"),
    ("origin of code", "origin"),
    ("last amendment", "last_amendment"),
    ("interpretation summaries", "interpretations"),
    ("advisory bulletin/alert notice summaries", "advisories"),
    ("other reference material & source", "references"),
    ("guidance information", "guidance"),
    ("examples of a probable violation or inadequate procedures", "violations"),
    ("examples of evidence", "evidence"),
    ("other special notations", "notations"),
]
_RECORD_FIELD = "record"
_CONTINUATION = "continuation"
_INDEXED_FIELDS = {
    "guidance": "Guidance information",
    "violations": "Examples of probable violations or inadequate procedures",
    "evidence": "Examples of evidence",
    "notations": "Other special notations",
    "references": "Other reference material and sources",
}
_METADATA_FIELDS = ("revision_date", "section_title", "origin", "last_amendment")
_EMPTY_VALUES = frozenset({"", "none", "none.", "n/a", "na", "not applicable", "not applicable."})

_CODE = re.compile(r"§?\s*(\d{1,3}\.\d+[a-z]?(?:\([a-z0-9]+\))*)")
_INTERPRETATION = re.compile(r"^Interpretation\s*:?\s*(.*?)\s*(?:Date\s*:\s*(.*))?$", re.I)
_ADVISORY = re.compile(r"^(Advisory Bulletin|Alert Notice)\b", re.I)
_ADVISORY_ID = re.compile(r"\b(ADB|ALN)\s*-?\s*(\d{2,4})\s*-\s*(\d{1,2})\b", re.I)


class EnforcementGuidanceOptions(ParserOptions):
    header: float = -1.0  # the form starts at the very top of the page; nothing is a header
    footer: float = 720.0  # the printed page number sits below this y (points)
    label_x_max: float = 120.0  # bold lines starting left of this x are form labels
    # A value's first line can start up to ~7pt above its label (e.g. 472.8 vs 479.2 on p. 54).
    # Lines are ~14pt apart, so an 8pt lead cannot capture the previous field's last line.
    label_lead: float = 8.0
    label_line_gap: float = 16.0  # wrapped label lines ("Existing Code" / "Language") are this close


@dataclass
class _Label:
    page: int
    y0: float
    text: str

    @property
    def field(self) -> str:
        """Map label text to a field. Labels can be cut by a page break: a head fragment
        ("Examples of a Probable") still names its field, a tail fragment ("& Source") continues it."""
        key = re.sub(r"\s+", " ", self.text.lower().replace("&", " & ")).strip()
        if key.startswith("advisory"):  # wording varies: "Advisory Bulletins/Alert Notice Summaries", ...
            return "advisories"
        for full, field_id in _FIELDS:
            if key.startswith(full) or full.startswith(key):
                return field_id
        if any(full.endswith(key) for full, _ in _FIELDS):
            return _CONTINUATION
        return f"other:{key}"


@dataclass
class _Record:
    fields: dict[str, list[LabeledLine]] = field(default_factory=dict)

    def value(self, name: str) -> str:
        return normalize(" ".join(ln.text for ln, _ in self.fields.get(name, [])))


@dataclass
class _Form:
    """Everything read from the PDF before sections are built."""

    intro: list[LabeledLine] = field(default_factory=list)
    records: list[_Record] = field(default_factory=list)
    toc_codes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class EnforcementGuidanceParser(BaseParser):
    name: ClassVar[str] = "enforcement_guidance_pdf"
    version: ClassVar[str] = "1.1.0"
    Options = EnforcementGuidanceOptions

    def parse(self, source: SourceSpec, path: Path) -> ParsedDocument:
        options: EnforcementGuidanceOptions = self._options(source)
        pages = read_pages(path, Margins(options.header, options.footer))
        form = _read_form(pages, options)
        builder = _SectionBuilder(
            source=source,
            line_step=typical_line_step(pages),
            vocab=build_vocabulary(ln.text for p in pages for ln in p.lines),
        )
        sections = builder.intro(form.intro) + [s for record in form.records for s in builder.record(record, form)]

        found = {s.metadata.get("unit") for s in sections}
        if missing := [c for c in form.toc_codes if c not in found]:
            form.warnings.append(f"TOC entries without any indexed content: {missing}")
        return ParsedDocument(
            source_id=source.id,
            parser=self.name,
            parser_version=self.version,
            sections=sections,
            paginated=True,
            expected_keys=form.toc_codes,
            warnings=form.warnings,
            metadata={"records": len(form.records), "omitted_fields": ["existing_code"]},
        )


def _read_form(pages: list[PageText], options: EnforcementGuidanceOptions) -> _Form:
    form = _Form()
    current_field: str | None = None
    for page in pages:
        page_label = page.label or str(page.number)
        texts = [ln.text for ln in page.lines]
        if any(t.lower().startswith("table of contents") for t in texts):
            form.toc_codes.extend(m.group(1) for t in texts if t.startswith("§") and (m := _CODE.match(t)))
            continue
        labels = _labels(page.lines, options)
        if not form.records and not any(lb.field == _RECORD_FIELD for lb in labels):
            if texts and texts[0].lower() != "glossary":
                form.intro.extend((ln, page_label) for ln in page.lines)
            continue

        content = [ln for ln in page.lines if not _is_label_line(ln, options.label_x_max)]
        events: list[tuple[float, int, _Label | Line]] = [(lb.y0 - options.label_lead, 0, lb) for lb in labels]
        events += [(ln.y0, 1, ln) for ln in content]
        for _, _, item in sorted(events, key=lambda e: (e[0], e[1])):
            if isinstance(item, _Label):
                if item.field == _CONTINUATION:
                    continue
                current_field = item.field
                if current_field == _RECORD_FIELD:
                    form.records.append(_Record())
                elif form.records:
                    form.records[-1].fields.setdefault(current_field, [])
                if current_field.startswith("other:"):
                    form.warnings.append(f"page {page.number}: unknown label {item.text!r}")
            elif form.records and current_field:
                form.records[-1].fields.setdefault(current_field, []).append((item, page_label))
    return form


@dataclass
class _SectionBuilder:
    source: SourceSpec
    line_step: float
    vocab: set[str]
    # Interpretations / advisories repeated verbatim under several code sections are stored once.
    shared: dict[str, Section] = field(default_factory=dict)

    def intro(self, lines: list[LabeledLine]) -> list[Section]:
        if not lines:
            return []
        return [
            Section(
                key="intro",
                kind=SectionKind.INTRO,
                heading="Introduction",
                path=["Introduction"],
                citation=f"{self.source.short_title}, introduction",
                paragraphs=self._paragraphs(lines),
                source_url=self.source.source_url,
                metadata={"unit": "intro"},
            )
        ]

    def record(self, record: _Record, form: _Form) -> list[Section]:
        m = _CODE.search(record.value("code_section"))
        if not m:
            form.warnings.append(f"record without a code section: {record.value('section_title')!r}")
            return []
        code = m.group(1)
        base = code.split("(")[0]
        path_head = f"§ {code} {record.value('section_title')}".strip()
        meta = {
            "unit": code,
            "code_section": code,
            "cfr_section": base,
            **{name: record.value(name) for name in _METADATA_FIELDS},
        }
        common = {"source_url": self.source.source_url, "cfr_refs": [base]}
        sections: list[Section] = []

        for field_id, heading in _INDEXED_FIELDS.items():
            paragraphs = self._paragraphs(record.fields.get(field_id, []))
            if _is_empty(paragraphs):
                continue
            sections.append(
                Section(
                    key=f"{code}/{field_id}",
                    kind=SectionKind.GUIDANCE,
                    heading=heading,
                    path=[path_head, heading],
                    citation=f"{self.source.short_title}, § {code} - {heading}",
                    paragraphs=paragraphs,
                    metadata={**meta, "field": field_id},
                    **common,
                )
            )

        for kind, items in (
            (SectionKind.INTERPRETATION, _split_items(record.fields.get("interpretations", []), _is_interpretation)),
            (SectionKind.ADVISORY, _split_items(record.fields.get("advisories", []), _is_advisory)),
        ):
            for title_lines, body in items:
                paragraphs = self._paragraphs([*title_lines, *body])
                if _is_empty(paragraphs):
                    continue
                item_id, heading, item_meta = _describe(kind, join_lines(ln.text for ln, _ in title_lines))
                fingerprint = hashlib.sha1(" ".join(p.text for p in paragraphs).lower().encode()).hexdigest()[:12]
                dedupe_key = f"{kind.value}/{item_id or fingerprint}"
                if dedupe_key in self.shared and _same_text(self.shared[dedupe_key], paragraphs):
                    self.shared[dedupe_key].metadata["applies_to"].append(code)
                    continue
                if dedupe_key in self.shared:
                    dedupe_key = f"{dedupe_key}/{fingerprint}"
                section = Section(
                    key=f"{code}/{dedupe_key}",
                    kind=kind,
                    heading=heading,
                    path=[path_head, heading],
                    citation=f"{self.source.short_title}, § {code} - {heading}",
                    paragraphs=paragraphs,
                    metadata={**meta, **item_meta, "applies_to": [code]},
                    **common,
                )
                self.shared[dedupe_key] = section
                sections.append(section)
        return sections

    def _paragraphs(self, lines: list[LabeledLine]) -> list[Paragraph]:
        return [b.to_paragraph(self.vocab) for b in group_paragraphs(lines, self.line_step)]


def _is_label_line(line: Line, x_max: float) -> bool:
    return line.bold and line.x0 < x_max


def _labels(lines: list[Line], options: EnforcementGuidanceOptions) -> list[_Label]:
    """Merge wrapped label lines ("Existing Code" + "Language") into single labels."""
    labels: list[_Label] = []
    last: Line | None = None
    for line in lines:
        if not _is_label_line(line, options.label_x_max):
            continue
        if last is not None and labels and 0 <= line.y0 - last.y0 <= options.label_line_gap:
            labels[-1].text = f"{labels[-1].text} {line.text}"
        else:
            labels.append(_Label(page=line.page, y0=line.y0, text=line.text))
        last = line
    return labels


def _is_interpretation(line: Line) -> bool:
    return line.bold and line.text.lower().startswith("interpretation")


def _is_advisory(line: Line) -> bool:
    return line.bold and bool(_ADVISORY.match(line.text))


def _split_items(
    lines: list[LabeledLine], is_start: Callable[[Line], bool]
) -> list[tuple[list[LabeledLine], list[LabeledLine]]]:
    """Split a field into (title lines, body lines) items; consecutive bold lines form the title."""
    items: list[tuple[list[LabeledLine], list[LabeledLine]]] = []
    for line, label in lines:
        if is_start(line):
            items.append(([(line, label)], []))
        elif not items:
            items.append(([], [(line, label)]))
        elif line.bold and not items[-1][1] and items[-1][0]:
            items[-1][0].append((line, label))
        else:
            items[-1][1].append((line, label))
    return items


def _describe(kind: SectionKind, title: str) -> tuple[str | None, str, dict[str, Any]]:
    if kind == SectionKind.INTERPRETATION:
        m = _INTERPRETATION.match(title)
        raw_id = (m.group(1) if m else "").replace(" ", "").strip("-:")
        date = (m.group(2) or "").strip() if m else ""
        heading = f"Interpretation {raw_id}" + (f" ({date})" if date else "") if raw_id else "Interpretation summary"
        return raw_id or None, heading, {"interpretation_id": raw_id or None, "interpretation_date": date or None}
    m = _ADVISORY_ID.search(title)
    adv_id = f"{m.group(1).upper()}-{m.group(2)}-{m.group(3).zfill(2)}" if m else None
    return adv_id, title.rstrip(".") or "Advisory summary", {"advisory_id": adv_id}


def _is_empty(paragraphs: list[Paragraph]) -> bool:
    return all(p.text.strip().lower() in _EMPTY_VALUES for p in paragraphs)


def _same_text(section: Section, paragraphs: list[Paragraph]) -> bool:
    return [p.text for p in section.paragraphs] == [p.text for p in paragraphs]
