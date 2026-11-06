"""Finds the few lines, inside the chunks search returned, most likely to answer a question. No LLM.

A chunk runs up to 60 lines and most of them are beside the point, so asking a model about
whole chunks gives it plenty of real material for a plausible wrong sentence. Measured on two
question sets, this simple ranking finds the proof at least as often as the quotes small
models chose themselves. See docs/answer-plan.md.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from repomind.embeddings.provider import Embedder
from repomind.search.hybrid import reciprocal_rank_fusion
from repomind.search.tokenizer import tokenize
from repomind.store.database import ChunkRow

WINDOW_LINES = 3
WINDOW_STEP = 2
MAX_PASSAGES = 5
# Neighbouring windows merge, so that a passage reads as one piece. On the earlier question sets,
# 5 lines kept nearly all of what 7 or 9 reached, at about 21 lines per question in total.
MAX_PASSAGE_LINES = 5
# The small model almost never declines, so refusing cannot be left to it. One shared word is
# what nearby code looks like. On an earlier question set, this rule took unanswerable questions
# refused from 3 of 10 to 7 of 10, and wrong sources from 11 of 26 to 7, while gold fell from 15 to 13.
MIN_SHARED_TERMS = 2
_HAS_WORD = re.compile(r"[^\W\d_]{3}")


@dataclass(frozen=True)
class Passage:
    id: str
    chunk: ChunkRow
    start_line: int
    end_line: int
    text: str
    shared_terms: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return self.chunk.path


@dataclass(frozen=True)
class _Window:
    chunk_rank: int
    chunk: ChunkRow
    start_line: int
    end_line: int
    text: str
    tokens: frozenset[str]


def _windows(chunks: Sequence[ChunkRow]) -> list[_Window]:
    windows: list[_Window] = []
    seen: set[str] = set()
    for rank, chunk in enumerate(chunks):
        lines = chunk.content.split("\n")
        for offset in range(0, max(len(lines) - WINDOW_LINES + 1, 1), WINDOW_STEP):
            piece = lines[offset : offset + WINDOW_LINES]
            text = "\n".join(piece)
            if text in seen or not _HAS_WORD.search(text):
                continue
            seen.add(text)
            start = chunk.start_line + offset
            windows.append(_Window(rank, chunk, start, start + len(piece) - 1, text, frozenset(tokenize(text))))
    return windows


def _unit(vector) -> np.ndarray:
    array = np.asarray(vector, dtype=float)
    return array / (np.linalg.norm(array) or 1.0)


def _by_meaning(windows: Sequence[_Window], queries: Sequence[str], embedder: Embedder) -> list[int]:
    matrix = np.stack([_unit(v) for v in embedder.embed_passages([w.text for w in windows])])
    cosines = np.full(len(windows), -1.0)
    for query in queries:
        cosines = np.maximum(cosines, matrix @ _unit(embedder.embed_query(query)))
    return sorted(range(len(windows)), key=lambda i: (-cosines[i], i))


def _by_shared_terms(windows: Sequence[_Window], terms: set[str]) -> list[int]:
    """A term in few windows says more than one in many, as in BM25."""
    frequency = {term: sum(term in window.tokens for window in windows) for term in terms}
    rarity = {term: math.log(1 + len(windows) / (1 + count)) for term, count in frequency.items()}
    score = [sum(rarity[term] for term in terms & window.tokens) for window in windows]
    return sorted(range(len(windows)), key=lambda i: (-score[i], i))


def _lines(chunk: ChunkRow, start: int, end: int) -> str:
    return "\n".join(chunk.content.split("\n")[start - chunk.start_line : end - chunk.start_line + 1])


def _merged(ordered: Sequence[_Window], terms: set[str], limit: int) -> list[Passage]:
    """Windows overlap, so each one either extends a passage it touches or adds what is new in it.

    No line is ever shown twice, because a repeated line would look like a second piece of evidence.
    """
    taken: list[list] = []  # chunk, start line, end line

    def overlaps_another(chunk: ChunkRow, start: int, end: int, ignoring: list) -> bool:
        return any(i is not ignoring and i[0].id == chunk.id and start <= i[2] and i[1] <= end for i in taken)

    for window in ordered:
        chunk, start, end = window.chunk, window.start_line, window.end_line
        for item in taken:
            if item[0].id != chunk.id or start > item[2] + 1 or item[1] > end + 1:
                continue
            wider = min(item[1], start), max(item[2], end)
            if wider[1] - wider[0] + 1 <= MAX_PASSAGE_LINES and not overlaps_another(chunk, *wider, item):
                item[1], item[2] = wider
                break
        else:
            if len(taken) == limit:
                continue
            for other, first, last in taken:
                if other.id == chunk.id and start <= last and first <= end:
                    start, end = (last + 1, end) if start >= first else (start, first - 1)
            if start <= end and terms & set(tokenize(_lines(chunk, start, end))):
                taken.append([chunk, start, end])
    return [
        Passage(f"P{number}", chunk, start, end, _lines(chunk, start, end), tuple(sorted(terms & set(tokenize(_lines(chunk, start, end))))))
        for number, (chunk, start, end) in enumerate(taken, start=1)
    ]


def is_evidence(passage: Passage, wording: str) -> bool:
    """Whether these lines mention enough of the question to count as an answer, rather than the closest lines."""
    terms = set(tokenize(wording))
    return len(terms & set(tokenize(passage.text))) >= min(MIN_SHARED_TERMS, len(terms))


def select_passages(
    chunks: Sequence[ChunkRow], queries: Sequence[str], embedder: Embedder | None, limit: int = MAX_PASSAGES
) -> list[Passage]:
    """The best short passages, best first. A window that shares no term with the question is not evidence.

    Without any such window the result is empty, which the caller reports as not enough evidence.
    """
    terms = {term for query in queries for term in tokenize(query)}
    windows = [w for w in _windows(chunks) if terms & w.tokens]
    if not windows:
        return []
    lists = {
        "terms": _by_shared_terms(windows, terms),
        # Search already weighed the whole chunk, which a 3-line window cannot see.
        "search": sorted(range(len(windows)), key=lambda i: (windows[i].chunk_rank, i)),
    }
    if embedder is not None:
        lists["meaning"] = _by_meaning(windows, queries, embedder)
    ordered = [windows[item.key] for item in reciprocal_rank_fusion(lists, k=60)]
    return _merged(ordered, terms, limit)
