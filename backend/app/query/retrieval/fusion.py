"""Weighted reciprocal rank fusion (Cormack et al., 2009).

Each signal contributes weight / (k + rank) for every item it ranks. Only ranks matter, so signals
with incomparable scores (cosine similarity, ts_rank, reference priority) fuse without calibration.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

RRF_K = 60  # the constant from the original paper; damps the advantage of the very first ranks


@dataclass(frozen=True)
class FusedItem:
    id: str
    score: float
    ranks: dict[str, int]  # signal name -> 1-based rank, only for signals that returned the item


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[str]], weights: Mapping[str, float], k: int = RRF_K
) -> list[FusedItem]:
    """Fuse ranked id lists (best first). Ties keep the order in which items were first seen."""
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for signal, ids in rankings.items():
        weight = weights.get(signal, 1.0)
        for rank, item_id in enumerate(dict.fromkeys(ids), start=1):  # a duplicate keeps its best rank
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank)
            ranks.setdefault(item_id, {})[signal] = rank
    ordered = sorted(scores, key=lambda i: scores[i], reverse=True)
    return [FusedItem(i, scores[i], ranks[i]) for i in ordered]
