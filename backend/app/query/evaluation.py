"""Retrieval evaluation against the gold question set (data/evaluation/questions.json).

Measures whether the passages an answer needs are retrieved, independently of generation, so
fusion weights and candidate limits can be tuned without spending LLM tokens. A question's
expected source is found when any chunk of that section (document id + section key) is retrieved.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from app.query.domain.errors import ConfigurationError
from app.query.domain.interfaces import QueryRewriter
from app.query.domain.models import ConversationTurn, SearchFilters
from app.query.service import Searcher


class ExpectedSource(BaseModel):
    document_id: str
    section_key: str
    citation: str


class EvaluationQuestion(BaseModel):
    id: str
    category: str
    question: str
    conversation: list[ConversationTurn] = Field(default_factory=list)
    answerable: bool
    expected_sources: list[ExpectedSource] = Field(default_factory=list)


class _EvaluationSet(BaseModel):
    questions: list[EvaluationQuestion]


def load_evaluation_set(path: Path) -> list[EvaluationQuestion]:
    try:
        return _EvaluationSet.model_validate(json.loads(path.read_text(encoding="utf-8"))).questions
    except FileNotFoundError as exc:
        raise ConfigurationError(f"evaluation set not found: {path}") from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ConfigurationError(f"evaluation set {path} is invalid: {exc}") from exc


@dataclass(frozen=True)
class QuestionResult:
    question: EvaluationQuestion
    search_text: str
    ranks: dict[str, int | None]  # expected citation -> 1-based rank of its first chunk, None if missed

    def recall_at(self, k: int) -> float:
        return sum(r is not None and r <= k for r in self.ranks.values()) / len(self.ranks)

    @property
    def reciprocal_rank(self) -> float:
        found = [r for r in self.ranks.values() if r is not None]
        return 1.0 / min(found) if found else 0.0


@dataclass(frozen=True)
class Summary:
    questions: int
    recall: dict[int, float]  # k -> mean recall@k over questions
    mrr: float


class RetrievalEvaluator:
    def __init__(self, searcher: Searcher, rewriter: QueryRewriter):
        self._searcher = searcher
        self._rewriter = rewriter

    def evaluate(self, questions: Sequence[EvaluationQuestion]) -> list[QuestionResult]:
        """Unanswerable questions have no expected sources and are skipped."""
        scored = [q for q in questions if q.answerable and q.expected_sources]
        texts = [self._rewriter.rewrite(q.question, q.conversation) if q.conversation else q.question for q in scored]
        outcomes = self._searcher.search_many(texts, SearchFilters())
        results = []
        for question, text, outcome in zip(scored, texts, outcomes, strict=True):
            keys = [(c.chunk.document_id, c.chunk.section_key) for c in outcome.chunks]
            ranks = {
                e.citation: next((i for i, key in enumerate(keys, 1) if key == (e.document_id, e.section_key)), None)
                for e in question.expected_sources
            }
            results.append(QuestionResult(question, text, ranks))
        return results


def summarize(results: Sequence[QuestionResult], ks: Sequence[int]) -> Summary:
    n = len(results) or 1
    return Summary(
        questions=len(results),
        recall={k: sum(r.recall_at(k) for r in results) / n for k in ks},
        mrr=sum(r.reciprocal_rank for r in results) / n,
    )
