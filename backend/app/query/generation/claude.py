"""Claude adapters for the AnswerGenerator and QueryRewriter interfaces (ADR-0003, ADR-0006).

Answers come back as structured output ({"answer", "sufficient_evidence"}), so FR-06 does not
depend on parsing prose. Every API failure, refusal or truncated response becomes a
GenerationError (NFR-04); the SDK itself retries 408/409/429/5xx and connection errors.
"""

import json
from collections.abc import Callable, Sequence
from typing import Literal

import anthropic
from anthropic.types.beta import BetaMessage, BetaMessageParam
from pydantic import BaseModel, ValidationError

from app.query.domain.errors import GenerationError
from app.query.domain.interfaces import TextListener
from app.query.domain.models import ContextSource, ConversationTurn, GeneratedAnswer, Role
from app.query.generation.prompts import (
    ANSWER_SCHEMA,
    ANSWER_SYSTEM_PROMPT,
    REWRITE_SYSTEM_PROMPT,
    recent_turns,
    render_answer_request,
    render_rewrite_request,
)
from app.query.generation.structured import JsonStringFieldStream

Effort = Literal["low", "medium", "high", "xhigh", "max"]

# If the answer model's safety classifiers decline a request (NIST OT-security material can look
# like a cyber request), the API re-runs it on Anthropic's recommended fallback model server-side.
SERVER_SIDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"
REWRITE_MAX_TOKENS = 2_048  # a one-line question plus the model's (low-effort) thinking


class _AnswerPayload(BaseModel):
    answer: str
    sufficient_evidence: bool


class ClaudeAnswerGenerator:
    def __init__(
        self,
        client: anthropic.Anthropic,
        model: str,
        *,
        effort: Effort,
        max_tokens: int,
        max_history_turns: int,
    ):
        self.model = model
        self._client = client
        self._effort: Effort = effort
        self._max_tokens = max_tokens
        self._max_history_turns = max_history_turns

    def generate(
        self,
        question: str,
        sources: Sequence[ContextSource],
        history: Sequence[ConversationTurn],
        listener: TextListener | None = None,
    ) -> GeneratedAnswer:
        messages = [_message(turn) for turn in recent_turns(history, self._max_history_turns)]
        messages.append({"role": "user", "content": render_answer_request(question, sources)})
        answer_text = JsonStringFieldStream("answer")

        def send() -> BetaMessage:
            # Always streamed: no HTTP timeout on long answers, and text can reach the listener early.
            with self._client.beta.messages.stream(
                model=self.model,
                max_tokens=self._max_tokens,
                system=ANSWER_SYSTEM_PROMPT,
                messages=messages,
                output_config={"effort": self._effort, "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
                betas=[SERVER_SIDE_FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                for event in stream:
                    if event.type == "content_block_start" and event.content_block.type == "fallback":
                        answer_text.reset()  # the fallback model writes the answer from the start
                        if listener:
                            listener.on_restart()
                    elif event.type == "text" and listener and (delta := answer_text.feed(event.text)):
                        listener.on_text(delta)
                return stream.get_final_message()

        message = _request(send, "answer generation")
        try:
            payload = _AnswerPayload.model_validate(json.loads(_text(message, "answer generation")))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise GenerationError(f"answer generation returned malformed output: {exc}") from exc
        return GeneratedAnswer(
            text=payload.answer.strip(),
            sufficient_evidence=payload.sufficient_evidence,
            model=message.model,  # the model that served the answer, which may be the fallback
            usage=_usage(message),
        )


class ClaudeQueryRewriter:
    def __init__(self, client: anthropic.Anthropic, model: str, *, max_history_turns: int):
        self.model = model
        self._client = client
        self._max_history_turns = max_history_turns

    def rewrite(self, question: str, history: Sequence[ConversationTurn]) -> str:
        turns = recent_turns(history, self._max_history_turns)
        if not turns:
            return question

        def send() -> BetaMessage:
            return self._client.beta.messages.create(
                model=self.model,
                max_tokens=REWRITE_MAX_TOKENS,
                system=REWRITE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": render_rewrite_request(question, turns)}],
                output_config={"effort": "low"},
            )

        rewritten = _text(_request(send, "query rewriting"), "query rewriting").strip()
        if not rewritten:
            raise GenerationError("query rewriting returned an empty question")
        return rewritten


def _message(turn: ConversationTurn) -> BetaMessageParam:
    if turn.role == Role.USER:
        return {"role": "user", "content": turn.content}
    return {"role": "assistant", "content": turn.content}


def _request(send: Callable[[], BetaMessage], purpose: str) -> BetaMessage:
    try:
        return send()
    except anthropic.APITimeoutError as exc:
        raise GenerationError(f"{purpose} timed out") from exc
    except anthropic.APIConnectionError as exc:
        raise GenerationError(f"{purpose}: cannot reach the Anthropic API: {exc}") from exc
    except anthropic.RateLimitError as exc:
        raise GenerationError(f"{purpose} is rate limited by the Anthropic API; retry shortly") from exc
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise GenerationError(f"{purpose}: the Anthropic API key was rejected ({exc.status_code})") from exc
    except anthropic.APIStatusError as exc:
        raise GenerationError(f"{purpose} failed with HTTP {exc.status_code}: {exc.message}") from exc


def _text(message: BetaMessage, purpose: str) -> str:
    """The response text, after making sure the model finished normally."""
    if message.stop_reason == "refusal":
        category = message.stop_details.category if message.stop_details else None
        raise GenerationError(f"{purpose} was declined by the model (category: {category or 'unspecified'})")
    if message.stop_reason == "max_tokens":
        raise GenerationError(f"{purpose} was cut off at the output token limit")
    # After a mid-stream fallback, text before the last fallback block came from a model that declined.
    blocks = message.content
    start = max((i + 1 for i, block in enumerate(blocks) if block.type == "fallback"), default=0)
    return "".join(block.text for block in blocks[start:] if block.type == "text")


def _usage(message: BetaMessage) -> dict[str, int]:
    usage = message.usage
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
    }
