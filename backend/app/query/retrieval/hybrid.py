"""Hybrid retrieval: every candidate source searches independently, results are fused by rank.

Adding a retrieval signal (a reranker's candidates, a different index) is a new CandidateSource
passed in by bootstrap.py; this class does not change.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.query.domain.interfaces import CandidateSource
from app.query.domain.models import KnowledgeChunk, RetrievedChunk, SearchQuery
from app.query.retrieval.fusion import RRF_K, reciprocal_rank_fusion


@dataclass(frozen=True)
class WeightedSource:
    source: CandidateSource
    weight: float = 1.0


class HybridRetriever:
    def __init__(self, sources: Sequence[WeightedSource], *, candidates: int, top_k: int, rrf_k: int = RRF_K):
        names = [s.source.name for s in sources]
        if len(set(names)) != len(names):
            raise ValueError(f"candidate source names must be unique: {names}")
        if not sources or candidates <= 0 or top_k <= 0:
            raise ValueError("a retriever needs at least one source and positive limits")
        self._sources = list(sources)
        self._candidates = candidates
        self._top_k = top_k
        self._rrf_k = rrf_k

    def retrieve(self, query: SearchQuery) -> list[RetrievedChunk]:
        rankings: dict[str, list[str]] = {}
        chunks: dict[str, KnowledgeChunk] = {}
        for weighted in self._sources:
            hits = weighted.source.search(query, self._candidates)
            rankings[weighted.source.name] = [h.chunk.id for h in hits]
            chunks.update((h.chunk.id, h.chunk) for h in hits)
        weights = {w.source.name: w.weight for w in self._sources}
        fused = reciprocal_rank_fusion(rankings, weights, self._rrf_k)
        return [RetrievedChunk(chunk=chunks[f.id], score=f.score, ranks=f.ranks) for f in fused[: self._top_k]]
