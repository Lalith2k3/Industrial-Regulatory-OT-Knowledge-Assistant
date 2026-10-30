"""HTTP request and response bodies. Domain models are reused where they already fit."""

from pydantic import BaseModel, Field

from app.query.domain.models import (
    MAX_QUESTION_CHARS,
    Conversation,
    ConversationMessage,
    DocumentInfo,
    SearchFilters,
)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    filters: SearchFilters = Field(default_factory=SearchFilters)


class ConversationDetail(BaseModel):
    conversation: Conversation
    messages: list[ConversationMessage]


class Exchange(BaseModel):
    """The stored question and answer, sent as the final `done` event of an answer stream."""

    question: ConversationMessage
    answer: ConversationMessage


class ConversationList(BaseModel):
    conversations: list[Conversation]


class SourceList(BaseModel):
    documents: list[DocumentInfo]


class Health(BaseModel):
    status: str
    answer_model: str
    embedding_model: str
    offline_answers: bool  # answers are extractive passages, not written by a language model
