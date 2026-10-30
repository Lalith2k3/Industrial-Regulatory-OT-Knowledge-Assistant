from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import StreamingResponse

from app.api.schemas import AskRequest, ConversationDetail, ConversationList, Exchange, Health, SourceList
from app.api.streaming import stream_events
from app.query.bootstrap import ApiServices
from app.query.domain.interfaces import AnswerListener
from app.query.domain.models import Conversation

RECENT_CONVERSATIONS = 50

router = APIRouter(prefix="/api")


def _services(request: Request) -> ApiServices:
    services: ApiServices = request.app.state.services
    return services


Services = Annotated[ApiServices, Depends(_services)]


@router.get("/health")
def health(services: Services) -> Health:
    return Health(
        status="ok",
        answer_model=services.answer_model,
        embedding_model=services.embedding_model,
        offline_answers=services.offline_answers,
    )


@router.get("/sources")
def sources(services: Services) -> SourceList:
    """Approved, ingested documents, for the source filter (FR-07)."""
    return SourceList(documents=services.catalog.list_documents())


@router.get("/conversations")
def list_conversations(services: Services) -> ConversationList:
    return ConversationList(conversations=services.conversations.recent(RECENT_CONVERSATIONS))


@router.post("/conversations", status_code=status.HTTP_201_CREATED)
def create_conversation(services: Services) -> Conversation:
    return services.conversations.start()


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, services: Services) -> ConversationDetail:
    conversation, messages = services.conversations.get(conversation_id)
    return ConversationDetail(conversation=conversation, messages=messages)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: str, services: Services) -> Response:
    services.conversations.delete(conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/conversations/{conversation_id}/messages")
def ask(conversation_id: str, body: AskRequest, services: Services) -> StreamingResponse:
    """Answer a question in the conversation, streamed as server-sent events (see streaming.py)."""
    services.conversations.get(conversation_id)  # 404 now, not as an event after the stream started

    def run(listener: AnswerListener) -> Exchange:
        question, answer = services.conversations.ask(conversation_id, body.question, body.filters, listener)
        return Exchange(question=question, answer=answer)

    return StreamingResponse(
        stream_events(run),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
