"""Server-sent events for answers that are generated while the client watches.

The query service is synchronous. It runs on a worker thread and reports through an
AnswerListener that puts events on a queue; the response iterates the queue. If the client goes
away, the worker still finishes, so the answer is stored in the conversation either way.

Events: stage {stage} · sources {citations} · delta {text} · restart {} · done {...} · error {message}
"""

import json
import queue
import threading
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import structlog
from pydantic import BaseModel

from app.query.domain.errors import QueryError
from app.query.domain.interfaces import AnswerListener
from app.query.domain.models import AnswerStage, Citation

log = structlog.get_logger("api.streaming")

Event = tuple[str, dict[str, Any]]


class QueueListener:
    def __init__(self, events: "queue.Queue[Event | None]"):
        self._events = events

    def on_stage(self, stage: AnswerStage) -> None:
        self._events.put(("stage", {"stage": stage.value}))

    def on_sources(self, sources: Sequence[Citation]) -> None:
        self._events.put(("sources", {"citations": [c.model_dump(mode="json") for c in sources]}))

    def on_text(self, delta: str) -> None:
        self._events.put(("delta", {"text": delta}))

    def on_restart(self) -> None:
        self._events.put(("restart", {}))


def stream_events(run: Callable[[AnswerListener], BaseModel]) -> Iterator[str]:
    """Run `run` on a worker thread and yield its events as SSE frames, ending with done or error."""
    events: queue.Queue[Event | None] = queue.Queue()  # None ends the stream

    def work() -> None:
        try:
            result = run(QueueListener(events))
            events.put(("done", result.model_dump(mode="json")))
        except QueryError as exc:
            events.put(("error", {"message": str(exc)}))
        except Exception:
            log.exception("stream.failed")
            events.put(("error", {"message": "internal error while answering; see the server log"}))
        finally:
            events.put(None)

    threading.Thread(target=work, name="answer-stream", daemon=True).start()
    while (event := events.get()) is not None:
        yield sse_frame(*event)


def sse_frame(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
