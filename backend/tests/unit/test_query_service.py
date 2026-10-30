"""RagService orchestration with in-memory fakes: no database, embedding provider or LLM involved."""

import pytest

from app.query.citations import CitationResolver
from app.query.context import ContextBuilder
from app.query.domain.errors import GenerationError
from app.query.domain.models import (
    AnswerStatus,
    ConversationTurn,
    GeneratedAnswer,
    QueryRequest,
    RetrievedChunk,
    Role,
    SearchFilters,
)
from app.query.embedding import HashQueryEmbedder
from app.query.retrieval import QueryAnalyzer
from app.query.service import NO_EVIDENCE_ANSWER, RagService, Searcher


class FakeRetriever:
    def __init__(self, chunks):
        self.chunks = chunks
        self.queries = []

    def retrieve(self, query):
        self.queries.append(query)
        return [RetrievedChunk(chunk=c, score=1.0 / i, ranks={"vector": i}) for i, c in enumerate(self.chunks, 1)]


class FakeRewriter:
    model = "fake-rewriter"

    def __init__(self):
        self.calls = 0

    def rewrite(self, question, history):
        self.calls += 1
        return f"standalone: {question}"


class FakeGenerator:
    model = "fake-llm"

    def __init__(self, text="Answer [1].", sufficient=True, error=None):
        self.text, self.sufficient, self.error = text, sufficient, error
        self.calls = []

    def generate(self, question, sources, history, listener=None):
        self.calls.append((question, [s.chunk.id for s in sources], list(history)))
        if self.error:
            raise self.error
        return GeneratedAnswer(text=self.text, sufficient_evidence=self.sufficient, model=self.model, usage={"x": 1})


class RecordingLog:
    def __init__(self, fail=False):
        self.entries = []
        self.fail = fail

    def record(self, entry):
        if self.fail:
            raise RuntimeError("database down")
        self.entries.append(entry)


@pytest.fixture
def make_service(references, make_chunk):
    def make(chunks=None, generator=None, query_log=None, max_tokens=1_000):
        retriever = FakeRetriever([make_chunk("195.304"), make_chunk("195.310")] if chunks is None else chunks)
        rewriter, generator, query_log = FakeRewriter(), generator or FakeGenerator(), query_log or RecordingLog()
        service = RagService(
            rewriter=rewriter,
            searcher=Searcher(HashQueryEmbedder(16), QueryAnalyzer(references.cfr, references.controls), retriever),
            context_builder=ContextBuilder(max_tokens),
            generator=generator,
            citations=CitationResolver(),
            query_log=query_log,
        )
        return service, retriever, rewriter, generator, query_log

    return make


def test_answers_with_citations_and_logs_the_retrieval(make_service):
    service, retriever, rewriter, generator, query_log = make_service()
    filters = SearchFilters(document_ids=["ecfr-49-cfr-195"])
    answer = service.answer(QueryRequest(question="Hydrotest per 195.304?", filters=filters))

    assert answer.status == AnswerStatus.ANSWERED and answer.text == "Answer [1]."
    assert [c.citation for c in answer.citations] == ["49 CFR 195.304"]
    assert rewriter.calls == 0 and answer.standalone_question == answer.question  # nothing to rewrite
    assert retriever.queries[0].cfr_refs == ["195.304"] and retriever.queries[0].filters == filters
    assert generator.calls[0][1] == ["195.304", "195.310"]

    (entry,) = query_log.entries
    assert entry.id == answer.query_id and entry.status == AnswerStatus.ANSWERED
    assert [(r.chunk_id, r.in_context, r.ranks) for r in entry.retrieved] == [
        ("195.304", True, {"vector": 1}),
        ("195.310", True, {"vector": 2}),
    ]
    assert entry.cited_chunk_ids == ["195.304"] and entry.usage == {"x": 1}
    assert entry.models == {"embedding": "local-hash", "generation": "fake-llm"}
    assert entry.timings.total_ms >= entry.timings.generation_ms >= 0
    assert answer.timings == entry.timings


def test_follow_ups_are_rewritten_for_retrieval_but_answered_in_conversation(make_service):
    service, retriever, _, generator, query_log = make_service()
    history = [
        ConversationTurn(role=Role.USER, content="Hydrotest pressure?"),
        ConversationTurn(role=Role.ASSISTANT, content="125% of MOP [1]."),
    ]
    answer = service.answer(QueryRequest(question="And the records?", history=history))
    assert retriever.queries[0].text == "standalone: And the records?"
    assert generator.calls[0][0] == "And the records?" and generator.calls[0][2] == history
    assert answer.standalone_question == "standalone: And the records?"
    assert query_log.entries[0].models["rewrite"] == "fake-rewriter"
    assert query_log.entries[0].history_turns == 2


def test_no_retrieved_passages_means_insufficient_evidence_without_calling_the_model(make_service):
    service, _, _, generator, query_log = make_service(chunks=[])
    answer = service.answer(QueryRequest(question="LNG thermal exclusion zones under Part 193?"))
    assert answer.status == AnswerStatus.INSUFFICIENT_EVIDENCE and answer.text == NO_EVIDENCE_ANSWER
    assert generator.calls == [] and query_log.entries[0].status == AnswerStatus.INSUFFICIENT_EVIDENCE


def test_the_model_can_declare_the_evidence_insufficient(make_service):
    generator = FakeGenerator(text="The sources do not cover Part 193.", sufficient=False)
    service, *_ = make_service(generator=generator)
    answer = service.answer(QueryRequest(question="Part 193 exclusion zones?"))
    assert answer.status == AnswerStatus.INSUFFICIENT_EVIDENCE and answer.citations == []


def test_context_budget_limits_what_the_model_reads(make_service, make_chunk):
    chunks = [make_chunk("195.1", token_count=700), make_chunk("195.2", token_count=700)]
    service, _, _, generator, query_log = make_service(chunks=chunks, max_tokens=1_000)
    service.answer(QueryRequest(question="q"))
    assert generator.calls[0][1] == ["195.1"]
    assert [r.in_context for r in query_log.entries[0].retrieved] == [True, False]


def test_generation_failure_is_logged_with_the_retrieval_and_re_raised(make_service):
    service, _, _, _, query_log = make_service(generator=FakeGenerator(error=GenerationError("timed out")))
    with pytest.raises(GenerationError):
        service.answer(QueryRequest(question="q"))
    (entry,) = query_log.entries
    assert entry.status == AnswerStatus.FAILED and entry.error == "timed out"
    assert [r.chunk_id for r in entry.retrieved] == ["195.304", "195.310"]


def test_a_failing_query_log_does_not_cost_the_answer(make_service):
    service, *_ = make_service(query_log=RecordingLog(fail=True))
    assert service.answer(QueryRequest(question="q")).status == AnswerStatus.ANSWERED


def test_requests_are_validated():
    with pytest.raises(ValueError):
        QueryRequest(question="")
    with pytest.raises(ValueError):
        QueryRequest(question="x" * 2_001)


class EventLog:
    def __init__(self):
        self.events = []

    def on_stage(self, stage):
        self.events.append(("stage", stage.value))

    def on_sources(self, sources):
        self.events.append(("sources", [c.number for c in sources]))

    def on_text(self, delta):
        self.events.append(("text", delta))

    def on_restart(self):
        self.events.append(("restart",))


class StreamingGenerator(FakeGenerator):
    def generate(self, question, sources, history, listener=None):
        if listener:
            listener.on_text("Answer ")
            listener.on_text("[1].")
        return super().generate(question, sources, history)


def test_listener_sees_stages_sources_and_text_in_order(make_service):
    service, *_ = make_service(generator=StreamingGenerator())
    listener = EventLog()
    history = [ConversationTurn(role=Role.USER, content="q0"), ConversationTurn(role=Role.ASSISTANT, content="a0")]
    service.answer(QueryRequest(question="q", history=history), listener)
    assert listener.events == [
        ("stage", "rewriting"),
        ("stage", "retrieving"),
        ("sources", [1, 2]),
        ("stage", "generating"),
        ("text", "Answer "),
        ("text", "[1]."),
    ]
