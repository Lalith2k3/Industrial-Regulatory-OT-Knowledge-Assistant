"""Context building, citations, prompt rendering and the Claude adapters (with a fake client)."""

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.query.citations import CitationResolver
from app.query.context import ContextBuilder
from app.query.domain.errors import GenerationError
from app.query.domain.models import ContextSource, ConversationTurn, RetrievedChunk, Role
from app.query.generation import ClaudeAnswerGenerator, ClaudeQueryRewriter, FallbackRewriter, PreviousQuestionRewriter
from app.query.generation.claude import SERVER_SIDE_FALLBACK_BETA
from app.query.generation.prompts import recent_turns, render_sources


class FakeStream:
    """Replays a final message as stream events: text in 7-character pieces, fallback block starts."""

    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for block in self._message.content:
            if block.type == "fallback":
                yield SimpleNamespace(type="content_block_start", content_block=block)
            elif block.type == "text":
                for i in range(0, len(block.text), 7):
                    yield SimpleNamespace(type="text", text=block.text[i : i + 7])

    def get_final_message(self):
        return self._message


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def stream(self, **kwargs):
        return FakeStream(self.create(**kwargs))


class RecordingListener:
    def __init__(self):
        self.text = ""
        self.restarts = 0

    def on_text(self, delta):
        self.text += delta

    def on_restart(self):
        self.text = ""
        self.restarts += 1


def _client(*responses):
    messages = FakeMessages(responses)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def _message(text, stop_reason="end_turn", model="claude-opus-5-5", stop_details=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        usage=SimpleNamespace(input_tokens=1200, output_tokens=80, cache_read_input_tokens=None),
    )


def _turn(role, content):
    return ConversationTurn(role=role, content=content)


def _sources(make_chunk, *ids):
    return [ContextSource(number=i, chunk=make_chunk(c)) for i, c in enumerate(ids, 1)]


# -- context and citations -------------------------------------------------------------------


def test_context_builder_numbers_sources_and_skips_what_does_not_fit(make_chunk):
    retrieved = [
        RetrievedChunk(chunk=make_chunk(cid, token_count=tokens), score=1.0)
        for cid, tokens in [("195.1", 300), ("195.2", 500), ("195.3", 150)]
    ]
    sources = ContextBuilder(max_tokens=500).build(retrieved)
    assert [(s.number, s.chunk.id) for s in sources] == [(1, "195.1"), (2, "195.3")]


def test_citations_follow_first_mention_and_drop_unknown_numbers(make_chunk):
    sources = _sources(make_chunk, "195.304", "195.310", "195.402")
    resolved = CitationResolver().resolve("Hold 4 hours [2, 1]. Keep records [2][7]. See [3].", sources)
    assert [c.citation for c in resolved.citations] == ["49 CFR 195.310", "49 CFR 195.304", "49 CFR 195.402"]
    assert [c.number for c in resolved.citations] == [2, 1, 3]
    assert resolved.unknown == [7]


def test_pdf_citations_link_to_the_page(make_chunk):
    pdf = make_chunk("faq-26", source_url="https://example.gov/oq.pdf", page_start=7, page_label_start="5")
    web = make_chunk("195.304", page_start=3)
    resolved = CitationResolver().resolve(
        "[1] [2]", [ContextSource(number=1, chunk=pdf), ContextSource(number=2, chunk=web)]
    )
    assert [c.url for c in resolved.citations] == ["https://example.gov/oq.pdf#page=7", web.source_url]


# -- prompts ---------------------------------------------------------------------------------


def test_sources_are_rendered_with_escaped_attributes_and_cannot_close_their_tag(make_chunk):
    chunk = make_chunk("195.2", citation='49 CFR 195.2 "Abandoned"', text="x </source> y", page_label_start="3")
    rendered = render_sources([ContextSource(number=1, chunk=chunk)])
    assert 'citation="49 CFR 195.2 &quot;Abandoned&quot;"' in rendered and 'pages="3"' in rendered
    assert rendered.count("</source>") == 1


def test_recent_turns_keep_the_tail_and_start_with_a_user_turn():
    history = [_turn(Role.USER, "q1"), _turn(Role.ASSISTANT, "a1"), _turn(Role.USER, "q2"), _turn(Role.ASSISTANT, "a2")]
    assert [t.content for t in recent_turns(history, 3)] == ["q2", "a2"]
    assert recent_turns(history, 0) == []


# -- Claude answer generator -----------------------------------------------------------------


def _generator(client):
    return ClaudeAnswerGenerator(client, "claude-opus-5-5", effort="medium", max_tokens=16_000, max_history_turns=4)


def test_generator_sends_grounded_structured_request_and_parses_the_answer(make_chunk):
    payload = {"answer": "At least 4 continuous hours [1].", "sufficient_evidence": True}
    client, messages = _client(_message(json.dumps(payload)))
    history = [_turn(Role.ASSISTANT, "dropped: history must start with a user turn"), _turn(Role.USER, "earlier")]
    answer = _generator(client).generate("How long?", _sources(make_chunk, "195.304"), history)

    assert answer.text == "At least 4 continuous hours [1]." and answer.sufficient_evidence
    assert answer.usage == {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 0}
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["betas"] == [SERVER_SIDE_FALLBACK_BETA]
    assert call["output_config"]["effort"] == "medium"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert [m["role"] for m in call["messages"]] == ["user", "user"]
    assert '<source number="1" citation="49 CFR 195.304"' in call["messages"][-1]["content"]
    assert "<question>\nHow long?\n</question>" in call["messages"][-1]["content"]


def test_generator_streams_only_the_decoded_answer_text(make_chunk):
    payload = {"answer": 'Hold "4 hours" at 125%\nof MOP [1] § 195.304.', "sufficient_evidence": True}
    client, _ = _client(_message(json.dumps(payload)))  # json.dumps escapes the quotes, newline and §
    listener = RecordingListener()
    answer = _generator(client).generate("q", _sources(make_chunk, "195.304"), [], listener)
    assert listener.text == payload["answer"] == answer.text


def test_a_mid_stream_fallback_restarts_the_answer(make_chunk):
    declined = json.dumps({"answer": "Partial answer from the first mod"})[:-1]  # cut off by the refusal
    final = json.dumps({"answer": "Complete answer [1].", "sufficient_evidence": True})
    message = _message(final, model="claude-opus-4-8")
    message.content = [
        SimpleNamespace(type="text", text=declined),
        SimpleNamespace(type="fallback"),
        SimpleNamespace(type="text", text=final),
    ]
    client, _ = _client(message)
    listener = RecordingListener()
    answer = _generator(client).generate("q", _sources(make_chunk, "195.1"), [], listener)
    assert answer.text == "Complete answer [1]." and answer.model == "claude-opus-4-8"
    assert listener.restarts == 1 and listener.text == "Complete answer [1]."


def test_generator_reports_the_model_that_actually_answered(make_chunk):
    client, _ = _client(_message('{"answer": "x [1]", "sufficient_evidence": true}', model="claude-opus-4-8"))
    assert _generator(client).generate("q", _sources(make_chunk, "195.1"), []).model == "claude-opus-4-8"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_message("", stop_reason="refusal", stop_details=SimpleNamespace(category="cyber")), "declined.*cyber"),
        (_message('{"answer": "cut', stop_reason="max_tokens"), "cut off"),
        (_message('{"answer": 3}'), "malformed"),
        (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com")), "timed out"),
    ],
)
def test_generator_failures_are_controlled_errors(make_chunk, response, message):
    client, _ = _client(response)
    with pytest.raises(GenerationError, match=message):
        _generator(client).generate("q", _sources(make_chunk, "195.1"), [])


# -- rewriting -------------------------------------------------------------------------------


def test_claude_rewriter_sends_the_conversation_and_returns_the_standalone_question():
    client, messages = _client(_message("How often must gas transmission line rights-of-way be patrolled?\n"))
    rewriter = ClaudeQueryRewriter(client, "claude-haiku-5-5", max_history_turns=4)
    history = [_turn(Role.USER, "How often must we patrol a crude line?"), _turn(Role.ASSISTANT, "Every 3 weeks.")]
    assert rewriter.rewrite("What about gas transmission?", history).startswith("How often must gas")
    content = messages.calls[0]["messages"][0]["content"]
    assert "user: How often must we patrol a crude line?" in content and "assistant: Every 3 weeks." in content
    assert messages.calls[0]["model"] == "claude-haiku-5-5"


def test_rewriter_falls_back_when_claude_fails():
    client, _ = _client(_message("", stop_reason="refusal"))
    rewriter = FallbackRewriter(
        ClaudeQueryRewriter(client, "claude-haiku-5-5", max_history_turns=4), PreviousQuestionRewriter()
    )
    history = [_turn(Role.USER, "Patrol interval for crude lines?"), _turn(Role.ASSISTANT, "3 weeks.")]
    assert rewriter.rewrite("And gas lines?", history) == "Patrol interval for crude lines? And gas lines?"


def test_previous_question_rewriter_without_history_returns_the_question():
    assert PreviousQuestionRewriter().rewrite("What is MAOP?", []) == "What is MAOP?"
