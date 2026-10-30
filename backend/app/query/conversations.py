"""Server-side conversations (FR-08): stored turns become the history for follow-up questions.

Coordinates the ConversationRepository and the RagService; knows nothing about HTTP or storage.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import pairwise

from app.query.domain.errors import ConversationNotFoundError, QueryError
from app.query.domain.interfaces import AnswerListener, ConversationRepository
from app.query.domain.models import (
    AnswerStatus,
    Citation,
    Conversation,
    ConversationMessage,
    ConversationTurn,
    QueryRequest,
    Role,
    SearchFilters,
)
from app.query.service import RagService

TITLE_MAX_CHARS = 80
FAILED_ANSWER_PREFIX = "The answer could not be generated: "


class ConversationService:
    def __init__(self, repository: ConversationRepository, rag: RagService):
        self._repository = repository
        self._rag = rag

    def start(self) -> Conversation:
        return self._repository.create()

    def recent(self, limit: int) -> list[Conversation]:
        return self._repository.list_recent(limit)

    def get(self, conversation_id: str) -> tuple[Conversation, list[ConversationMessage]]:
        conversation = self._require(conversation_id)
        return conversation, self._repository.messages(conversation_id)

    def delete(self, conversation_id: str) -> None:
        if not self._repository.delete(conversation_id):
            raise ConversationNotFoundError(f"conversation {conversation_id} not found")

    def ask(
        self,
        conversation_id: str,
        question: str,
        filters: SearchFilters,
        listener: AnswerListener | None = None,
    ) -> tuple[ConversationMessage, ConversationMessage]:
        """Answer a question in a conversation; returns (question, answer) as stored.

        A failed answer is stored too, as an assistant message with status FAILED, before the
        QueryError is re-raised, so the conversation shows what happened after a reload.
        """
        conversation = self._require(conversation_id)
        history = _history(self._repository.messages(conversation_id))
        request = QueryRequest(question=question, history=history, filters=filters)
        user_message = _message(conversation_id, Role.USER, question)
        self._repository.add_message(user_message)
        if conversation.title is None:
            self._repository.set_title(conversation_id, _title(question))

        try:
            answer = self._rag.answer(request, listener)
        except QueryError as exc:
            self._repository.add_message(
                _message(conversation_id, Role.ASSISTANT, FAILED_ANSWER_PREFIX + str(exc), status=AnswerStatus.FAILED)
            )
            raise
        assistant_message = _message(
            conversation_id,
            Role.ASSISTANT,
            answer.text,
            status=answer.status,
            citations=answer.citations,
            query_id=answer.query_id,
            standalone_question=answer.standalone_question,
        )
        self._repository.add_message(assistant_message)
        return user_message, assistant_message

    def _require(self, conversation_id: str) -> Conversation:
        conversation = self._repository.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"conversation {conversation_id} not found")
        return conversation


def _history(messages: Sequence[ConversationMessage]) -> list[ConversationTurn]:
    """Question/answer pairs that were answered; a failed exchange is left out of the history."""
    turns: list[ConversationTurn] = []
    for question, answer in pairwise(messages):
        if question.role == Role.USER and answer.role == Role.ASSISTANT and answer.status != AnswerStatus.FAILED:
            turns += [
                ConversationTurn(role=Role.USER, content=question.content),
                ConversationTurn(role=Role.ASSISTANT, content=answer.content),
            ]
    return turns


def _message(
    conversation_id: str,
    role: Role,
    content: str,
    *,
    status: AnswerStatus | None = None,
    citations: Sequence[Citation] = (),
    query_id: str | None = None,
    standalone_question: str | None = None,
) -> ConversationMessage:
    return ConversationMessage(
        id=uuid.uuid4().hex,
        conversation_id=conversation_id,
        role=role,
        content=content,
        created_at=datetime.now(UTC),
        status=status,
        citations=list(citations),
        query_id=query_id,
        standalone_question=standalone_question,
    )


def _title(question: str) -> str:
    title = " ".join(question.split())
    return title if len(title) <= TITLE_MAX_CHARS else title[: TITLE_MAX_CHARS - 1].rstrip() + "…"
