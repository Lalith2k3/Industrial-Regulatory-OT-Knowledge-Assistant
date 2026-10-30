from itertools import pairwise

from app.ingestion.chunking import DEFAULT_POLICIES, ChunkPolicy
from app.ingestion.domain.models import DocType, Paragraph, ParsedDocument, Section, SectionKind


def _doc(*sections: Section) -> ParsedDocument:
    return ParsedDocument(source_id="test-195", parser="t", parser_version="0", sections=list(sections))


def _section(paragraphs: list[Paragraph], kind: SectionKind = SectionKind.SECTION) -> Section:
    return Section(
        key="195.452",
        kind=kind,
        heading="§ 195.452 Pipeline integrity management.",
        path=["Subpart F—Operation and Maintenance", "§ 195.452 Pipeline integrity management."],
        citation="49 CFR 195.452",
        paragraphs=paragraphs,
        metadata={"unit": "195.452"},
        own_cfr_ref="195.452",
        paragraph_citations=kind == SectionKind.SECTION,
    )


def _para(marker: str, words: int, starts: bool = True) -> Paragraph:
    return Paragraph(text=f"({marker}) " + " ".join(["word"] * words) + ".", top_marker=marker, starts_unit=starts)


def test_small_section_is_one_chunk_with_section_citation(mini_source, make_chunker):
    chunks = make_chunker(ChunkPolicy(100, 200)).chunk(mini_source, _doc(_section([_para("a", 50), _para("b", 50)])))
    assert len(chunks) == 1
    assert chunks[0].citation == "49 CFR 195.452"


def test_long_section_splits_at_top_level_paragraphs_with_range_citations(mini_source, make_chunker):
    # (a) fills a chunk; small (b)+(c) share one; (d) starts fresh because the chunk is already half full
    paragraphs = [_para("a", 80), _para("a", 80, starts=False), _para("b", 40), _para("c", 40), _para("d", 80)]
    chunks = make_chunker(ChunkPolicy(target_tokens=150, max_tokens=250)).chunk(mini_source, _doc(_section(paragraphs)))
    assert [c.citation for c in chunks] == ["49 CFR 195.452(a)", "49 CFR 195.452(b)–(c)", "49 CFR 195.452(d)"]
    assert all(c.metadata["parts"] == 3 for c in chunks)
    assert all(c.token_count <= 250 for c in chunks)


def test_paragraph_citations_only_when_the_parser_asks_for_them(mini_source, make_chunker):
    paragraphs = [_para("a", 80), _para("b", 80), _para("c", 80), _para("d", 80)]
    chunks = make_chunker(ChunkPolicy(150, 250)).chunk(mini_source, _doc(_section(paragraphs, SectionKind.APPENDIX)))
    assert len(chunks) > 1 and {c.citation for c in chunks} == {"49 CFR 195.452"}


def test_oversized_table_is_split_with_repeated_header(mini_source, make_chunker):
    rows = "\n".join(f"Row {i} | value {i} with some extra words" for i in range(60))
    table = Paragraph(text=f"Table: Limits\nName | Value\n{rows}", is_table=True)
    chunks = make_chunker(ChunkPolicy(120, 200)).chunk(mini_source, _doc(_section([table], SectionKind.APPENDIX)))
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text.startswith("Table: Limits\nName | Value\n")


def test_overlap_carries_trailing_sentences_within_a_section(mini_source, make_chunker):
    sentences = " ".join(f"Sentence number {i} is here." for i in range(40))
    chunker = make_chunker(ChunkPolicy(target_tokens=80, max_tokens=120, overlap_tokens=15))
    chunks = chunker.chunk(mini_source, _doc(_section([Paragraph(text=sentences)])))
    assert len(chunks) > 2
    for prev, nxt in pairwise(chunks):
        last_sentence = prev.text.split(". ")[-1]
        assert last_sentence.rstrip(".") in nxt.text


def test_embed_text_has_contextual_header_and_ids_are_deterministic(mini_source, make_chunker):
    chunker = make_chunker(DEFAULT_POLICIES[DocType.REGULATION])
    doc = _doc(_section([_para("a", 10)]))
    first, again = chunker.chunk(mini_source, doc)[0], chunker.chunk(mini_source, doc)[0]
    assert first.embed_text.startswith(
        "49 CFR Part 195 — Transportation of Hazardous Liquids by Pipeline\n"
        "Subpart F—Operation and Maintenance > § 195.452 Pipeline integrity management.\n\n"
    )
    assert first.id == again.id and first.content_sha256 == again.content_sha256


def test_references_from_text_and_from_the_parser_are_merged(mini_source, make_chunker):
    section = _section([Paragraph(text="See § 195.452(b), § 195.12 and control AC-17.")])
    section = section.model_copy(update={"cfr_refs": ["195.505"], "control_refs": ["SC-7"]})
    chunk = make_chunker(ChunkPolicy(100, 200)).chunk(mini_source, _doc(section))[0]
    assert chunk.cfr_refs == ["195.12", "195.505"]  # own section 195.452 is not a reference to itself
    assert chunk.control_refs == ["AC-17", "SC-7"]


def test_cfr_refs_from_parsed_xml(mini_source, make_chunker, ecfr_parser, fixtures_dir):
    parsed = ecfr_parser.parse(mini_source, fixtures_dir / "mini_part.xml")
    chunks = make_chunker(DEFAULT_POLICIES[DocType.REGULATION]).chunk(mini_source, parsed)
    chunk = next(c for c in chunks if c.section_key == "195.452")
    assert chunk.cfr_refs == ["195.12"]
    assert chunk.page_start is None  # XML sources have no pages; citation is the section
