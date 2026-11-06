"""The backend's check of what a model chose.

The model points at passages by id. The lines shown are the backend's own, already compared
with Git, so the model cannot change a value, a file or a line number in them.

Its note is another matter. A sentence cannot be verified mechanically, so a note is always
shown apart from the code and labelled as the model's reading, and it is left out when it
names or counts something that the chosen lines do not contain. That catches invented names
and numbers. It cannot catch a false statement made with real ones, and nothing here proves
that the chosen lines really answer the question. A citation alone does not prove an answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Sequence

from repomind.qa.passages import Passage

MAX_CHOSEN = 2
_MAX_NOTE_CHARACTERS = 300

_MARKER = re.compile(r"\s*\[(?:[SP]\d+|unverified)\]")
_FILE_REFERENCE = re.compile(
    r"[\w./\\-]+\.(?:java|tsx?|jsx?|mjs|cjs|mts|cts|py|go|rs|kt|sql|graphql|gql|proto|ya?ml|json|xml|html|css|sh|md)\b"
    r"(?::\d+(?:-\d+)?)?"
)
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_IDENTIFIER_PART = re.compile(r"[A-Za-z_$][\w$]{2,}")
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_NUMBER = re.compile(r"(?<![\w.])\d(?:[\d_.,]*\d)?(?!\w)")
_NUMBER_SEPARATORS = re.compile(r"[_.,]")
# Local models trained largely on Chinese sometimes switch language mid-sentence.
_FOREIGN_SCRIPT = re.compile(r"[Ѐ-ӿ぀-ヿ㐀-鿿가-힯]")


@dataclass(frozen=True)
class VerifiedCitation:
    source_id: str
    repo: str
    commit_sha: str
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Selection:
    """What became of the model's reply. ``outcome``: chosen, none, invalid."""

    outcome: str
    chosen: tuple[Passage, ...] = ()
    note: str = ""
    note_problem: str | None = None
    reason: str | None = None


def _parse(text: str) -> dict | None:
    candidate = text.strip()
    match = _JSON_OBJECT.search(candidate)
    if match:
        candidate = match.group(0)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def fabricated_references(text: str, passages: Sequence[Passage], question: str) -> list[str]:
    """Files and identifiers in the text that these lines do not contain.

    This catches invented names such as `schema.graphql` or `recomputeInvariants`.
    It cannot catch a false statement made only with real names.
    """
    known_paths = {passage.chunk.path for passage in passages}
    evidence = "\n".join(
        [question, *(f"{p.chunk.path}\n{p.chunk.symbol or ''}\n{p.text}" for p in passages)]
    ).casefold()
    fabricated: list[str] = []
    for mention in dict.fromkeys(_FILE_REFERENCE.findall(text)):
        path = re.sub(r":\d+(?:-\d+)?$", "", mention).replace("\\", "/")
        if not any(known == path or known.endswith("/" + path) for known in known_paths):
            fabricated.append(mention)
    for identifier in dict.fromkeys(_BACKTICKED.findall(text)):
        for part in _IDENTIFIER_PART.findall(identifier):
            if part.casefold() not in evidence and part not in fabricated:
                fabricated.append(part)
    return fabricated


def _numbers(text: str) -> set[str]:
    """``1_000_000`` in code and ``1.000.000`` in a Turkish sentence are the same number."""
    return {_NUMBER_SEPARATORS.sub("", number) for number in _NUMBER.findall(text)}


def _note_problem(note: str, chosen: Sequence[Passage], question: str) -> str | None:
    lines = "\n".join(passage.text for passage in chosen)
    if len(note) > _MAX_NOTE_CHARACTERS:
        return "it is too long to be one sentence"
    if _FOREIGN_SCRIPT.search(note) and not _FOREIGN_SCRIPT.search(f"{question}\n{lines}"):
        return "it drifted into a language that is neither the question's nor the code's"
    fabricated = fabricated_references(note, chosen, question)
    if fabricated:
        return "it names things that are not in the chosen lines: " + ", ".join(fabricated)
    invented = _numbers(note) - _numbers(lines) - _numbers(question)
    if invented:
        return "it gives numbers that are not in the chosen lines: " + ", ".join(sorted(invented))
    return None


def verify_selection(raw_text: str, passages: Sequence[Passage], question: str = "") -> Selection:
    parsed = _parse(raw_text)
    if parsed is None:
        return Selection("invalid", reason="the model did not return the required JSON")
    wanted = parsed.get("chosen")
    if isinstance(wanted, str):
        wanted = [wanted]
    if not isinstance(wanted, list) or not all(isinstance(item, str) for item in wanted):
        return Selection("invalid", reason="the model's reply has no list of chosen passages")
    if not wanted:
        return Selection("none")

    by_id = {passage.id: passage for passage in passages}
    chosen = tuple(by_id[item] for item in dict.fromkeys(wanted) if item in by_id)[:MAX_CHOSEN]
    if not chosen:
        return Selection("invalid", reason="the model chose passages that were not offered")

    note = parsed.get("note")
    note = _MARKER.sub("", note).strip() if isinstance(note, str) else ""
    problem = _note_problem(note, chosen, question) if note else None
    return Selection("chosen", chosen, "" if problem else note, problem)
