"""Parser for PHMSA FAQ guidance PDFs: numbered questions grouped under bold topic headings.

    Contractor Qualification                                  <- topic (bold)
    2 – Are contractors required to have ...? (§§ 192.805, 195.505)   <- question + cited sections
    No. The operator must ensure ...                          <- answer
"""

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from app.ingestion.models import Paragraph, ParsedDocument, Section, SourceSpec
from app.ingestion.parsers.pdf_layout import Line, Margins, group_paragraphs, read_pages, typical_line_step
from app.ingestion.references import extract_cfr_refs
from app.ingestion.text import build_vocabulary, join_lines

_QUESTION = re.compile(r"^(\d{1,3})\s*[–—-]\s*(.+)$")
# Question ends with its citation parenthetical, which may nest one level: (§§ 192.805(b), (c); 195.505(b))
_CITATION_END = re.compile(r"\(\s*§[^()]*(?:\([^()]*\)[^()]*)*\)\s*$")
_REVISED = re.compile(r"Revised\s+([A-Z][a-z]+ \d{1,2}, \d{4})")


@dataclass
class _Faq:
    number: int
    topic: str | None
    question: list[Line] = field(default_factory=list)
    answer: list[tuple[Line, str | None]] = field(default_factory=list)
    question_done: bool = False
    labels: list[str | None] = field(default_factory=list)


class FaqPdfParser:
    name = "faq_pdf"
    version = "1.0.0"

    def parse(self, source: SourceSpec, path: Path) -> ParsedDocument:
        margins = Margins(header=source.options.get("header", 60), footer=source.options.get("footer", 760))
        pages = read_pages(path, margins)
        step = typical_line_step(pages)
        body_size = statistics.median(ln.size for p in pages for ln in p.lines)
        footnote_size = body_size - 1.5
        vocab = build_vocabulary(ln.text for p in pages for ln in p.lines)

        intro: list[tuple[Line, str | None]] = []
        faqs: list[_Faq] = []
        footnotes: dict[str, str] = {}
        topic: str | None = None
        revised: str | None = None
        prev_was_topic = False

        for page in pages:
            label = page.label or str(page.number)
            for line in page.lines:
                if line.size <= footnote_size:
                    _add_footnote(footnotes, line.text)
                    continue
                if line.size >= 13 and line.bold:
                    continue  # document title
                if (m := _REVISED.search(line.text)) and not faqs:
                    revised = m.group(1)
                    continue
                is_topic = line.bold and not _QUESTION.match(line.text)
                if is_topic:
                    topic = f"{topic} {line.text}" if prev_was_topic and topic else line.text
                    prev_was_topic = True
                    continue
                prev_was_topic = False
                q = _QUESTION.match(line.text)
                # Accept only the next few numbers, so a numbered list inside an answer is not a question.
                if q and (not faqs or (faqs[-1].question_done and 0 < int(q.group(1)) - faqs[-1].number <= 3)):
                    faqs.append(_Faq(number=int(q.group(1)), topic=topic, question=[line]))
                    faqs[-1].labels.append(label)
                    faqs[-1].question_done = _question_complete(faqs[-1].question)
                    continue
                if not faqs:
                    intro.append((line, label))
                    continue
                current = faqs[-1]
                current.labels.append(label)
                if not current.question_done:
                    current.question.append(line)
                    current.question_done = _question_complete(current.question)
                else:
                    current.answer.append((line, label))

        sections: list[Section] = []
        if intro:
            text = join_lines((ln.text for ln, _ in intro), vocab)
            text = _attach_footnotes(text, footnotes)
            sections.append(
                Section(
                    key="intro",
                    kind="intro",
                    heading="About this guidance",
                    path=["Introduction"],
                    citation=f"{source.short_title}, introduction",
                    paragraphs=[Paragraph(text=text, page=intro[0][0].page, page_label=intro[0][1])],
                    source_url=source.source_url,
                    metadata={"unit": "intro"},
                )
            )

        warnings: list[str] = []
        for faq in faqs:
            question = join_lines((ln.text for ln in faq.question), vocab)
            question = _QUESTION.sub(r"\2", question, count=1)
            blocks = group_paragraphs(faq.answer, step)
            answer = [b.to_paragraph(vocab) for b in blocks]
            if not answer:
                warnings.append(f"FAQ {faq.number} has no answer text")
            for p in answer:
                p.text = _attach_footnotes(p.text, footnotes)
            first = faq.question[0]
            paragraphs = [Paragraph(text=f"Q{faq.number}: {question}", page=first.page, page_label=faq.labels[0]), *answer]
            sections.append(
                Section(
                    key=f"faq-{faq.number}",
                    kind="faq",
                    heading=question,
                    path=[faq.topic or "General", f"Question {faq.number}"],
                    citation=f"{source.short_title}, Q{faq.number}",
                    paragraphs=paragraphs,
                    source_url=source.source_url,
                    metadata={
                        "unit": str(faq.number),
                        "faq_number": faq.number,
                        "topic": faq.topic,
                        "question": question,
                        "cited_sections": extract_cfr_refs(question),
                    },
                )
            )

        numbers = [f.number for f in faqs]
        if numbers != sorted(set(numbers)):
            warnings.append(f"FAQ numbers out of order or duplicated: {numbers}")
        return ParsedDocument(
            source_id=source.id,
            parser=self.name,
            parser_version=self.version,
            sections=sections,
            expected_keys=[str(i) for i in range(1, max(numbers, default=0) + 1)],
            warnings=warnings,
            metadata={"revised": revised, "faq_count": len(faqs), "footnotes": footnotes},
        )


def _question_complete(lines: list[Line]) -> bool:
    text = " ".join(ln.text for ln in lines)
    if _CITATION_END.search(text):
        return True
    # Fallback for a question without a citation: stop at the first "?" after a few lines.
    return len(lines) >= 4 and text.rstrip().endswith("?")


def _add_footnote(footnotes: dict[str, str], text: str) -> None:
    m = re.match(r"^(\d{1,2})\s+(.*)$", text)
    if m:
        footnotes[m.group(1)] = m.group(2)
    elif footnotes:
        last = list(footnotes)[-1]
        footnotes[last] = f"{footnotes[last]} {text}"


def _attach_footnotes(text: str, footnotes: dict[str, str]) -> str:
    """Append footnote text when its marker is glued to the end of a word ("standards.1")."""
    notes = [f"[Footnote {n}: {body}]" for n, body in footnotes.items() if re.search(rf"[A-Za-z.,;)]{n}(?!\d)(?=\s|$)", text)]
    return f"{text} {' '.join(notes)}" if notes else text
