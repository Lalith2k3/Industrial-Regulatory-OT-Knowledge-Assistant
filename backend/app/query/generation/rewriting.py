"""Query rewriters that need no language model, and a decorator that falls back to one."""

from collections.abc import Sequence

import structlog

from app.query.domain.errors import QueryError
from app.query.domain.interfaces import QueryRewriter
from app.query.domain.models import ConversationTurn, Role

log = structlog.get_logger("query.rewriting")


class PreviousQuestionRewriter:
    """Prefixes the previous user question: "How often must we patrol...? What about gas lines?"

    Crude but deterministic; keeps follow-ups retrievable when no LLM is available.
    """

    model = "previous-question"

    def rewrite(self, question: str, history: Sequence[ConversationTurn]) -> str:
        previous = next((t.content for t in reversed(history) if t.role == Role.USER), None)
        return f"{previous} {question}" if previous else question


class FallbackRewriter:
    """Uses `primary`; if it fails, logs and uses `fallback` so a follow-up is still answered (NFR-04)."""

    def __init__(self, primary: QueryRewriter, fallback: QueryRewriter):
        self._primary = primary
        self._fallback = fallback

    @property
    def model(self) -> str:
        return self._primary.model

    def rewrite(self, question: str, history: Sequence[ConversationTurn]) -> str:
        try:
            return self._primary.rewrite(question, history)
        except QueryError as exc:
            log.warning("query_rewrite.fallback", error=str(exc), fallback=self._fallback.model)
            return self._fallback.rewrite(question, history)
