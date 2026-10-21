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
from dataclasses import dataclass, field
from pathlib import Path

from app.ingestion.models import Paragraph, ParsedDocument, Section, SourceSpec
from app.ingestion.parsers.pdf_layout import Line, Margins, group_paragraphs, read_pages, typical_line_step
from app.ingestion.text import build_vocabulary, join_lines, normalize

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
CONTINUATION = "continuation"
_INDEXED_FIELDS = {
    "guidance": "Guidance information",
    "violations": "Examples of probable violations or inadequate procedures",
    "evidence": "Examples of evidence",
    "notations": "Other special notations",
    "references": "Other reference material and sources",
}
_METADATA_FIELDS = {"revision_date", "code_section", "section_title", "origin", "last_amendment"}
_EMPTY_VALUES = {"", "none", "none.", "n/a", "na", "not applicable", "not applicable."}

_CODE = re.compile(r"§?\s*(19\d\.\d+[a-z]?(?:\([a-z0-9]+\))*)")
_INTERPRETATION = re.compile(r"^Interpretation\s*:?\s*(.*?)\s*(?:Date\s*:\s*(.*))?$", re.I)
_ADVISORY = re.compile(r"^(Advisory Bulletin|Alert Notice)\b", re.I)
_ADVISORY_ID = re.compile(r"\b(ADB|ALN)\s*-?\s*(\d{2,4})\s*-\s*(\d{1,2})\b", re.I)


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
            return CONTINUATION
        return f"other:{key}"


@dataclass
class _Record:
    fields: dict[str, list[tuple[Line, str | None]]] = field(default_factory=dict)

    def value(self, name: str) -> str:
        return normalize(" ".join(ln.text for ln, _ in self.fields.get(name, [])))


class EnforcementGuidanceParser:
    name = "enforcement_guidance_pdf"
    version = "1.1.0"

    def parse(self, source: SourceSpec, path: Path) -> ParsedDocument:
        opts = source.options
        label_x_max = float(opts.get("label_x_max", 120))
        pages = read_pages(path, Margins(header=opts.get("header", -1), footer=opts.get("footer", 720)))
        step = typical_line_step(pages)
        vocab = build_vocabulary(ln.text for p in pages for ln in p.lines)
        warnings: list[str] = []

        expected: list[str] = []
        intro: list[tuple[Line, str | None]] = []
        records: list[_Record] = []
        current_field: str | None = None

        for page in pages:
            label = page.label or str(page.number)
            texts = [ln.text for ln in page.lines]
            if any(t.lower().startswith("table of contents") for t in texts):
                expected.extend(m.group(1) for t in texts if t.startswith("§") and (m := _CODE.match(t)))
                continue
            labels = _labels(page.lines, label_x_max)
            if not records and not any(lb.field == "record" for lb in labels):
                if texts and texts[0].lower() != "glossary":
                    intro.extend((ln, label) for ln in page.lines)
                continue

            content = [ln for ln in page.lines if not _is_label_line(ln, label_x_max)]
            # A value's first line can start up to ~7pt above its label (e.g. 472.8 vs 479.2 on p. 54).
            # Lines are ~14pt apart, so an 8pt lead cannot capture the previous field's last line.
            events: list[tuple[float, int, object]] = [(lb.y0 - 8, 0, lb) for lb in labels]
            events += [(ln.y0, 1, ln) for ln in content]
            for _, _, item in sorted(events, key=lambda e: (e[0], e[1])):
                if isinstance(item, _Label):
                    if item.field == CONTINUATION:
                        continue
                    current_field = item.field
                    if current_field == "record":
                        records.append(_Record())
                    elif records:
                        records[-1].fields.setdefault(current_field, [])
                    if current_field.startswith("other:"):
                        warnings.append(f"page {page.number}: unknown label {item.text!r}")
                elif records and current_field:
                    records[-1].fields.setdefault(current_field, []).append((item, label))

        sections: list[Section] = []
        if intro:
            blocks = group_paragraphs(intro, step)
            sections.append(
                Section(
                    key="intro",
                    kind="intro",
                    heading="Introduction",
                    path=["Introduction"],
                    citation=f"{source.short_title}, introduction",
                    paragraphs=[b.to_paragraph(vocab) for b in blocks],
                    source_url=source.source_url,
                    metadata={"unit": "intro"},
                )
            )

        shared: dict[str, Section] = {}  # interpretations / advisories repeated across sections
        for record in records:
            m = _CODE.search(record.value("code_section"))
            if not m:
                warnings.append(f"record without a code section: {record.value('section_title')!r}")
                continue
            code = m.group(1)
            base = code.split("(")[0]
            title = record.value("section_title")
            path_head = f"§ {code} {title}".strip()
            meta = {
                "unit": code,
                "code_section": code,
                "cfr_section": base,
                **{name: record.value(name) for name in _METADATA_FIELDS if name != "code_section"},
            }

            for field_id, heading in _INDEXED_FIELDS.items():
                lines = record.fields.get(field_id, [])
                paragraphs = [b.to_paragraph(vocab) for b in group_paragraphs(lines, step)]
                if _is_empty(paragraphs):
                    continue
                sections.append(
                    Section(
                        key=f"{code}/{field_id}",
                        kind="guidance",
                        heading=heading,
                        path=[path_head, heading],
                        citation=f"{source.short_title}, § {code} - {heading}",
                        paragraphs=paragraphs,
                        source_url=source.source_url,
                        metadata={**meta, "field": field_id},
                    )
                )

            for kind, items in (
                ("interpretation", _split_items(record.fields.get("interpretations", []), _is_interpretation_start)),
                ("advisory", _split_items(record.fields.get("advisories", []), _is_advisory_start)),
            ):
                for title_lines, body in items:
                    title_text = join_lines(ln.text for ln, _ in title_lines)
                    paragraphs = [b.to_paragraph(vocab) for b in group_paragraphs([*title_lines, *body], step)]
                    if _is_empty(paragraphs):
                        continue
                    item_id, heading, item_meta = _describe(kind, title_text)
                    fingerprint = hashlib.sha1(
                        " ".join(p.text for p in paragraphs).lower().encode()
                    ).hexdigest()[:12]
                    dedupe_key = f"{kind}/{item_id or fingerprint}"
                    if dedupe_key in shared and _same_text(shared[dedupe_key], paragraphs):
                        shared[dedupe_key].metadata["applies_to"].append(code)
                        continue
                    if dedupe_key in shared:
                        dedupe_key = f"{dedupe_key}/{fingerprint}"
                    section = Section(
                        key=f"{code}/{dedupe_key}",
                        kind=kind,
                        heading=heading,
                        path=[path_head, heading],
                        citation=f"{source.short_title}, § {code} - {heading}",
                        paragraphs=paragraphs,
                        source_url=source.source_url,
                        metadata={**meta, **item_meta, "applies_to": [code]},
                    )
                    shared[dedupe_key] = section
                    sections.append(section)

        found = {s.metadata.get("unit") for s in sections}
        missing = [c for c in expected if c not in found]
        if missing:
            warnings.append(f"TOC entries without any indexed content: {missing}")
        return ParsedDocument(
            source_id=source.id,
            parser=self.name,
            parser_version=self.version,
            sections=sections,
            expected_keys=expected,
            warnings=warnings,
            metadata={"records": len(records), "omitted_fields": ["existing_code"]},
        )


def _is_label_line(line: Line, x_max: float) -> bool:
    return line.bold and line.x0 < x_max


def _labels(lines: list[Line], x_max: float) -> list[_Label]:
    """Merge wrapped label lines ("Existing Code" + "Language") into single labels."""
    labels: list[_Label] = []
    last: Line | None = None
    for line in lines:
        if not _is_label_line(line, x_max):
            continue
        if last is not None and labels and 0 <= line.y0 - last.y0 <= 16:
            labels[-1].text = f"{labels[-1].text} {line.text}"
        else:
            labels.append(_Label(page=line.page, y0=line.y0, text=line.text))
        last = line
    return labels


def _is_interpretation_start(line: Line) -> bool:
    return line.bold and line.text.lower().startswith("interpretation")


def _is_advisory_start(line: Line) -> bool:
    return line.bold and bool(_ADVISORY.match(line.text))


def _split_items(
    lines: list[tuple[Line, str | None]], is_start
) -> list[tuple[list[tuple[Line, str | None]], list[tuple[Line, str | None]]]]:
    """Split a field into (title lines, body lines) items; consecutive bold lines form the title."""
    items: list[tuple[list, list]] = []
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


def _describe(kind: str, title: str) -> tuple[str | None, str, dict]:
    if kind == "interpretation":
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
