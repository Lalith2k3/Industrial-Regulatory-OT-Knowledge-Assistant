"""Parser for eCFR "full" XML (GPO/eCFR schema): DIV5 part > DIV6 subpart > DIV7 subject group > DIV8 section.

The XML carries the legal structure explicitly, so no layout heuristics are needed: every section
becomes one Section, every definition in a definitions section becomes its own Section, and each
appendix is split at its HD1/HD2 headings.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

from lxml import etree

from app.ingestion.domain.errors import ParseError
from app.ingestion.domain.models import Paragraph, ParsedDocument, Section, SectionKind, SourceSpec
from app.ingestion.parsing.base import BaseParser, ParserOptions
from app.ingestion.parsing.ecfr_markers import assign_markers
from app.ingestion.text import normalize, slugify

_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True, remove_pis=True)

_DEFINITIONS_HEAD = re.compile(r"\bdefinitions?\b", re.I)
_DEFINITION_TAIL = re.compile(r"^\s*(means|mean|includes?|is|are|has|refers|shall|or|,|\(|:|—|-)", re.I)
# "Emergency flow restricting device or EFRD means ..." -> the term is everything before the verb
_TERM_BEFORE_VERB = re.compile(r"^(.{2,120}?)\s+(?:means|includes|refers to)\b")
_RESERVED_PARAGRAPH = re.compile(r"^(\([a-z0-9]+\)\s*)+\[Reserved\]\.?$")
_MIN_DEFINITIONS_TO_SPLIT = 2

# Inline elements whose text is navigation noise rather than content.
_DROP_INLINE = frozenset({"XREF", "FTREF"})
_SKIPPED_BLOCKS = frozenset({"HEAD", "XREF", "CITA", "SECAUTH"})
_PARAGRAPH_TAGS = re.compile(r"^(P|FP(-\d+|-DASH)?|HD\d|PSPACE)$")
_HEADING_TAGS = ("HD1", "HD2", "HD3")
_NOTE_PREFIX = {
    "NOTE": "Note:",
    "FTNT": "Footnote:",
    "EDNOTE": "Editorial note:",
    "EFFDNOT": "Effective date note:",
    "EXAMPLE": "Example:",
}


class EcfrOptions(ParserOptions):
    cfr_title: int | None = None  # only needed when the source has no `fetch:` spec


class EcfrXmlParser(BaseParser):
    name: ClassVar[str] = "ecfr_xml"
    version: ClassVar[str] = "1.1.0"
    Options = EcfrOptions

    def __init__(self, site_url: str):
        self._site_url = site_url.rstrip("/")

    def parse(self, source: SourceSpec, path: Path) -> ParsedDocument:
        options: EcfrOptions = self._options(source)
        title = source.fetch.title if source.fetch else options.cfr_title
        if title is None:
            raise ParseError(f"{source.id}: set `fetch.title` or `options.cfr_title`")
        try:
            root = etree.parse(str(path), _XML_PARSER).getroot()
        except (OSError, etree.XMLSyntaxError) as exc:
            raise ParseError(f"{source.id}: cannot read eCFR XML {path}: {exc}") from exc
        if root.tag != "DIV5" or root.get("TYPE") != "PART":
            raise ParseError(f"{source.id}: expected a DIV5 PART root element, got {root.tag}")
        part = root.get("N", "")
        if source.fetch and part != str(source.fetch.part):
            raise ParseError(f"{source.id}: file contains part {part}, registry expects {source.fetch.part}")

        snapshot = source.fetch.date.isoformat() if source.fetch else "current"
        walker = _PartWalker(title=title, part=part, url_base=f"{self._site_url}/on/{snapshot}/title-{title}")
        sections = list(walker.walk(root, []))
        return ParsedDocument(
            source_id=source.id,
            parser=self.name,
            parser_version=self.version,
            sections=sections,
            expected_keys=walker.expected,
            warnings=walker.warnings,
            metadata={"part": part, "reserved_sections": walker.reserved},
        )


@dataclass
class _PartWalker:
    title: int
    part: str
    url_base: str
    expected: list[str] = field(default_factory=list)
    reserved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # -- structure -------------------------------------------------------------------------

    def walk(self, el: etree._Element, path: list[str]) -> Iterator[Section]:
        for child in el:
            if child.tag in ("DIV6", "DIV7"):
                yield from self.walk(child, [*path, _head(child)])
            elif child.tag == "DIV8":
                yield from self.section(child, path)
            elif child.tag == "DIV9":
                yield from self.appendix(child, path)

    def section(self, div: etree._Element, path: list[str]) -> Iterator[Section]:
        number = div.get("N", "")
        head = _head(div)
        paragraphs = [Paragraph(text=t, is_table=tbl) for t, tbl in self.blocks(list(div))]
        if not paragraphs:
            if "[Reserved]" not in head:
                self.warnings.append(f"section {number} has no content")
            self.reserved.append(number)
            return
        self.expected.append(number)
        assign_markers(paragraphs)

        base = {
            "citation": f"{self.title} CFR {number}",
            "source_url": f"{self.url_base}/section-{number}",
            "own_cfr_ref": number,
            "metadata": {"unit": number, "section": number, "amendments": _amendments(div)},
        }
        section_path = [*path, head]

        if _DEFINITIONS_HEAD.search(head):
            definitions = list(self.definitions(div, number, section_path, base))
            if len(definitions) >= _MIN_DEFINITIONS_TO_SPLIT:
                yield from definitions
                return

        yield Section(
            key=number,
            kind=SectionKind.SECTION,
            heading=head,
            path=section_path,
            paragraphs=paragraphs,
            paragraph_citations=True,
            **base,
        )

    def definitions(self, div: etree._Element, number: str, path: list[str], base: dict[str, Any]) -> Iterator[Section]:
        """One Section per defined term; numbered sub-paragraphs stay with their term."""
        seen: dict[str, int] = {}
        term: str | None = None
        body: list[Paragraph] = []

        def build() -> Section | None:
            if term is None or not body:
                return None
            slug = slugify(term)
            seen[slug] = seen.get(slug, 0) + 1
            suffix = f"-{seen[slug]}" if seen[slug] > 1 else ""
            return Section(
                key=f"{number}/def/{slug}{suffix}",
                kind=SectionKind.DEFINITION,
                heading=f"{term} (definition)",
                path=[*path, term],
                paragraphs=list(body),
                **{**base, "metadata": {**base["metadata"], "term": term}},
            )

        for child in div:
            if child.tag == "P" and _is_defined_term(child):
                if done := build():
                    yield done
                text = _clean(_text(child))
                verb = _TERM_BEFORE_VERB.match(text)
                term = (verb.group(1) if verb else _clean(_text(child[0]))).rstrip(".,: ")
                body = [Paragraph(text=text)]
            elif term is not None:
                body.extend(Paragraph(text=t, is_table=tbl) for t, tbl in self.blocks([child]))
        if done := build():
            yield done

    def appendix(self, div: etree._Element, path: list[str]) -> Iterator[Section]:
        name = div.get("N", "")  # "Appendix A to Part 195"
        head = _head(div)
        letter = re.search(r"Appendix\s+(\w+)", name)
        label = f"Appendix {letter.group(1)}" if letter else name
        if "[Reserved]" in head:
            self.reserved.append(label)
            return
        self.expected.append(label)

        groups: list[tuple[list[str], list[etree._Element]]] = [([], [])]
        for child in div:
            if child.tag == "HEAD":
                continue
            if child.tag in _HEADING_TAGS:
                level = int(child.tag[-1])
                parent = groups[-1][0][: level - 1]
                groups.append(([*parent, _clean(_text(child))], []))
            else:
                groups[-1][1].append(child)

        for i, (sub_path, elements) in enumerate(groups):
            paragraphs = [Paragraph(text=t, is_table=tbl) for t, tbl in self.blocks(elements)]
            if not paragraphs:
                continue
            assign_markers(paragraphs)
            yield Section(
                key=f"{slugify(label)}/{i}",
                kind=SectionKind.APPENDIX,
                heading=sub_path[-1] if sub_path else head,
                path=[*path, head, *sub_path],
                citation=f"{self.title} CFR Part {self.part}, {label}",
                paragraphs=paragraphs,
                source_url=f"{self.url_base}/part-{self.part}/appendix-{quote(name)}",
                metadata={"unit": label, "appendix": label},
            )

    # -- content blocks --------------------------------------------------------------------

    def blocks(self, elements: list[etree._Element]) -> Iterator[tuple[str, bool]]:
        """Yield (text, is_table) for content elements, skipping headings and amendment notes."""
        for child in elements:
            tag = child.tag
            if not isinstance(tag, str) or tag in _SKIPPED_BLOCKS:
                continue
            if _PARAGRAPH_TAGS.match(tag):
                text = _clean(_text(child))
                if text and not _RESERVED_PARAGRAPH.match(text):
                    yield text, False
            elif tag in ("TABLE", "GPOTABLE"):
                yield _table_text(child), True
            elif tag == "DIV":
                tables = list(child.iter("TABLE", "GPOTABLE"))
                if tables:
                    for table in tables:
                        yield _table_text(table), True
                else:
                    yield from self.blocks(list(child))
            elif tag in _NOTE_PREFIX:
                text = _clean(" ".join(_text(c) for c in child if c.tag != "HED") or _text(child))
                if text:
                    yield f"{_NOTE_PREFIX[tag]} {text}", False
            elif tag == "EXTRACT":
                yield from self.blocks(list(child))
            else:
                text = _clean(_text(child))
                if text:
                    self.warnings.append(f"unhandled element <{tag}> kept as plain text")
                    yield text, False


def _clean(text: str) -> str:
    """XML whitespace (including newlines inside an element) is not meaningful."""
    return normalize(" ".join(text.split()))


def _head(el: etree._Element) -> str:
    head = el.find("HEAD")
    return _clean(_text(head)) if head is not None else el.get("N", "")


def _text(el: etree._Element) -> str:
    parts = [el.text or ""]
    for child in el:
        if isinstance(child.tag, str) and child.tag not in _DROP_INLINE:
            # Fractions must not fuse with the whole number: "7<FR>1/2</FR>" and
            # "7<sup>1</sup>⁄<sub>2</sub>" read "7 1/2", not "71/2". <br/> separates words.
            fraction = child.tag == "FR" or (child.tag == "sup" and (child.tail or "").startswith("⁄"))
            parts.append(" " if child.tag in ("br", "BR") or fraction else "")
            parts.append(_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _amendments(div: etree._Element) -> list[str]:
    return [_clean(_text(c)).strip("[] ") for c in div.findall("CITA")]


def _is_defined_term(p: etree._Element) -> bool:
    if len(p) == 0 or (p.text or "").strip() or p[0].tag not in ("I", "E"):
        return False
    return bool(_DEFINITION_TAIL.match(p[0].tail or ""))


def _table_text(table: etree._Element) -> str:
    lines: list[str] = []
    caption = table.find("CAPTION")
    if caption is not None and (caption_text := _clean(_text(caption))):
        lines.append(f"Table: {caption_text}")
    for row in table.iter("TR", "ROW"):
        cells = [c for c in (_clean(_text(cell)) for cell in row if cell.tag in ("TH", "TD", "ENT")) if c]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)
