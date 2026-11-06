"""The backend's check of what a model returned. No model output is trusted as a citation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Sequence

from repomind.evaluation.citations import CitationCheck
from repomind.qa.prompt import ContextSource

_MARKER = re.compile(r"\[(S\d+)\]")
_FILE_REFERENCE = re.compile(
    r"[\w./\\-]+\.(?:java|tsx?|jsx?|mjs|cjs|mts|cts|py|go|rs|kt|sql|graphql|gql|proto|ya?ml|json|xml|html|css|sh|md)\b"
    r"(?::\d+(?:-\d+)?)?"
)
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_IDENTIFIER_PART = re.compile(r"[A-Za-z_$][\w$]{2,}")
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

GitCheck = Callable[[ContextSource, int, int], CitationCheck]


@dataclass(frozen=True)
class VerifiedCitation:
    source_id: str
    repo: str
    commit_sha: str
    path: str
    start_line: int
    end_line: int


@dataclass
class VerifiedAnswer:
    status: str  # answered | insufficient | invalid_response
    answer: str = ""
    citations: list[VerifiedCitation] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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


def _line_number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _fabricated_references(answer: str, sources: Sequence[ContextSource], question: str) -> list[str]:
    """Files and identifiers in the answer that the model cannot have read.

    This catches invented names such as `schema.graphql` or `recomputeInvariants`.
    It cannot catch a false statement made only with real names.
    """
    known_paths = {source.chunk.path for source in sources}
    evidence = "\n".join(
        [question, *(f"{s.chunk.path}\n{s.chunk.symbol or ''}\n{s.chunk.content}" for s in sources)]
    ).casefold()
    fabricated: list[str] = []
    for mention in dict.fromkeys(_FILE_REFERENCE.findall(answer)):
        path = re.sub(r":\d+(?:-\d+)?$", "", mention).replace("\\", "/")
        if not any(known == path or known.endswith("/" + path) for known in known_paths):
            fabricated.append(mention)
    for identifier in dict.fromkeys(_BACKTICKED.findall(answer)):
        for part in _IDENTIFIER_PART.findall(identifier):
            if part.casefold() not in evidence and part not in fabricated:
                fabricated.append(part)
    return fabricated


def verify_answer(
    raw_text: str, sources: Sequence[ContextSource], git_check: GitCheck, question: str = ""
) -> VerifiedAnswer:
    parsed = _parse(raw_text)
    if parsed is None or not isinstance(parsed.get("answer", ""), str):
        return VerifiedAnswer("invalid_response", warnings=["The model did not return the required JSON."])
    if parsed.get("insufficient") is True:
        return VerifiedAnswer("insufficient")

    answer = parsed.get("answer", "").strip()
    if not answer:
        return VerifiedAnswer("insufficient")

    by_id = {source.id: source for source in sources}
    verified: dict[str, VerifiedCitation] = {}
    rejected: list[str] = []
    declared = parsed.get("citations")
    declared = declared if isinstance(declared, list) else []

    for item in declared:
        if not isinstance(item, dict):
            rejected.append(f"malformed citation: {str(item)[:80]}")
            continue
        source_id = str(item.get("source", ""))
        source = by_id.get(source_id)
        if source is None:
            rejected.append(f"{source_id or 'citation without a source'}: not among the supplied sources")
            continue
        start = _line_number(item.get("start_line"))
        end = _line_number(item.get("end_line"))
        chunk = source.chunk
        if start is None or end is None:
            start, end = chunk.start_line, chunk.end_line
        if not (chunk.start_line <= start <= end <= chunk.end_line):
            rejected.append(
                f"{source_id}: lines {start}-{end} are outside the source range {chunk.start_line}-{chunk.end_line}"
            )
            continue
        check = git_check(source, start, end)
        if not check.valid:
            rejected.append(f"{source_id}: {check.reason}")
            continue
        verified[source_id] = VerifiedCitation(source_id, chunk.repo, chunk.commit_sha, chunk.path, start, end)

    # A marker in the text also backs a claim, so it needs a verified source behind it.
    for source_id in dict.fromkeys(_MARKER.findall(answer)):
        if source_id in verified:
            continue
        source = by_id.get(source_id)
        if source is None:
            rejected.append(f"{source_id}: marker refers to a source that was not supplied")
            continue
        chunk = source.chunk
        check = git_check(source, chunk.start_line, chunk.end_line)
        if check.valid:
            verified[source_id] = VerifiedCitation(
                source_id, chunk.repo, chunk.commit_sha, chunk.path, chunk.start_line, chunk.end_line
            )
        else:
            rejected.append(f"{source_id}: {check.reason}")

    # A verified structured citation backs the answer even without an inline marker:
    # small local models often return one but not the other.
    if not verified:
        return VerifiedAnswer(
            "insufficient",
            rejected=rejected,
            warnings=["The model's answer had no verifiable citation behind it, so it was discarded."],
        )
    answer = _MARKER.sub(lambda m: m.group(0) if m.group(1) in verified else "[unverified]", answer)

    fabricated = _fabricated_references(answer, sources, question)
    if fabricated:
        return VerifiedAnswer(
            "insufficient",
            rejected=rejected,
            warnings=[
                "The model's answer was discarded because it refers to things that are not in "
                "the sources: " + ", ".join(fabricated) + "."
            ],
        )

    warnings: list[str] = []
    return VerifiedAnswer(
        "answered",
        answer=answer,
        citations=[verified[source_id] for source_id in sorted(verified, key=lambda s: int(s[1:]))],
        rejected=rejected,
        warnings=warnings,
    )
