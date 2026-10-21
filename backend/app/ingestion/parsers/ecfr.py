"""Parser for eCFR "full" XML (GPO/eCFR schema): DIV5 part > DIV6 subpart > DIV7 subject group > DIV8 section.

The XML carries the legal structure explicitly, so no layout heuristics are needed: every section
becomes one Section, every definition in a definitions section becomes its own Section, and each
appendix is split at its HD1/HD2 headings.
"""

import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

from lxml import etree

from app.ingestion.errors import ParseError
from app.ingestion.models import Paragraph, ParsedDocument, Section, SourceSpec
from app.ingestion.text import normalize

_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True, remove_pis=True)

_MARKER = re.compile(r"^\(([a-z]{1,2}|\d{1,2}|[ivxl]{1,6}|[A-Z])\)")
_DEFINITIONS_HEAD = re.compile(r"\bdefinitions?\b", re.I)
_DEFINITION_TAIL = re.compile(r"^\s*(means|mean|includes?|is|are|has|refers|shall|or|,|\(|:|—|-)", re.I)
# "Emergency flow restricting device or EFRD means ..." -> the term is everything before the verb
_TERM_BEFORE_VERB = re.compile(r"^(.{2,120}?)\s+(?:means|includes|refers to)\b")
_ROMAN_AMBIGUOUS = {"i", "v", "x", "l"}
_RESERVED_PARAGRAPH = re.compile(r"^(\([a-z0-9]+\)\s*)+\[Reserved\]\.?$")

# Inline elements whose text is navigation noise rather than content.
_DROP_INLINE = {"XREF", "FTREF"}
_PARAGRAPH_TAGS = re.compile(r"^(P|FP(-\d+|-DASH)?|HD\d|PSPACE)$")
_NOTE_PREFIX = {
    "NOTE": "Note:",
    "FTNT": "Footnote:",
    "EDNOTE": "Editorial note:",
    "EFFDNOT": "Effective date note:",
    "EXAMPLE": "Example:",
}


class EcfrXmlParser:
    name = "ecfr_xml"
    version = "1.1.0"

    def parse(self, source: SourceSpec, path: Path) -> ParsedDocument:
        try:
            root = etree.parse(str(path), _XML_PARSER).getroot()
        except (OSError, etree.XMLSyntaxError) as exc:
            raise ParseError(f"{source.id}: cannot read eCFR XML {path}: {exc}") from exc
        if root.tag != "DIV5" or root.get("TYPE") != "PART":
            raise ParseError(f"{source.id}: expected a DIV5 PART root element, got {root.tag}")
        part = root.get("N", "")
        if source.fetch and part != str(source.fetch.part):
            raise ParseError(f"{source.id}: file contains part {part}, registry expects {source.fetch.part}")

        ctx = _Context(source=source, part=part)
        sections = list(ctx.walk(root, []))
        return ParsedDocument(
            source_id=source.id,
            parser=self.name,
            parser_version=self.version,
            sections=sections,
            expected_keys=ctx.expected,
            warnings=ctx.warnings,
            metadata={"part": part, "reserved_sections": ctx.reserved},
        )


class _Context:
    def __init__(self, source: SourceSpec, part: str):
        self.source = source
        self.part = part
        self.snapshot = source.fetch.date.isoformat() if source.fetch else "current"
        self.expected: list[str] = []
        self.reserved: list[str] = []
        self.warnings: list[str] = []

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
        paragraphs = [Paragraph(text=t, is_table=tbl) for t, tbl in self.blocks(div)]
        if not paragraphs:
            if "[Reserved]" not in head:
                self.warnings.append(f"section {number} has no content")
            self.reserved.append(number)
            return
        self.expected.append(number)
        _assign_markers(paragraphs)

        citation = f"49 CFR {number}"
        url = f"{self.source_fetch_base()}/section-{number}"
        meta = {"unit": number, "section": number, "amendments": _citations(div)}
        section_path = [*path, head]

        if _DEFINITIONS_HEAD.search(head):
            definitions = list(self.definitions(div, number, section_path, citation, url, meta))
            if len(definitions) >= 2:
                yield from definitions
                return

        yield Section(
            key=number,
            kind="section",
            heading=head,
            path=section_path,
            citation=citation,
            paragraphs=paragraphs,
            source_url=url,
            metadata=meta,
        )

    def definitions(
        self, div: etree._Element, number: str, path: list[str], citation: str, url: str, meta: dict
    ) -> Iterator[Section]:
        """One Section per defined term; numbered sub-paragraphs stay with their term."""
        term: str | None = None
        body: list[Paragraph] = []
        seen: dict[str, int] = {}

        def flush() -> Section | None:
            if term is None or not body:
                return None
            slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
            seen[slug] = seen.get(slug, 0) + 1
            key = f"{number}/def/{slug}" + (f"-{seen[slug]}" if seen[slug] > 1 else "")
            return Section(
                key=key,
                kind="definition",
                heading=f"{term} (definition)",
                path=[*path, term],
                citation=citation,
                paragraphs=list(body),
                source_url=url,
                metadata={**meta, "term": term},
            )

        for child in div:
            if child.tag == "P" and _defined_term(child):
                done = flush()
                if done:
                    yield done
                text = _clean(_text(child))
                verb = _TERM_BEFORE_VERB.match(text)
                term = (verb.group(1) if verb else _clean(_text(child[0]))).rstrip(".,: ")
                body = [Paragraph(text=text)]
            elif term is not None:
                body.extend(Paragraph(text=t, is_table=tbl) for t, tbl in self.blocks_of([child]))
        done = flush()
        if done:
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
        citation = f"49 CFR Part {self.part}, {label}"
        url = f"{self.source_fetch_base()}/part-{self.part}/appendix-{quote(name)}"

        groups: list[tuple[list[str], list[etree._Element]]] = [([], [])]
        for child in div:
            if child.tag == "HEAD":
                continue
            if child.tag in ("HD1", "HD2", "HD3"):
                level = int(child.tag[-1])
                parent = groups[-1][0][: level - 1]
                groups.append(([*parent, _clean(_text(child))], []))
            else:
                groups[-1][1].append(child)

        for i, (sub_path, elements) in enumerate(groups):
            paragraphs = [Paragraph(text=t, is_table=tbl) for t, tbl in self.blocks_of(elements)]
            if not paragraphs:
                continue
            _assign_markers(paragraphs)
            yield Section(
                key=f"{label.lower().replace(' ', '-')}/{i}",
                kind="appendix",
                heading=sub_path[-1] if sub_path else head,
                path=[*path, head, *sub_path],
                citation=citation,
                paragraphs=paragraphs,
                source_url=url,
                metadata={"unit": label, "appendix": label},
            )

    def source_fetch_base(self) -> str:
        return f"https://www.ecfr.gov/on/{self.snapshot}/title-49"

    # -- content blocks --------------------------------------------------------------------

    def blocks(self, el: etree._Element) -> Iterator[tuple[str, bool]]:
        return self.blocks_of(list(el))

    def blocks_of(self, elements: list[etree._Element]) -> Iterator[tuple[str, bool]]:
        """Yield (text, is_table) for content elements, skipping headings and amendment notes."""
        for child in elements:
            tag = child.tag
            if not isinstance(tag, str) or tag in ("HEAD", "XREF", "CITA", "SECAUTH"):
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
                    yield from self.blocks(child)
            elif tag in _NOTE_PREFIX:
                text = _clean(" ".join(_text(c) for c in child if c.tag not in ("HED",)) or _text(child))
                if text:
                    yield f"{_NOTE_PREFIX[tag]} {text}", False
            elif tag == "EXTRACT":
                yield from self.blocks(child)
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


def _citations(div: etree._Element) -> list[str]:
    return [_clean(_text(c)).strip("[] ") for c in div.findall("CITA")]


def _defined_term(p: etree._Element) -> bool:
    if len(p) == 0 or (p.text or "").strip() or p[0].tag not in ("I", "E"):
        return False
    return bool(_DEFINITION_TAIL.match(p[0].tail or ""))


def _table_text(table: etree._Element) -> str:
    lines: list[str] = []
    caption = table.find("CAPTION")
    if caption is not None and _clean(_text(caption)):
        lines.append(f"Table: {_clean(_text(caption))}")
    for row in table.iter("TR", "ROW"):
        cells = [_clean(_text(c)) for c in row if c.tag in ("TH", "TD", "ENT")]
        cells = [c for c in cells if c]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _assign_markers(paragraphs: list[Paragraph]) -> None:
    """Track the top-level paragraph designation ((a), (b), ...) through nested sub-paragraphs.

    The next letter may skip ahead a little (reserved paragraphs are dropped). "(i)", "(v)", "(x)"
    are ambiguous with roman numerals; they count as letters only when they are exactly the next
    expected letter, the previous paragraph does not introduce a list, and the following letter
    ("(j)") appears before the next roman numeral ("(ii)").
    """
    tokens = [(m.group(1) if (m := _MARKER.match(p.text)) and not p.is_table else "") for p in paragraphs]
    expected = "a"
    current: str | None = None
    previous = ""
    for i, p in enumerate(paragraphs):
        token = tokens[i]
        if len(token) == 1 and token.islower() and expected <= token <= chr(ord(expected) + 3):
            introduces_list = previous.rstrip().endswith((":", "—", "-"))
            ambiguous = token in _ROMAN_AMBIGUOUS and (
                token != expected or introduces_list or _roman_follows(token, tokens[i + 1 :])
            )
            if not ambiguous:
                current = token
                expected = chr(ord(token) + 1)
                p.starts_unit = True
        p.top_marker = current
        previous = p.text


_ROMAN_NEXT = {"i": "ii", "v": "vi", "x": "xi", "l": "li"}


def _roman_follows(token: str, later: list[str]) -> bool:
    """True if `token` opens a roman-numeral list: its successor ("ii") comes before the next
    letter ("j") or any digit marker. Roman items never contain (1), (2)..., whereas a letter
    paragraph (i) does - sometimes inline: "(i) What measures ...?—(1) General requirements"."""
    roman, letter = _ROMAN_NEXT[token], chr(ord(token) + 1)
    for t in later:
        if t == roman:
            return True
        if t == letter or t.isdigit():
            return False
    return False
