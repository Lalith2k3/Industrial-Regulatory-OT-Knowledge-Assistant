"""PostgreSQL implementations of the QueryLog, ConversationRepository and DocumentCatalog interfaces."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Engine, delete, insert, select, update

from app.ingestion.storage.schema import KnowledgeSchema
from app.query.domain.models import Conversation, ConversationMessage, DocumentInfo, QueryLogEntry
from app.query.storage.schema import QuerySchema


def create_query_tables(engine: Engine, schema: QuerySchema) -> None:
    """Create the query log and conversation tables if they do not exist."""
    with engine.begin() as conn:
        schema.metadata.create_all(conn)


class PostgresQueryLog:
    """QueryLog implementation: one row per question in `query_logs`."""

    def __init__(self, engine: Engine, schema: QuerySchema):
        self._engine = engine
        self._table = schema.query_logs

    def record(self, entry: QueryLogEntry) -> None:
        row = entry.model_dump(mode="json")
        row["created_at"] = entry.created_at  # keep the timezone-aware datetime for the timestamptz column
        with self._engine.begin() as conn:
            conn.execute(insert(self._table).values(row))


class PostgresConversationRepository:
    def __init__(self, engine: Engine, schema: QuerySchema):
        self._engine = engine
        self._conversations = schema.conversations
        self._messages = schema.messages

    def create(self) -> Conversation:
        now = datetime.now(UTC)
        conversation = Conversation(id=uuid.uuid4().hex, created_at=now, updated_at=now)
        with self._engine.begin() as conn:
            conn.execute(insert(self._conversations).values(conversation.model_dump()))
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        c = self._conversations.c
        with self._engine.connect() as conn:
            row = conn.execute(select(self._conversations).where(c.id == conversation_id)).mappings().first()
        return Conversation.model_validate(dict(row)) if row else None

    def list_recent(self, limit: int) -> list[Conversation]:
        c = self._conversations.c
        with self._engine.connect() as conn:
            rows = conn.execute(select(self._conversations).order_by(c.updated_at.desc()).limit(limit)).mappings()
            return [Conversation.model_validate(dict(r)) for r in rows]

    def set_title(self, conversation_id: str, title: str) -> None:
        c = self._conversations.c
        with self._engine.begin() as conn:
            conn.execute(update(self._conversations).where(c.id == conversation_id).values(title=title))

    def delete(self, conversation_id: str) -> bool:
        c = self._conversations.c
        with self._engine.begin() as conn:
            return conn.execute(delete(self._conversations).where(c.id == conversation_id)).rowcount > 0

    def messages(self, conversation_id: str) -> list[ConversationMessage]:
        m = self._messages.c
        stmt = select(self._messages).where(m.conversation_id == conversation_id).order_by(m.created_at, m.id)
        with self._engine.connect() as conn:
            return [ConversationMessage.model_validate(dict(r)) for r in conn.execute(stmt).mappings()]

    def add_message(self, message: ConversationMessage) -> None:
        row = message.model_dump(mode="json")
        row["created_at"] = message.created_at
        c = self._conversations.c
        with self._engine.begin() as conn:
            conn.execute(insert(self._messages).values(row))
            conn.execute(
                update(self._conversations).where(c.id == message.conversation_id).values(updated_at=message.created_at)
            )


class PostgresDocumentCatalog:
    """Reads the ingested documents (the knowledge base schema is owned by ingestion)."""

    def __init__(self, engine: Engine, schema: KnowledgeSchema):
        self._engine = engine
        self._documents = schema.documents

    def list_documents(self) -> list[DocumentInfo]:
        d = self._documents.c
        stmt = select(
            d.id, d.title, d.short_title, d.publisher, d.domain, d.doc_type, d.version, d.effective_date,
            d.source_url, d.chunk_count,
        ).order_by(d.domain, d.doc_type, d.id)  # fmt: skip
        with self._engine.connect() as conn:
            return [DocumentInfo.model_validate(dict(r)) for r in conn.execute(stmt).mappings()]
