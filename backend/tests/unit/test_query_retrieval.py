"""Retrieval logic without a database: query analysis, rank fusion, the hybrid retriever."""

import pytest

from app.query.domain.models import ScoredChunk, SearchFilters, SearchQuery
from app.query.retrieval import HybridRetriever, QueryAnalyzer, WeightedSource
from app.query.retrieval.fusion import reciprocal_rank_fusion


class FixedSource:
    def __init__(self, name, chunks):
        self.name = name
        self._chunks = chunks
        self.limits: list[int] = []

    def search(self, query, limit):
        self.limits.append(limit)
        return [ScoredChunk(chunk=c, score=1.0) for c in self._chunks[:limit]]


def _query(**overrides) -> SearchQuery:
    return SearchQuery(**{"text": "q", "embedding": [0.0], **overrides})


@pytest.fixture
def analyzer(references) -> QueryAnalyzer:
    return QueryAnalyzer(references.cfr, references.controls)


def test_analyzer_extracts_cfr_sections_controls_and_document_identifiers(analyzer):
    query = analyzer.analyze(
        "Does 195.452(h) apply? See ac-17, interpretation PI-09-0005 and ADB -99-03; pressure 12.5 psi",
        [0.1],
        SearchFilters(),
    )
    assert query.cfr_refs == ["195.452"]
    assert query.control_refs == ["AC-17"]
    assert query.identifiers == ["AC-17", "PI-09-0005", "ADB-99-03"]


def test_analyzer_finds_nothing_in_plain_language(analyzer):
    query = analyzer.analyze("How often must we patrol a crude oil right-of-way?", [0.1], SearchFilters())
    assert query.cfr_refs == query.control_refs == query.identifiers == []


def test_fusion_rewards_agreement_between_signals():
    fused = reciprocal_rank_fusion(
        {"vector": ["a", "b", "c"], "reference": ["c", "d"]}, {"vector": 1.0, "reference": 1.0}, k=60
    )
    assert [f.id for f in fused] == ["c", "a", "b", "d"]  # c: 1/63 + 1/61 beats a: 1/61
    assert fused[0].ranks == {"vector": 3, "reference": 1}
    assert fused[1].ranks == {"vector": 1}


def test_fusion_weights_and_duplicates():
    fused = reciprocal_rank_fusion({"a": ["x", "x", "y"], "b": ["y"]}, {"a": 1.0, "b": 0.0})
    assert [f.id for f in fused] == ["x", "y"]  # weight 0 adds nothing; duplicate x keeps rank 1
    assert fused[1].ranks == {"a": 2, "b": 1}


def test_hybrid_retriever_fuses_sources_and_keeps_top_k(make_chunk):
    a, b, c = make_chunk("195.1"), make_chunk("195.2"), make_chunk("195.3")
    vector, empty = FixedSource("vector", [a, b, c]), FixedSource("identifier", [])
    reference = FixedSource("reference", [c])
    retriever = HybridRetriever(
        [WeightedSource(vector), WeightedSource(reference), WeightedSource(empty)], candidates=5, top_k=2
    )
    results = retriever.retrieve(_query())
    assert [r.chunk.id for r in results] == ["195.3", "195.1"]
    assert results[0].ranks == {"vector": 3, "reference": 1}
    assert vector.limits == [5] and empty.limits == [5]


def test_hybrid_retriever_rejects_ambiguous_configuration(make_chunk):
    with pytest.raises(ValueError, match="unique"):
        HybridRetriever(
            [WeightedSource(FixedSource("v", [])), WeightedSource(FixedSource("v", []))], candidates=5, top_k=2
        )
    with pytest.raises(ValueError):
        HybridRetriever([], candidates=5, top_k=2)
