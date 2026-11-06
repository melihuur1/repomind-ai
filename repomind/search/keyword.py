"""BM25 over code, with extra weight for symbol names and file paths.

The index is held in memory: at this corpus size that takes milliseconds to build,
and it keeps the ranking formula in plain, unit-testable Python.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Sequence

from repomind.search.tokenizer import tokenize
from repomind.store.database import ChunkRow


@dataclass(frozen=True)
class BM25Config:
    k1: float = 1.2
    b: float = 0.75
    symbol_weight: float = 3.0
    path_weight: float = 1.5


@dataclass(frozen=True)
class KeywordMatch:
    chunk: ChunkRow
    score: float
    matched_terms: dict[str, float]


class KeywordIndex:
    def __init__(self, chunks: Sequence[ChunkRow], config: BM25Config = BM25Config()) -> None:
        self.chunks = list(chunks)
        self.config = config
        self.postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        self.lengths: list[float] = []
        for position, chunk in enumerate(self.chunks):
            body = Counter(tokenize(chunk.content))
            symbol_text = " ".join([chunk.symbol or "", *chunk.symbols])
            symbol = Counter(tokenize(symbol_text))
            path = Counter(tokenize(PurePosixPath(chunk.path).stem))
            weighted: Counter[str] = Counter()
            for term, count in body.items():
                weighted[term] += count
            for term, count in symbol.items():
                weighted[term] += count * config.symbol_weight
            for term, count in path.items():
                weighted[term] += count * config.path_weight
            for term, frequency in weighted.items():
                self.postings[term].append((position, frequency))
            self.lengths.append(sum(weighted.values()))
        self.average_length = sum(self.lengths) / len(self.lengths) if self.lengths else 0.0

    def _idf(self, term: str) -> float:
        document_frequency = len(self.postings.get(term, ()))
        return math.log(1 + (len(self.chunks) - document_frequency + 0.5) / (document_frequency + 0.5))

    def search(self, query: str, limit: int) -> list[KeywordMatch]:
        k1, b = self.config.k1, self.config.b
        scores: dict[int, float] = defaultdict(float)
        matched: dict[int, dict[str, float]] = defaultdict(dict)
        for term in dict.fromkeys(tokenize(query)):
            idf = self._idf(term)
            for position, frequency in self.postings.get(term, ()):
                normalizer = k1 * (1 - b + b * self.lengths[position] / self.average_length)
                contribution = idf * frequency * (k1 + 1) / (frequency + normalizer)
                scores[position] += contribution
                matched[position][term] = contribution
        ranked = sorted(scores.items(), key=lambda item: (-item[1], self.chunks[item[0]].id))
        return [KeywordMatch(self.chunks[p], score, matched[p]) for p, score in ranked[:limit]]
