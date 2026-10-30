"""Query side against a real PostgreSQL + pgvector knowledge base (marker: db).

Ingests the mini Part 195 fixture with the offline hash embedder, then runs the real candidate
sources, the hybrid retriever and the query log. Generation uses a fake: no LLM is called.
"""

import shutil

import pytest
from sqlalchemy import text

from app.ingestion import bootstrap as ingestion_bootstrap
from app.ingestion.embedding import HashEmbedder
from app.ingestion.pipeline import IngestionPipeline, KnowledgeStore, RunOptions
from app.ingestion.quality import QualityGate
from app.ingestion.storage import FileArtifactSink, PostgresDocumentRepository, PostgresRunLog, build_schema
from app.query.citations import CitationResolver
from app.query.context import ContextBuilder
from app.query.conversations import ConversationService
from app.query.domain.models import AnswerStatus, GeneratedAnswer, QueryRequest, SearchFilters
from app.query.embedding import HashQueryEmbedder
from app.query.generation import PreviousQuestionRewriter
from app.query.retrieval import (
    HybridRetriever,
    PostgresIdentifierSearch,
    PostgresReferenceSearch,
    PostgresVectorSearch,
    QueryAnalyzer,
    WeightedSource,
)
from app.query.service import RagService, Searcher
from app.query.storage import (
    PostgresConversationRepository,
    PostgresDocumentCatalog,
    PostgresQueryLog,
    build_query_schema,
    create_query_tables,
)

pytestmark = pytest.mark.db
DIM = 64


class CitingGenerator:
    model = "fake-llm"

    def generate(self, question, sources, history, listener=None):
        return GeneratedAnswer(text=f"See {sources[0].chunk.citation} [1].", sufficient_evidence=True, model=self.model)


@pytest.fixture
def knowledge_base(tmp_path, fixtures_dir, engine, default_settings, references, mini_source):
    shutil.copy(fixtures_dir / "mini_part.xml", tmp_path / "mini_part.xml")
    schema = build_schema(DIM)
    pipeline = IngestionPipeline(
        data_dir=tmp_path,
        parsers=ingestion_bootstrap.build_parsers(default_settings, references),
        chunker=ingestion_bootstrap.build_chunker(references),
        quality_gate=QualityGate(),
        artifacts=FileArtifactSink(tmp_path / "processed"),
        store=KnowledgeStore(
            PostgresDocumentRepository(engine, schema), PostgresRunLog(engine, schema), HashEmbedder(DIM)
        ),
    )
    pipeline.run([mini_source], RunOptions())
    return schema


@pytest.fixture
def searcher(engine, knowledge_base, references):
    retriever = HybridRetriever(
        [
            WeightedSource(PostgresVectorSearch(engine, knowledge_base)),
            WeightedSource(PostgresReferenceSearch(engine, knowledge_base)),
            WeightedSource(PostgresIdentifierSearch(engine, knowledge_base)),
        ],
        candidates=20,
        top_k=5,
    )
    return Searcher(HashQueryEmbedder(DIM), QueryAnalyzer(references.cfr, references.controls), retriever)


def test_reference_vector_and_filtered_search(searcher):
    by_reference = searcher.search("What does 195.452 require?", SearchFilters()).chunks
    assert by_reference[0].chunk.section_key == "195.452" and "reference" in by_reference[0].ranks
    assert by_reference[0].chunk.document_title == "Transportation of Hazardous Liquids by Pipeline"
    assert by_reference[0].chunk.source_url  # section URL or the document's

    by_meaning = searcher.search("Abandoned means permanently removed from service.", SearchFilters()).chunks
    assert by_meaning[0].chunk.section_key == "195.2/def/abandoned"

    assert searcher.search("195.452", SearchFilters(document_ids=["another-document"])).chunks == []
    assert searcher.search("195.452", SearchFilters(domains=["ot_security"])).chunks == []


def test_identifier_search_runs_only_for_identifiers(engine, knowledge_base, references):
    source = PostgresIdentifierSearch(engine, knowledge_base)
    analyzer = QueryAnalyzer(references.cfr, references.controls)
    assert source.search(analyzer.analyze("plain words", [0.1] * DIM, SearchFilters()), 5) == []
    assert source.search(analyzer.analyze("PI-09-0005 and ADB-11-04", [0.1] * DIM, SearchFilters()), 5) == []


def test_answers_are_logged(engine, searcher):
    schema = build_query_schema()
    create_query_tables(engine, schema)
    query_log = PostgresQueryLog(engine, schema)
    service = RagService(
        rewriter=None,  # no history in these requests
        searcher=searcher,
        context_builder=ContextBuilder(6_000),
        generator=CitingGenerator(),
        citations=CitationResolver(),
        query_log=query_log,
    )
    answer = service.answer(QueryRequest(question="What does 195.452 require?"))
    assert answer.status == AnswerStatus.ANSWERED and answer.citations[0].citation.startswith("49 CFR 195.452")

    empty = service.answer(QueryRequest(question="195.452", filters=SearchFilters(document_ids=["none"])))
    assert empty.status == AnswerStatus.INSUFFICIENT_EVIDENCE

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, status, cited_chunk_ids, retrieved, timings, filters FROM query_logs ORDER BY created_at")
        ).all()
    expected = [(answer.query_id, "answered"), (empty.query_id, "insufficient_evidence")]
    assert [(r.id, r.status) for r in rows] == expected
    assert rows[0].cited_chunk_ids == [answer.citations[0].chunk_id]
    assert rows[0].retrieved[0]["in_context"] is True and "reference" in rows[0].retrieved[0]["ranks"]
    assert rows[0].timings["total_ms"] >= 0 and rows[1].filters["document_ids"] == ["none"]


def test_conversations_and_catalog(engine, searcher, knowledge_base):
    schema = build_query_schema()
    create_query_tables(engine, schema)
    rag = RagService(
        rewriter=PreviousQuestionRewriter(),
        searcher=searcher,
        context_builder=ContextBuilder(6_000),
        generator=CitingGenerator(),
        citations=CitationResolver(),
        query_log=PostgresQueryLog(engine, schema),
    )
    repository = PostgresConversationRepository(engine, schema)
    service = ConversationService(repository, rag)
    conversation = service.start()
    service.ask(conversation.id, "What does 195.452 require?", SearchFilters())
    _, follow_up = service.ask(conversation.id, "And for records?", SearchFilters())
    assert follow_up.standalone_question == "What does 195.452 require? And for records?"

    stored, messages = service.get(conversation.id)
    assert stored.title == "What does 195.452 require?" and stored.updated_at > stored.created_at
    assert [m.role.value for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1].status == AnswerStatus.ANSWERED and messages[1].query_id
    assert messages[1].citations[0].citation.startswith("49 CFR 195.452")
    assert [c.id for c in service.recent(10)] == [conversation.id]

    service.delete(conversation.id)
    assert repository.get(conversation.id) is None
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM conversation_messages")).scalar_one() == 0

    (document,) = PostgresDocumentCatalog(engine, knowledge_base).list_documents()
    assert document.id == "test-195" and document.short_title == "49 CFR Part 195" and document.chunk_count > 0
