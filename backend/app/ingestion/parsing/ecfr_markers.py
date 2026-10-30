"""Paragraph designations in the CFR: (a) > (1) > (i) > (A) > (<i>1</i>) > (<i>i</i>).

The chunker splits long sections at top-level paragraphs and cites chunks by them
("49 CFR 195.452(c)–(f)"), so each paragraph needs to know which top-level letter it belongs to.
"""

import re

from app.ingestion.domain.models import Paragraph

_MARKER = re.compile(r"^\(([a-z]{1,2}|\d{1,2}|[ivxl]{1,6}|[A-Z])\)")
_ROMAN_AMBIGUOUS = frozenset({"i", "v", "x", "l"})
_ROMAN_NEXT = {"i": "ii", "v": "vi", "x": "xi", "l": "li"}
# Reserved paragraphs are dropped by the parser, so the next letter may skip ahead this far.
_MAX_LETTER_SKIP = 3
_LIST_INTRODUCERS = (":", "—", "-")


def assign_markers(paragraphs: list[Paragraph]) -> None:
    """Set `top_marker` and `starts_unit` on each paragraph, in place.

    "(i)", "(v)", "(x)" are ambiguous with roman numerals; they count as letters only when they
    are exactly the next expected letter, the previous paragraph does not introduce a list, and
    the following letter ("(j)") appears before the next roman numeral ("(ii)").
    """
    tokens = [_marker(p) for p in paragraphs]
    expected = "a"
    current: str | None = None
    previous = ""
    for i, p in enumerate(paragraphs):
        token = tokens[i]
        if len(token) == 1 and token.islower() and expected <= token <= chr(ord(expected) + _MAX_LETTER_SKIP):
            introduces_list = previous.rstrip().endswith(_LIST_INTRODUCERS)
            ambiguous = token in _ROMAN_AMBIGUOUS and (
                token != expected or introduces_list or _roman_follows(token, tokens[i + 1 :])
            )
            if not ambiguous:
                current = token
                expected = chr(ord(token) + 1)
                p.starts_unit = True
        p.top_marker = current
        previous = p.text


def _marker(paragraph: Paragraph) -> str:
    if paragraph.is_table:
        return ""
    m = _MARKER.match(paragraph.text)
    return m.group(1) if m else ""


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
