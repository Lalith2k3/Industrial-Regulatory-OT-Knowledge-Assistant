"""Regressions found while building the evaluation set against the real corpus."""

from lxml import etree

from app.ingestion.models import Paragraph
from app.ingestion.parsers.ecfr import _assign_markers, _clean, _text
from app.ingestion.parsers.pdf_layout import _table_rows
from app.ingestion.text import normalize


def test_paragraph_i_vs_roman_numeral_i():
    # 49 CFR 195.452(h)(4)(i)-(iv) are roman numerals; 195.452(i) is a paragraph letter whose (1) is inline.
    texts = [
        "(h) What actions must an operator take?",
        "(4) Special requirements for scheduling remediation",
        "(i) Immediate repair conditions.",
        "(ii) 60-day conditions.",
        "(iv) Other conditions.",
        "(i) What preventive and mitigative measures must an operator take?—(1) General requirements.",
        "(2) Risk analysis criteria. Factors include:",
        "(i) Terrain surrounding the pipeline segment;",
        "(ii) Elevation profile;",
        "(j) What is a continual process of evaluation?",
    ]
    preceding = [f"({letter}) Earlier paragraph." for letter in "abcdefg"]
    paragraphs = [Paragraph(text=t) for t in preceding + texts]
    _assign_markers(paragraphs)
    assert [p.top_marker for p in paragraphs[7:]] == ["h", "h", "h", "h", "h", "i", "i", "i", "i", "j"]
    assert [p.text[:3] for p in paragraphs[7:] if p.starts_unit] == ["(h)", "(i)", "(j)"]


def test_fractions_and_line_breaks_in_ecfr_markup():
    def xml_text(markup: str) -> str:
        return _clean(_text(etree.fromstring(markup)))

    assert xml_text("<TD>7<sup>1</sup>⁄<sub>2</sub> months</TD>") == "7 1/2 months"
    assert xml_text("<P>219.1 mm (8<FR>5/8</FR> in)</P>") == "219.1 mm (8 5/8 in)"
    assert xml_text("<TH>Installed before<br/>(Nov. 12, 1970)</TH>") == "Installed before (Nov. 12, 1970)"


def test_vulgar_fraction_after_whole_number():
    assert normalize("every 7½ months") == "every 7 1/2 months"


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows

    def extract(self):
        return self._rows


def test_pdf_tables_keep_column_positions():
    table = _FakeTable(
        [
            ["CNTL\nNO.", None, None, "CONTROL NAME", "CONTROL BASELINES", None, None],
            [None, None, None, None, "LOW", "MOD", "HIGH"],
            ["AC-18", None, None, "Wireless Access", "Select", "Select", "Select"],
            ["", "AC-18", "", "WIRELESS ACCESS | DISABLE WIRELESS NETWORKING", "", "Select", "Select"],
            [None, "(3)", None, None, None, None, None],
        ]
    )
    assert _table_rows(table) == [
        "CNTL NO. | - | CONTROL NAME | CONTROL BASELINES | - | -",
        "- | - | - | LOW | MOD | HIGH",
        "AC-18 | - | Wireless Access | Select | Select | Select",  # three baselines, not one
        "- | AC-18 (3) | WIRELESS ACCESS | DISABLE WIRELESS NETWORKING | - | Select | Select",
    ]
