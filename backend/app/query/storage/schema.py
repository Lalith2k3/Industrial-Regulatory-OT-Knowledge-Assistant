"""Query-side tables. Kept apart from the knowledge base schema, which ingestion owns and rebuilds."""

from dataclasses import dataclass

from sqlalchemy import ARRAY, Column, DateTime, ForeignKey, Index, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB


@dataclass(frozen=True)
class QuerySchema:
    metadata: MetaData
    query_logs: Table
    conversations: Table
    messages: Table


def build_query_schema() -> QuerySchema:
    metadata = MetaData()
    query_logs = Table(
        "query_logs",
        metadata,
        Column("id", Text, primary_key=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("question", Text, nullable=False),
        Column("standalone_question", Text),
        Column("history_turns", Integer, nullable=False),
        Column("filters", JSONB, nullable=False),
        Column("status", Text, nullable=False),
        Column("answer", Text),
        Column("retrieved", JSONB, nullable=False),  # [{chunk_id, citation, score, ranks, in_context}]
        Column("cited_chunk_ids", ARRAY(Text), nullable=False),
        Column("timings", JSONB, nullable=False),  # milliseconds per stage (NFR-07)
        Column("models", JSONB, nullable=False),
        Column("usage", JSONB, nullable=False),
        Column("error", Text),
        Index("ix_query_logs_created_at", "created_at"),
        Index("ix_query_logs_status", "status"),
    )
    conversations = Table(
        "conversations",
        metadata,
        Column("id", Text, primary_key=True),
        Column("title", Text),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Index("ix_conversations_updated_at", "updated_at"),
    )
    messages = Table(
        "conversation_messages",
        metadata,
        Column("id", Text, primary_key=True),
        Column("conversation_id", Text, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        Column("role", Text, nullable=False),
        Column("content", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("status", Text),
        Column("citations", JSONB, nullable=False),  # Citation objects as shown with the answer
        Column("query_id", Text),  # query_logs.id; not a foreign key, logs may be pruned independently
        Column("standalone_question", Text),
        Index("ix_conversation_messages_conversation", "conversation_id", "created_at"),
    )
    return QuerySchema(metadata, query_logs, conversations, messages)
