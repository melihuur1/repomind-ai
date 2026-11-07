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
# A chunk is one syntactic unit: a method, a SQL statement, a YAML block. Up to this size it is shown
# whole, so that a signature, its condition and a constraint are not cut apart. Windows cut the right
# lines for 43 of the 78 development questions whose lines they reached. Three quarters of chunks fit,
# and five of them still fit the prompt's budget.
UNIT_LINES = 16
# So that one file cannot fill every passage. Limits per chunk and on near duplicates were
# measured too, and they did not help.
MAX_PASSAGES_PER_FILE = 3
# The small model almost never declines, so refusing cannot be left to it. One shared word is
# what nearby code looks like. On an earlier question set, this rule took unanswerable questions
# refused from 3 of 10 to 7 of 10, and wrong sources from 11 of 26 to 7, while gold fell from 15 to 13.
MIN_SHARED_TERMS = 2
# Lower than the usual 60, so that a window one list ranks first can beat one all lists rank in the
# middle. Lower still lets more unanswerable questions through. See docs/evaluation.md.
_WINDOW_RRF_K = 30
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
        last = max(len(lines) - WINDOW_LINES, 0)
        # The last window is added on its own: with steps of 2, the last line of a chunk with an
        # even number of lines would be in no window, and could never be shown.
        for offset in dict.fromkeys((*range(0, last + 1, WINDOW_STEP), last)):
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
    # With its path and symbol, as chunks are embedded, because 3 lines alone say little.
    texts = [f"{w.chunk.path} {w.chunk.symbol or ''}\n{w.text}" for w in windows]
    matrix = np.stack([_unit(v) for v in embedder.embed_passages(texts)])
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


def _merged(ordered: Sequence[_Window], terms: set[str], limit: int, unit_lines: int = UNIT_LINES) -> list[Passage]:
    """Windows overlap, so each one either extends a passage it touches or adds what is new in it.

    A window inside a short chunk brings the whole chunk instead, once. The windows still decide the
    order, so this changes which lines a passage holds and not which code is chosen.
    No line is ever shown twice, because a repeated line would look like a second piece of evidence.
    """
    taken: list[list] = []  # chunk, start line, end line

    def overlaps_another(chunk: ChunkRow, start: int, end: int, ignoring: list) -> bool:
        return any(i is not ignoring and i[0].id == chunk.id and start <= i[2] and i[1] <= end for i in taken)

    def extended(chunk: ChunkRow, start: int, end: int) -> bool:
        for item in taken:
            if item[0].id != chunk.id or start > item[2] + 1 or item[1] > end + 1:
                continue
            wider = min(item[1], start), max(item[2], end)
            if wider[1] - wider[0] + 1 <= MAX_PASSAGE_LINES and not overlaps_another(chunk, *wider, item):
                item[1], item[2] = wider
                return True
        return False

    for window in ordered:
        chunk, start, end = window.chunk, window.start_line, window.end_line
        if chunk.end_line - chunk.start_line + 1 <= unit_lines:
            if any(item[0].id == chunk.id for item in taken):
                continue
            same_file = sum(item[0].path == chunk.path for item in taken)
            if len(taken) < limit and same_file < MAX_PASSAGES_PER_FILE:
                taken.append([chunk, chunk.start_line, chunk.end_line])
            continue
        if extended(chunk, start, end):
            continue
        for other, first, last in taken:
            if other.id == chunk.id and start <= last and first <= end:
                start, end = (last + 1, end) if start >= first else (start, first - 1)
        # What is left is often a line between two passages, which should not use up a passage of its own.
        if start > end or extended(chunk, start, end):
            continue
        same_file = sum(item[0].path == chunk.path for item in taken)
        if len(taken) == limit or same_file >= MAX_PASSAGES_PER_FILE:
            continue
        if terms & set(tokenize(_lines(chunk, start, end))):
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
    chunks: Sequence[ChunkRow],
    queries: Sequence[str],
    embedder: Embedder | None,
    limit: int = MAX_PASSAGES,
    more_terms: Sequence[str] = (),
) -> list[Passage]:
    """The best short passages, best first. A window that shares no term with the question is not evidence.

    Without any such window the result is empty, which the caller reports as not enough evidence.
    ``more_terms`` are other words the question may mean, such as the senses in the glossary.
    """
    terms = {term for query in (*queries, *more_terms) for term in tokenize(query)}
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
    ordered = [windows[item.key] for item in reciprocal_rank_fusion(lists, k=_WINDOW_RRF_K)]
    if embedder is not None:
        # Only this list can see a synonym, so when the question's words are not in the code, the
        # other two can bury the right window. Measured, this let in no more unanswerable questions.
        closest = windows[lists["meaning"][0]]
        ordered = [closest, *(window for window in ordered if window is not closest)]
    return _merged(ordered, terms, limit)
