"""Interfaces (ports) the query service depends on. Concrete implementations live in embedding/,
retrieval/, generation/ and storage/ and are wired together in bootstrap.py, so the service never
imports Voyage, Anthropic or SQLAlchemy and can be unit-tested with in-memory fakes."""

from collections.abc import Sequence
from typing import Protocol

from app.query.domain.models import (
    AnswerStage,
    Citation,
    ContextSource,
    Conversation,
    ConversationMessage,
    ConversationTurn,
    DocumentInfo,
    GeneratedAnswer,
    QueryLogEntry,
    RetrievedChunk,
    ScoredChunk,
    SearchQuery,
)


class QueryEmbedder(Protocol):
    """Must use the same model and dimension as the stored chunks, with the provider's query mode."""

    @property
    def model(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...


class CandidateSource(Protocol):
    """One retrieval signal (vector similarity, full text, exact references...).

    Returns its best candidates, best first. A source with nothing to contribute (for example a
    reference search when the question names no section) returns an empty list.
    """

    @property
    def name(self) -> str: ...

    def search(self, query: SearchQuery, limit: int) -> list[ScoredChunk]: ...


class Retriever(Protocol):
    def retrieve(self, query: SearchQuery) -> list[RetrievedChunk]: ...


class QueryRewriter(Protocol):
    """Turns a follow-up ("what about gas lines?") into a standalone question for retrieval (FR-08)."""

    @property
    def model(self) -> str: ...

    def rewrite(self, question: str, history: Sequence[ConversationTurn]) -> str: ...


class TextListener(Protocol):
    """Receives answer text while it is generated."""

    def on_text(self, delta: str) -> None: ...

    def on_restart(self) -> None:
        """Discard the text received so far: the answer starts again (e.g. on a model fallback)."""


class AnswerListener(TextListener, Protocol):
    """Everything a caller can observe while a question is answered (used for streaming)."""

    def on_stage(self, stage: AnswerStage) -> None: ...

    def on_sources(self, sources: Sequence[Citation]) -> None:
        """The numbered passages the answer may cite, before generation starts."""


class AnswerGenerator(Protocol):
    @property
    def model(self) -> str: ...

    def generate(
        self,
        question: str,
        sources: Sequence[ContextSource],
        history: Sequence[ConversationTurn],
        listener: TextListener | None = None,
    ) -> GeneratedAnswer:
        """Answer only from `sources`, citing them as [number]. Raises GenerationError.

        If a listener is given, answer text is passed to it as it is generated.
        """


class QueryLog(Protocol):
    """Records every question with its retrieval and outcome (FR-10, NFR-07, US-08)."""

    def record(self, entry: QueryLogEntry) -> None: ...


class ConversationRepository(Protocol):
    """Server-side conversations (FR-08)."""

    def create(self) -> Conversation: ...

    def get(self, conversation_id: str) -> Conversation | None: ...

    def list_recent(self, limit: int) -> list[Conversation]:
        """Most recently updated first."""

    def set_title(self, conversation_id: str, title: str) -> None: ...

    def delete(self, conversation_id: str) -> bool:
        """False if there was no such conversation."""

    def messages(self, conversation_id: str) -> list[ConversationMessage]:
        """Oldest first."""

    def add_message(self, message: ConversationMessage) -> None:
        """Append a message and mark the conversation as updated."""


class DocumentCatalog(Protocol):
    def list_documents(self) -> list[DocumentInfo]: ...
