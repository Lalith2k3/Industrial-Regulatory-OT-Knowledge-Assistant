"""Conversation service and HTTP API with in-memory fakes (no database, no LLM)."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.query.bootstrap import ApiServices
from app.query.conversations import FAILED_ANSWER_PREFIX, ConversationService
from app.query.domain.errors import ConversationNotFoundError, GenerationError
from app.query.domain.models import (
    Answer,
    AnswerStage,
    AnswerStatus,
    Citation,
    Conversation,
    DocType,
    DocumentInfo,
    Domain,
    Role,
    SearchFilters,
)


class InMemoryConversations:
    def __init__(self):
        self.conversations: dict[str, Conversation] = {}
        self.stored: dict[str, list] = {}

    def create(self):
        now = datetime.now(UTC)
        conversation = Conversation(id=f"c{len(self.conversations) + 1}", created_at=now, updated_at=now)
        self.conversations[conversation.id] = conversation
        self.stored[conversation.id] = []
        return conversation

    def get(self, conversation_id):
        return self.conversations.get(conversation_id)

    def list_recent(self, limit):
        return sorted(self.conversations.values(), key=lambda c: c.updated_at, reverse=True)[:limit]

    def set_title(self, conversation_id, title):
        self.conversations[conversation_id] = self.conversations[conversation_id].model_copy(update={"title": title})

    def delete(self, conversation_id):
        self.stored.pop(conversation_id, None)
        return self.conversations.pop(conversation_id, None) is not None

    def messages(self, conversation_id):
        return list(self.stored[conversation_id])

    def add_message(self, message):
        self.stored[message.conversation_id].append(message)


def _citation():
    return Citation(
        number=1, chunk_id="c1", document_id="ecfr-49-cfr-195", document_title="Part 195", document_version="eCFR",
        citation="49 CFR 195.412(a)", heading="§ 195.412", kind="section", text="...",
    )  # fmt: skip


class FakeRag:
    """Stands in for RagService: echoes the request and drives the listener like the real one."""

    def __init__(self, error=None):
        self.requests = []
        self.error = error

    def answer(self, request, listener=None):
        self.requests.append(request)
        if listener:
            listener.on_stage(AnswerStage.RETRIEVING)
            listener.on_sources([_citation()])
            listener.on_text("Every 3 weeks ")
            listener.on_text("[1].")
        if self.error:
            raise self.error
        return Answer(
            query_id="q-1",
            question=request.question,
            standalone_question=f"standalone {request.question}",
            status=AnswerStatus.ANSWERED,
            text="Every 3 weeks [1].",
            citations=[_citation()],
        )


class FakeCatalog:
    def list_documents(self):
        return [
            DocumentInfo(
                id="nist-sp-800-82r3", title="Guide to OT Security", short_title="NIST SP 800-82r3", publisher="NIST",
                domain=Domain.OT_SECURITY, doc_type=DocType.STANDARD, version="Rev. 3", chunk_count=685,
            )
        ]  # fmt: skip


@pytest.fixture
def setup():
    repository, rag = InMemoryConversations(), FakeRag()
    service = ConversationService(repository, rag)
    services = ApiServices(service, FakeCatalog(), answer_model="local-extractive", embedding_model="local-hash")
    return TestClient(create_app(services)), repository, rag


def _events(response):
    """Parse an SSE body into (event, data) pairs."""
    events = []
    for frame in response.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.split("\n"))
        events.append((fields["event"], json.loads(fields["data"])))
    return events


# -- conversation service ------------------------------------------------------------------


def test_history_comes_from_stored_answered_turns_and_failures_are_kept_out():
    repository, rag = InMemoryConversations(), FakeRag()
    service = ConversationService(repository, rag)
    conversation = service.start()
    service.ask(conversation.id, "Patrol interval for crude lines?", SearchFilters())

    rag.error = GenerationError("timed out")
    with pytest.raises(GenerationError):
        service.ask(conversation.id, "This one fails", SearchFilters())
    rag.error = None
    service.ask(conversation.id, "And gas lines?", SearchFilters(domains=[Domain.PIPELINE_SAFETY]))

    last = rag.requests[-1]
    assert [(t.role, t.content) for t in last.history] == [
        (Role.USER, "Patrol interval for crude lines?"),
        (Role.ASSISTANT, "Every 3 weeks [1]."),
    ]
    assert last.filters.domains == [Domain.PIPELINE_SAFETY]
    _, messages = service.get(conversation.id)
    statuses = [m.status for m in messages if m.role == Role.ASSISTANT]
    assert statuses == [AnswerStatus.ANSWERED, AnswerStatus.FAILED, AnswerStatus.ANSWERED]
    assert messages[3].content == FAILED_ANSWER_PREFIX + "timed out"
    assert repository.conversations[conversation.id].title == "Patrol interval for crude lines?"


def test_unknown_conversations_are_not_found():
    service = ConversationService(InMemoryConversations(), FakeRag())
    with pytest.raises(ConversationNotFoundError):
        service.ask("missing", "q", SearchFilters())
    with pytest.raises(ConversationNotFoundError):
        service.delete("missing")


# -- HTTP API ------------------------------------------------------------------------------


def test_health_and_sources(setup):
    client, *_ = setup
    assert client.get("/api/health").json() == {
        "status": "ok", "answer_model": "local-extractive", "embedding_model": "local-hash", "offline_answers": True,
    }  # fmt: skip
    (document,) = client.get("/api/sources").json()["documents"]
    assert document["id"] == "nist-sp-800-82r3" and document["domain"] == "ot_security"


def test_conversation_lifecycle(setup):
    client, *_ = setup
    created = client.post("/api/conversations")
    assert created.status_code == 201 and created.json()["title"] is None
    conversation_id = created.json()["id"]
    assert [c["id"] for c in client.get("/api/conversations").json()["conversations"]] == [conversation_id]
    assert client.get(f"/api/conversations/{conversation_id}").json()["messages"] == []
    assert client.delete(f"/api/conversations/{conversation_id}").status_code == 204
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404
    assert client.delete(f"/api/conversations/{conversation_id}").status_code == 404


def test_asking_streams_events_and_stores_the_exchange(setup):
    client, _, rag = setup
    conversation_id = client.post("/api/conversations").json()["id"]
    body = {"question": "How often must we patrol?", "filters": {"document_ids": ["ecfr-49-cfr-195"]}}
    response = client.post(f"/api/conversations/{conversation_id}/messages", json=body)

    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response)
    assert [name for name, _ in events] == ["stage", "sources", "delta", "delta", "done"]
    assert events[1][1]["citations"][0]["citation"] == "49 CFR 195.412(a)"
    assert "".join(data["text"] for name, data in events if name == "delta") == "Every 3 weeks [1]."
    done = events[-1][1]
    assert done["question"]["content"] == body["question"] and done["answer"]["status"] == "answered"
    assert done["answer"]["query_id"] == "q-1" and done["answer"]["citations"][0]["number"] == 1
    assert rag.requests[0].filters.document_ids == ["ecfr-49-cfr-195"]

    detail = client.get(f"/api/conversations/{conversation_id}").json()
    assert detail["conversation"]["title"] == body["question"]
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_a_failed_answer_ends_the_stream_with_an_error_event(setup):
    client, _, rag = setup
    rag.error = GenerationError("the Anthropic API key was rejected (401)")
    conversation_id = client.post("/api/conversations").json()["id"]
    events = _events(client.post(f"/api/conversations/{conversation_id}/messages", json={"question": "q"}))
    assert events[-1] == ("error", {"message": "the Anthropic API key was rejected (401)"})
    stored = client.get(f"/api/conversations/{conversation_id}").json()["messages"]
    assert stored[-1]["status"] == "failed"


def test_bad_requests_are_rejected_before_streaming(setup):
    client, *_ = setup
    assert client.post("/api/conversations/missing/messages", json={"question": "q"}).status_code == 404
    conversation_id = client.post("/api/conversations").json()["id"]
    url = f"/api/conversations/{conversation_id}/messages"
    assert client.post(url, json={"question": ""}).status_code == 422
    assert client.post(url, json={"question": "q", "filters": {"domains": ["astrology"]}}).status_code == 422
