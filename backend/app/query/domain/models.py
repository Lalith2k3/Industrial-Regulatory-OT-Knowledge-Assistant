"""Typed contracts between query stages.

QueryRequest -> (rewrite) -> SearchQuery -> Retriever -> RetrievedChunk -> ContextBuilder -> ContextSource
             -> AnswerGenerator -> GeneratedAnswer -> CitationResolver -> Answer

The knowledge base vocabulary (Domain, DocType, SectionKind) is shared with ingestion, which owns it.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.ingestion.domain.models import DocType, Domain, SectionKind

MAX_QUESTION_CHARS = 2_000  # a question, not a document; longer input is rejected (NFR-04)


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # FR-06: said so instead of guessing
    FAILED = "failed"  # written to the query log and the conversation; callers receive a QueryError


class AnswerStage(StrEnum):
    """Progress reported to listeners while a question is answered."""

    REWRITING = "rewriting"
    RETRIEVING = "retrieving"
    GENERATING = "generating"


# -- request ---------------------------------------------------------------------------------


class SearchFilters(BaseModel):
    """Restrict retrieval by source category (FR-07). Empty lists mean "no restriction"."""

    domains: list[Domain] = Field(default_factory=list)
    publishers: list[str] = Field(default_factory=list)
    doc_types: list[DocType] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)


class ConversationTurn(BaseModel):
    role: Role
    content: str


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    history: list[ConversationTurn] = Field(default_factory=list)  # earlier turns, oldest first (FR-08)
    filters: SearchFilters = Field(default_factory=SearchFilters)


# -- retrieval -------------------------------------------------------------------------------


class SearchQuery(BaseModel):
    """What every candidate source receives: the standalone question in all the forms they need."""

    text: str
    embedding: list[float]
    cfr_refs: list[str] = Field(default_factory=list)  # "195.452" mentioned in the question
    control_refs: list[str] = Field(default_factory=list)  # "AC-17" mentioned in the question
    identifiers: list[str] = Field(default_factory=list)  # document ids such as "PI-09-0005", "ADB-11-04"
    filters: SearchFilters = Field(default_factory=SearchFilters)


class KnowledgeChunk(BaseModel):
    """A stored chunk joined with the document facts a citation needs."""

    id: str
    document_id: str
    document_title: str
    document_short_title: str
    document_version: str
    publisher: str
    domain: Domain
    doc_type: DocType
    kind: SectionKind
    section_key: str
    citation: str
    heading: str
    section_path: list[str]
    text: str
    token_count: int
    page_start: int | None = None
    page_end: int | None = None
    page_label_start: str | None = None
    page_label_end: str | None = None
    source_url: str | None = None  # the section's own URL if it has one, else the document's
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScoredChunk(BaseModel):
    """One candidate from one candidate source, in that source's own score units."""

    chunk: KnowledgeChunk
    score: float


class RetrievedChunk(BaseModel):
    """A fused retrieval result. `ranks` keeps each signal's 1-based rank for the query log (US-08)."""

    chunk: KnowledgeChunk
    score: float
    ranks: dict[str, int] = Field(default_factory=dict)


# -- generation ------------------------------------------------------------------------------


class ContextSource(BaseModel):
    """A chunk as shown to the model, numbered so the answer can cite it as [number]."""

    number: int
    chunk: KnowledgeChunk


class GeneratedAnswer(BaseModel):
    text: str  # contains [n] markers referring to ContextSource.number
    sufficient_evidence: bool
    model: str
    usage: dict[str, int] = Field(default_factory=dict)


class Citation(BaseModel):
    """What the UI shows next to an answer and uses to open the source (FR-04, FR-05)."""

    number: int
    chunk_id: str
    document_id: str
    document_title: str
    document_version: str
    citation: str
    heading: str
    kind: SectionKind
    page_start: int | None = None
    page_end: int | None = None
    page_label_start: str | None = None
    page_label_end: str | None = None
    url: str | None = None
    text: str  # the passage itself, so the user can inspect it without leaving the answer


class Timings(BaseModel):
    """Milliseconds per stage (NFR-07). Stages that did not run stay at 0."""

    rewrite_ms: int = 0
    embedding_ms: int = 0
    retrieval_ms: int = 0
    generation_ms: int = 0
    total_ms: int = 0


class Answer(BaseModel):
    query_id: str
    question: str
    standalone_question: str
    status: AnswerStatus
    text: str
    citations: list[Citation] = Field(default_factory=list)
    timings: Timings = Field(default_factory=Timings)


# -- conversations and catalogue -------------------------------------------------------------


class Conversation(BaseModel):
    id: str
    title: str | None = None  # the first question, set when it is asked
    created_at: datetime
    updated_at: datetime


class ConversationMessage(BaseModel):
    id: str
    conversation_id: str
    role: Role
    content: str  # the question, the answer, or a user-facing error for a failed answer
    created_at: datetime
    status: AnswerStatus | None = None  # assistant messages only
    citations: list[Citation] = Field(default_factory=list)
    query_id: str | None = None  # links the answer to its query_logs row (US-08)
    standalone_question: str | None = None


class DocumentInfo(BaseModel):
    """An approved, ingested document, as the filter UI lists it (FR-07)."""

    id: str
    title: str
    short_title: str
    publisher: str
    domain: Domain
    doc_type: DocType
    version: str
    effective_date: date | None = None
    source_url: str | None = None
    chunk_count: int


# -- logging ---------------------------------------------------------------------------------


class RetrievalTrace(BaseModel):
    chunk_id: str
    citation: str
    score: float
    ranks: dict[str, int]
    in_context: bool


class QueryLogEntry(BaseModel):
    """One answered (or failed) question: what was asked, what was retrieved, what was cited (FR-10)."""

    id: str
    created_at: datetime
    question: str
    standalone_question: str | None = None
    history_turns: int = 0
    filters: SearchFilters = Field(default_factory=SearchFilters)
    status: AnswerStatus
    answer: str | None = None
    retrieved: list[RetrievalTrace] = Field(default_factory=list)
    cited_chunk_ids: list[str] = Field(default_factory=list)
    timings: Timings = Field(default_factory=Timings)
    models: dict[str, str] = Field(default_factory=dict)
    usage: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
