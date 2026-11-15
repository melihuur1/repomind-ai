"""Where the lines a question needs are lost on the way to an answer. For a developer, on request.

`ask` goes through several stages, and a right source can drop out at each: it may not be in
the index, not among the chunks search returns, not among the few passages made from them, not
sent because the question was refused first, not chosen by the model, or chosen and then turned
down by the evidence rule. Knowing which one decides what is worth changing.

Nothing here runs unless it is called. It reads what an `AskResult` already holds, and asks the
index one question per expected file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from repomind.qa.engine import AskResult

# In the order a source meets them. The first one that fails is where it was lost.
NOT_INDEXED = "not in the index"
NOT_RETRIEVED = "not among the chunks search returned"
NOT_A_PASSAGE = "retrieved, and not made a passage"
NOT_ASKED = "a passage, and the question was refused before the model"
NOT_SENT = "a passage, and left out of the prompt for its size"
NOT_CHOSEN = "sent to the model, and not chosen"
TURNED_DOWN = "chosen, and turned down by the evidence rule"
ANSWERED = "shown as an answer"
WAITING = "a passage, and no model was asked"
STAGES = (NOT_INDEXED, NOT_RETRIEVED, NOT_A_PASSAGE, NOT_ASKED, NOT_SENT, NOT_CHOSEN, TURNED_DOWN, ANSWERED, WAITING)


@dataclass(frozen=True)
class Lines:
    path: str
    start_line: int
    end_line: int

    @classmethod
    def parse(cls, text: str) -> "Lines":
        """``src/Order.java:10-20`` or ``src/Order.java:10``."""
        path, _, span = text.rpartition(":")
        if not path or not span:
            raise ValueError(f"Expected path:start-end, got '{text}'.")
        start, _, end = span.partition("-")
        return cls(path, int(start), int(end or start))


@dataclass(frozen=True)
class PlaceTrace:
    """One place an answer needs, which any of ``accepted`` would show."""

    accepted: tuple[Lines, ...]
    stage: str
    retrieval_rank: int | None = None
    refused_because: str | None = None

    @property
    def lost(self) -> bool:
        return self.stage not in (ANSWERED, WAITING)


def _touches(item, lines: Lines) -> bool:
    return item.path == lines.path and item.start_line <= lines.end_line and lines.start_line <= item.end_line


def trace_place(result: AskResult, accepted: Sequence[Lines], indexed_chunks) -> PlaceTrace:
    """``indexed_chunks(path)`` gives the chunks the index holds for a file of the commit that was asked about."""
    accepted = tuple(accepted)

    def any_of(items) -> bool:
        return any(_touches(item, lines) for item in items for lines in accepted)

    rank = next((hit.rank for hit in result.hits if any(_touches(hit.chunk, lines) for lines in accepted)), None)
    if any_of(item.passage for item in result.evidence):
        return PlaceTrace(accepted, ANSWERED, rank)
    if any_of(result.review_candidates):
        return PlaceTrace(accepted, TURNED_DOWN, rank)
    if any_of(result.sent):
        return PlaceTrace(accepted, NOT_CHOSEN, rank)
    if any_of(result.passages):
        if result.status == "no_llm":
            return PlaceTrace(accepted, WAITING, rank)
        if result.sent:
            return PlaceTrace(accepted, NOT_SENT, rank)
        return PlaceTrace(accepted, NOT_ASKED, rank, result.stopped_by)
    if rank is not None:
        return PlaceTrace(accepted, NOT_A_PASSAGE, rank, result.stopped_by)
    if any(_touches(chunk, lines) for lines in accepted for chunk in indexed_chunks(lines.path)):
        return PlaceTrace(accepted, NOT_RETRIEVED, None, result.stopped_by)
    return PlaceTrace(accepted, NOT_INDEXED)


def trace(result: AskResult, places: Sequence[Sequence[Lines]], indexed_chunks) -> list[PlaceTrace]:
    """One trace for each place the answer needs."""
    return [trace_place(result, accepted, indexed_chunks) for accepted in places]


def describe(traces: Sequence[PlaceTrace]) -> list[str]:
    lines = []
    for number, item in enumerate(traces, start=1):
        where = " or ".join(f"{a.path}:{a.start_line}-{a.end_line}" for a in item.accepted)
        rank = f", retrieved at rank {item.retrieval_rank}" if item.retrieval_rank else ""
        why = f" ({item.refused_because})" if item.refused_because and item.stage in (NOT_ASKED, NOT_A_PASSAGE, NOT_RETRIEVED) else ""
        lines.append(f"place {number}: {item.stage}{why}{rank}\n         {where}")
    return lines
