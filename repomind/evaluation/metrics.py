"""Retrieval metrics as pure functions, so they can be checked against hand-computed values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

import numpy as np

# A hit must cover at least this many lines of a gold span (or all of a shorter span),
# so that a chunk touching only a boundary line does not count as finding the code.
MIN_OVERLAP_LINES = 3


class Located(Protocol):
    path: str
    start_line: int
    end_line: int


def overlaps(hit: Located, gold: Located) -> bool:
    if hit.path != gold.path:
        return False
    overlap = min(hit.end_line, gold.end_line) - max(hit.start_line, gold.start_line) + 1
    gold_length = gold.end_line - gold.start_line + 1
    return overlap >= min(MIN_OVERLAP_LINES, gold_length)


def same_file(hit: Located, gold: Located) -> bool:
    return hit.path == gold.path


def recall_at_k(hits: Sequence[Located], gold: Sequence[Located], k: int, match=overlaps) -> float:
    """Share of the gold spans found by the top ``k`` hits."""
    if not gold:
        raise ValueError("recall is undefined without gold spans")
    top = hits[:k]
    found = sum(1 for span in gold if any(match(hit, span) for hit in top))
    return found / len(gold)


def hit_at_k(hits: Sequence[Located], gold: Sequence[Located], k: int, match=overlaps) -> float:
    """1.0 if any of the top ``k`` hits matches any gold span."""
    return float(any(match(hit, span) for hit in hits[:k] for span in gold))


def reciprocal_rank(hits: Sequence[Located], gold: Sequence[Located], match=overlaps) -> float:
    for rank, hit in enumerate(hits, start=1):
        if any(match(hit, span) for span in gold):
            return 1.0 / rank
    return 0.0


@dataclass(frozen=True)
class ConfidenceInterval:
    mean: float
    low: float
    high: float


def bootstrap_ci(values: Sequence[float], iterations: int = 10_000, seed: int = 20261106) -> ConfidenceInterval:
    """Percentile bootstrap over questions. With ~30 questions the intervals are wide,
    and reporting them keeps small differences from being over-interpreted."""
    data = np.asarray(values, dtype=float)
    if data.size == 0:
        return ConfidenceInterval(0.0, 0.0, 0.0)
    generator = np.random.default_rng(seed)
    samples = generator.choice(data, size=(iterations, data.size), replace=True).mean(axis=1)
    return ConfidenceInterval(float(data.mean()), float(np.percentile(samples, 2.5)), float(np.percentile(samples, 97.5)))


def paired_difference_ci(a: Sequence[float], b: Sequence[float], **kwargs) -> ConfidenceInterval:
    """CI of mean(a - b) over the same questions. If it spans 0, the data does not show a difference."""
    if len(a) != len(b):
        raise ValueError("paired comparison needs equally long sequences")
    return bootstrap_ci(np.asarray(a, dtype=float) - np.asarray(b, dtype=float), **kwargs)
