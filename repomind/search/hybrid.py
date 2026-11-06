"""Reciprocal Rank Fusion: combines rankings without comparing their raw scores.

BM25 scores and cosine similarities live on unrelated scales. RRF uses only ranks:
``score(d) = sum(weight_r / (k + rank_r(d)))``, so every result can be explained as
"rank a in list A plus rank b in list B".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Mapping, Sequence


@dataclass(frozen=True)
class FusedItem:
    key: Hashable
    score: float
    ranks: dict[str, int]


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[Hashable]],
    k: int = 60,
    weights: Mapping[str, float] | None = None,
) -> list[FusedItem]:
    scores: dict[Hashable, float] = {}
    ranks: dict[Hashable, dict[str, int]] = {}
    for name, ranking in rankings.items():
        weight = 1.0 if weights is None else weights.get(name, 1.0)
        for position, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + weight / (k + position)
            ranks.setdefault(key, {})[name] = position
    ordered = sorted(scores, key=lambda key: (-scores[key], min(ranks[key].values()), str(key)))
    return [FusedItem(key, scores[key], ranks[key]) for key in ordered]
