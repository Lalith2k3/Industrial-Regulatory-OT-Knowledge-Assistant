from collections.abc import Sequence

from app.query.domain.models import ContextSource, RetrievedChunk


class ContextBuilder:
    """Chooses which retrieved chunks the model reads, best first, within a token budget.

    A chunk that would overflow the budget is skipped rather than truncated (a half passage could
    drop the exception that changes the answer); smaller chunks further down may still fit.
    """

    def __init__(self, max_tokens: int):
        if max_tokens <= 0:
            raise ValueError("the context budget must be positive")
        self._max_tokens = max_tokens

    def build(self, retrieved: Sequence[RetrievedChunk]) -> list[ContextSource]:
        sources: list[ContextSource] = []
        used = 0
        for item in retrieved:
            if used + item.chunk.token_count > self._max_tokens:
                continue
            sources.append(ContextSource(number=len(sources) + 1, chunk=item.chunk))
            used += item.chunk.token_count
        return sources
