"""Parser for PHMSA FAQ guidance PDFs: numbered questions grouped under bold topic headings.

Contractor Qualification                                  <- topic (bold)
2 – Are contractors required to have ...? (§§ 192.805, 195.505)   <- question + cited sections
No. The operator must ensure ...                          <- answer
"""

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from app.ingestion.domain.models import Paragraph, ParsedDocument, Section, SectionKind, SourceSpec
from app.ingestion.parsing.base import BaseParser, ParserOptions
from app.ingestion.parsing.pdf_layout import Line, Margins, PageText, group_paragraphs, read_pages, typical_line_step
from app.ingestion.references import ReferenceExtractor
from app.ingestion.text import build_vocabulary, join_lines

LabeledLine = tuple[Line, str | None]

_QUESTION = re.compile(r"^(\d{1,3})\s*[–—-]\s*(.+)$")
# Question ends with its citation parenthetical, which may nest one level: (§§ 192.805(b), (c); 195.505(b))
_CITATION_END = re.compile(r"\(\s*§[^()]*(?:\([^()]*\)[^()]*)*\)\s*$")
_REVISED = re.compile(r"Revised\s+([A-Z][a-z]+ \d{1,2}, \d{4})")
_FOOTNOTE_START = re.compile(r"^(\d{1,2})\s+(.*)$")
_MAX_QUESTION_NUMBER_JUMP = 3  # a numbered list inside an answer must not be read as the next question
_MIN_LINES_FOR_UNCITED_QUESTION = 4


class FaqOptions(ParserOptions):
    header: float = 60.0  # the printed page number sits above this y (points)
    footer: float = 760.0
    footnote_size_delta: float = 1.5  # text this many points smaller than the body is a footnote
    title_min_size: float = 13.0  # bold text at least this large is the document title


@dataclass
class _Faq:
    number: int
    topic: str | None
    question: list[Line] = field(default_factory=list)
    answer: list[LabeledLine] = field(default_factory=list)
    question_done: bool = False
    page_labels: list[str | None] = field(default_factory=list)


@dataclass
class _Reading:
    intro: list[LabeledLine] = field(default_factory=list)
    faqs: list[_Faq] = field(default_factory=list)
    footnotes: dict[str, str] = field(default_factory=dict)
    revised: str | None = None


class FaqPdfParser(BaseParser):
    name: ClassVar[str] = "faq_pdf"
    version: ClassVar[str] = "1.0.0"
    Options = FaqOptions

    def __init__(self, cfr_refs: ReferenceExtractor):
        self._cfr_refs = cfr_refs

    def parse(self, source: SourceSpec, path: Path) -> ParsedDocument:
        options: FaqOptions = self._options(source)
        pages = read_pages(path, Margins(options.header, options.footer))
        reading = _read(pages, options)
        step = typical_line_step(pages)
        vocab = build_vocabulary(ln.text for p in pages for ln in p.lines)

        sections: list[Section] = []
        if reading.intro:
            first_line, first_label = reading.intro[0]
            text = _attach_footnotes(join_lines((ln.text for ln, _ in reading.intro), vocab), reading.footnotes)
            sections.append(
                Section(
                    key="intro",
                    kind=SectionKind.INTRO,
                    heading="About this guidance",
                    path=["Introduction"],
                    citation=f"{source.short_title}, introduction",
                    paragraphs=[Paragraph(text=text, page=first_line.page, page_label=first_label)],
                    source_url=source.source_url,
                    metadata={"unit": "intro"},
                )
            )

        warnings: list[str] = []
        for faq in reading.faqs:
            question = _QUESTION.sub(r"\2", join_lines((ln.text for ln in faq.question), vocab), count=1)
            answer = [b.to_paragraph(vocab) for b in group_paragraphs(faq.answer, step)]
            if not answer:
                warnings.append(f"FAQ {faq.number} has no answer text")
            for p in answer:
                p.text = _attach_footnotes(p.text, reading.footnotes)
            first = faq.question[0]
            sections.append(
                Section(
                    key=f"faq-{faq.number}",
                    kind=SectionKind.FAQ,
                    heading=question,
                    path=[faq.topic or "General", f"Question {faq.number}"],
                    citation=f"{source.short_title}, Q{faq.number}",
                    paragraphs=[
                        Paragraph(text=f"Q{faq.number}: {question}", page=first.page, page_label=faq.page_labels[0]),
                        *answer,
                    ],
                    source_url=source.source_url,
                    metadata={
                        "unit": str(faq.number),
                        "faq_number": faq.number,
                        "topic": faq.topic,
                        "question": question,
                        "cited_sections": self._cfr_refs.extract(question),
                    },
                )
            )

        numbers = [f.number for f in reading.faqs]
        if numbers != sorted(set(numbers)):
            warnings.append(f"FAQ numbers out of order or duplicated: {numbers}")
        return ParsedDocument(
            source_id=source.id,
            parser=self.name,
            parser_version=self.version,
            sections=sections,
            paginated=True,
            expected_keys=[str(i) for i in range(1, max(numbers, default=0) + 1)],
            warnings=warnings,
            metadata={"revised": reading.revised, "faq_count": len(reading.faqs), "footnotes": reading.footnotes},
        )


def _read(pages: list[PageText], options: FaqOptions) -> _Reading:
    """Classify every line as title, footnote, topic, question, answer or introduction."""
    reading = _Reading()
    footnote_size = statistics.median(ln.size for p in pages for ln in p.lines) - options.footnote_size_delta
    topic: str | None = None
    prev_was_topic = False
    for page in pages:
        page_label = page.label or str(page.number)
        for line in page.lines:
            if line.size <= footnote_size:
                _add_footnote(reading.footnotes, line.text)
                continue
            if line.size >= options.title_min_size and line.bold:
                continue
            if not reading.faqs and (m := _REVISED.search(line.text)):
                reading.revised = m.group(1)
                continue
            question = _QUESTION.match(line.text)
            if line.bold and not question:
                topic = f"{topic} {line.text}" if prev_was_topic and topic else line.text
                prev_was_topic = True
                continue
            prev_was_topic = False
            if question and _is_next_question(int(question.group(1)), reading.faqs):
                faq = _Faq(number=int(question.group(1)), topic=topic, question=[line], page_labels=[page_label])
                faq.question_done = _question_complete(faq.question)
                reading.faqs.append(faq)
                continue
            if not reading.faqs:
                reading.intro.append((line, page_label))
                continue
            current = reading.faqs[-1]
            current.page_labels.append(page_label)
            if current.question_done:
                current.answer.append((line, page_label))
            else:
                current.question.append(line)
                current.question_done = _question_complete(current.question)
    return reading


def _is_next_question(number: int, faqs: list[_Faq]) -> bool:
    if not faqs:
        return True
    return faqs[-1].question_done and 0 < number - faqs[-1].number <= _MAX_QUESTION_NUMBER_JUMP


def _question_complete(lines: list[Line]) -> bool:
    text = " ".join(ln.text for ln in lines)
    if _CITATION_END.search(text):
        return True
    # Fallback for a question without a citation: stop at the first "?" after a few lines.
    return len(lines) >= _MIN_LINES_FOR_UNCITED_QUESTION and text.rstrip().endswith("?")


def _add_footnote(footnotes: dict[str, str], text: str) -> None:
    m = _FOOTNOTE_START.match(text)
    if m:
        footnotes[m.group(1)] = m.group(2)
    elif footnotes:
        last = list(footnotes)[-1]
        footnotes[last] = f"{footnotes[last]} {text}"


def _attach_footnotes(text: str, footnotes: dict[str, str]) -> str:
    """Append footnote text when its marker is glued to the end of a word ("standards.1")."""
    notes = [
        f"[Footnote {n}: {body}]" for n, body in footnotes.items() if re.search(rf"[A-Za-z.,;)]{n}(?!\d)(?=\s|$)", text)
    ]
    return f"{text} {' '.join(notes)}" if notes else text
