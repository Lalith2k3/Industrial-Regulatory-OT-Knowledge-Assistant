from app.ingestion.chunking import ChunkPolicy, chunk_document
from app.ingestion.models import Paragraph, ParsedDocument, Section
from app.ingestion.parsers.ecfr import EcfrXmlParser


def _doc(*sections: Section) -> ParsedDocument:
    return ParsedDocument(source_id="test-195", parser="t", parser_version="0", sections=list(sections))


def _section(paragraphs: list[Paragraph], key: str = "195.452", kind: str = "section") -> Section:
    return Section(
        key=key,
        kind=kind,
        heading="§ 195.452 Pipeline integrity management.",
        path=["Subpart F—Operation and Maintenance", "§ 195.452 Pipeline integrity management."],
        citation="49 CFR 195.452",
        paragraphs=paragraphs,
        metadata={"unit": key, "section": key},
    )


def _para(marker: str, words: int, starts: bool = True) -> Paragraph:
    return Paragraph(text=f"({marker}) " + " ".join(["word"] * words) + ".", top_marker=marker, starts_unit=starts)


def test_small_section_is_one_chunk_with_section_citation(mini_source):
    chunks = chunk_document(mini_source, _doc(_section([_para("a", 50), _para("b", 50)])), ChunkPolicy(100, 200))
    assert len(chunks) == 1
    assert chunks[0].citation == "49 CFR 195.452"


def test_long_section_splits_at_top_level_paragraphs_with_range_citations(mini_source):
    # (a) fills a chunk; small (b)+(c) share one; (d) starts fresh because the chunk is already half full
    paragraphs = [_para("a", 80), _para("a", 80, starts=False), _para("b", 40), _para("c", 40), _para("d", 80)]
    chunks = chunk_document(mini_source, _doc(_section(paragraphs)), ChunkPolicy(target_tokens=150, max_tokens=250))
    assert [c.citation for c in chunks] == ["49 CFR 195.452(a)", "49 CFR 195.452(b)–(c)", "49 CFR 195.452(d)"]
    assert all(c.metadata["parts"] == 3 for c in chunks)
    assert all(c.token_count <= 250 for c in chunks)


def test_oversized_table_is_split_with_repeated_header(mini_source):
    rows = "\n".join(f"Row {i} | value {i} with some extra words" for i in range(60))
    table = Paragraph(text=f"Table: Limits\nName | Value\n{rows}", is_table=True)
    chunks = chunk_document(mini_source, _doc(_section([table], kind="appendix")), ChunkPolicy(120, 200))
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text.startswith("Table: Limits\nName | Value\n")


def test_overlap_carries_trailing_sentences_within_a_section(mini_source):
    sentences = " ".join(f"Sentence number {i} is here." for i in range(40))
    section = _section([Paragraph(text=sentences)], kind="section")
    chunks = chunk_document(mini_source, _doc(section), ChunkPolicy(target_tokens=80, max_tokens=120, overlap_tokens=15))
    assert len(chunks) > 2
    for prev, nxt in zip(chunks, chunks[1:]):
        last_sentence = prev.text.split(". ")[-1]
        assert last_sentence.rstrip(".") in nxt.text


def test_embed_text_has_contextual_header_and_ids_are_deterministic(mini_source):
    doc = _doc(_section([_para("a", 10)]))
    first = chunk_document(mini_source, doc)[0]
    again = chunk_document(mini_source, doc)[0]
    assert first.embed_text.startswith(
        "49 CFR Part 195 — Transportation of Hazardous Liquids by Pipeline\n"
        "Subpart F—Operation and Maintenance > § 195.452 Pipeline integrity management.\n\n"
    )
    assert first.id == again.id and first.content_sha256 == again.content_sha256


def test_cfr_refs_exclude_own_section_but_keep_others(mini_source, fixtures_dir):
    parsed = EcfrXmlParser().parse(mini_source, fixtures_dir / "mini_part.xml")
    chunk = next(c for c in chunk_document(mini_source, parsed) if c.section_key == "195.452")
    assert chunk.cfr_refs == ["195.12"]
    assert chunk.page_start is None  # XML sources have no pages; citation is the section
