"""Streaming pieces: the structured-output field extractor, the offline generator, SSE framing."""

import json

import pytest
from pydantic import BaseModel

from app.api.streaming import sse_frame, stream_events
from app.query.domain.errors import GenerationError
from app.query.domain.models import AnswerStage, ContextSource
from app.query.generation import ExtractiveAnswerGenerator
from app.query.generation.structured import JsonStringFieldStream


class Done(BaseModel):
    ok: bool = True


def _feed_in_pieces(text: str, size: int, field: str = "answer") -> str:
    stream = JsonStringFieldStream(field)
    return "".join(stream.feed(text[i : i + size]) for i in range(0, len(text), size))


@pytest.mark.parametrize("size", [1, 2, 3, 5, 64])
def test_field_value_is_decoded_however_the_json_is_split(size):
    value = 'Line one\nline "two" \\ § 195.452(c)–(f) \U0001f6e2 done'
    document = json.dumps({"answer": value, "sufficient_evidence": True})  # ASCII escapes: §, \ud83d...
    assert _feed_in_pieces(document, size) == value


def test_field_order_and_whitespace_do_not_matter():
    document = '{ "sufficient_evidence" : false ,\n  "answer" :  "Not covered [2]." }'
    assert _feed_in_pieces(document, 4) == "Not covered [2]."


def test_nothing_is_emitted_before_the_field_starts_and_reset_starts_over():
    stream = JsonStringFieldStream("answer")
    assert stream.feed('{"sufficient_evidence": true, "ans') == ""
    assert stream.feed('wer": "Hel') == "Hel"
    stream.reset()
    assert stream.feed('{"answer": "Again"}') == "Again"


def test_offline_generator_quotes_top_passages_with_citations_and_streams_them(make_chunk):
    sources = [
        ContextSource(number=1, chunk=make_chunk("195.412", text="Inspect at intervals not exceeding 3 weeks. More.")),
        ContextSource(number=2, chunk=make_chunk("195.413", text="Underwater inspection.")),
    ]
    deltas: list[str] = []
    listener = type("L", (), {"on_text": lambda self, d: deltas.append(d), "on_restart": lambda self: None})()
    answer = ExtractiveAnswerGenerator().generate("q", sources, [], listener)
    assert "**49 CFR 195.412:** Inspect at intervals not exceeding 3 weeks. More. [1]" in answer.text
    assert "[2]" in answer.text and answer.sufficient_evidence and answer.model == "local-extractive"
    assert "".join(deltas) == answer.text


def test_sse_frames_and_stream_lifecycle():
    assert sse_frame("delta", {"text": "§ 1"}) == 'event: delta\ndata: {"text": "§ 1"}\n\n'

    def run(listener):
        listener.on_stage(AnswerStage.GENERATING)
        listener.on_text("Hi")
        return Done()

    frames = list(stream_events(run))
    assert [f.split("\n")[0] for f in frames] == ["event: stage", "event: delta", "event: done"]

    def fail(listener):
        raise GenerationError("timed out")

    assert list(stream_events(fail)) == [sse_frame("error", {"message": "timed out"})]

    def crash(listener):
        raise RuntimeError("bug")

    (frame,) = stream_events(crash)
    assert frame.startswith("event: error") and "bug" not in frame  # internals stay in the server log
