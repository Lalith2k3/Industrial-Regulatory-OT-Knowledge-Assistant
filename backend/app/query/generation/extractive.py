from collections.abc import Sequence

from app.ingestion.text import split_sentences
from app.query.domain.interfaces import TextListener
from app.query.domain.models import ContextSource, ConversationTurn, GeneratedAnswer

LOCAL_EXTRACTIVE_MODEL = "local-extractive"
_PASSAGES = 3
_SENTENCES_PER_PASSAGE = 2

OFFLINE_NOTICE = (
    "**Offline mode:** no language model is configured, so this lists the most relevant passages "
    "instead of a written answer. Set `RAG_ANTHROPIC_API_KEY` to get answers."
)


class ExtractiveAnswerGenerator:
    """Offline counterpart of the Claude generator (development only, like the local-hash embedder).

    Quotes the opening sentences of the best passages with their citations, so the whole flow
    (retrieval, streaming, citations, conversations, UI) can run without an API key.
    """

    model = LOCAL_EXTRACTIVE_MODEL

    def generate(
        self,
        question: str,
        sources: Sequence[ContextSource],
        history: Sequence[ConversationTurn],
        listener: TextListener | None = None,
    ) -> GeneratedAnswer:
        lines = [OFFLINE_NOTICE, ""]
        for source in sources[:_PASSAGES]:
            sentences = split_sentences(source.chunk.text)[:_SENTENCES_PER_PASSAGE]
            excerpt = " ".join(" ".join(sentences).split())  # one line, so it stays inside its list item
            lines.append(f"- **{source.chunk.citation}:** {excerpt} [{source.number}]")
        text = "\n".join(lines)
        if listener:
            words = text.split(" ")
            for i, word in enumerate(words):
                listener.on_text(word if i == len(words) - 1 else word + " ")
        return GeneratedAnswer(text=text, sufficient_evidence=bool(sources), model=self.model)
