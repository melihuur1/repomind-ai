"""The backend's check of what a model said about one source.

A source existing does not mean it supports a statement. So the model must also copy the
lines that prove its statement, and those lines are looked up in the source. Line numbers
are derived from where the quote was found: the model never gets to choose them, and what
is displayed is the text of the source, not the model's copy of it.

This proves that the quoted code exists and that the statement uses real names. It cannot
prove that the statement describes the quote correctly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Sequence

from repomind.evaluation.citations import CitationCheck
from repomind.qa.prompt import ContextSource

_MARKER = re.compile(r"\s*\[(?:S\d+|unverified)\]")
_FILE_REFERENCE = re.compile(
    r"[\w./\\-]+\.(?:java|tsx?|jsx?|mjs|cjs|mts|cts|py|go|rs|kt|sql|graphql|gql|proto|ya?ml|json|xml|html|css|sh|md)\b"
    r"(?::\d+(?:-\d+)?)?"
)
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_IDENTIFIER_PART = re.compile(r"[A-Za-z_$][\w$]{2,}")
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_ELISION = re.compile(r"\.\.\.|…")
_WORD_IN_QUOTE = re.compile(r"[^\W\d_]{3}")
_ESCAPED_WHITESPACE = re.compile(r"\\[ntr]")
_ESCAPED_CHARACTER = re.compile(r"\\([^\w\s])")
_QUOTE_WRAPPERS = "\"'`“”‘’"
_HTML_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_CODE = re.compile(r"</?code>", re.IGNORECASE)
_FRAGMENT_BOUNDARY = re.compile(r"\n|(?<=;)\s+")
# Local models trained largely on Chinese sometimes switch language mid-sentence.
_FOREIGN_SCRIPT = re.compile(r"[Ѐ-ӿ぀-ヿ㐀-鿿가-힯]")

_MIN_QUOTE_CHARACTERS = 15
_MIN_QUOTE_SIMILARITY = 0.9
_MAX_QUOTE_LINES = 15
_MAX_STATEMENT_CHARACTERS = 500

GitCheck = Callable[[ContextSource, int, int], CitationCheck]


@dataclass(frozen=True)
class VerifiedCitation:
    source_id: str
    repo: str
    commit_sha: str
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Evidence:
    statement: str
    quote: str
    citation: VerifiedCitation


@dataclass(frozen=True)
class Reading:
    """What became of one source. ``outcome``: supported, not_relevant, unsupported, invalid."""

    source: ContextSource
    outcome: str
    evidence: Evidence | None = None
    reason: str | None = None
    claimed_statement: str = ""
    claimed_quote: str = ""


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


def fabricated_references(text: str, sources: Sequence[ContextSource], question: str) -> list[str]:
    """Files and identifiers in the text that the model cannot have read.

    This catches invented names such as `schema.graphql` or `recomputeInvariants`.
    It cannot catch a false statement made only with real names.
    """
    known_paths = {source.chunk.path for source in sources}
    evidence = "\n".join(
        [question, *(f"{s.chunk.path}\n{s.chunk.symbol or ''}\n{s.chunk.content}" for s in sources)]
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


def _clean_quote(quote: str) -> str:
    """Undoes how models wrap a copied line: extra quotation marks, and double-escaped text."""
    text = _ESCAPED_WHITESPACE.sub("\n", quote)
    text = _ESCAPED_CHARACTER.sub(r"\1", text)
    text = _HTML_BREAK.sub("\n", text)
    text = _HTML_CODE.sub("", text).strip()
    while len(text) >= 2 and text[0] in _QUOTE_WRAPPERS and text[-1] in _QUOTE_WRAPPERS:
        text = text[1:-1].strip()
    return text


def _fragments_found(cleaned: str, haystack: str) -> tuple[int, int] | None:
    """Models skip or reorder lines they copy. Every line they kept must still be in the source."""
    fragments = [compact for compact in ("".join(f.split()) for f in _FRAGMENT_BOUNDARY.split(cleaned)) if compact]
    if len(fragments) < 2:
        return None
    spans = []
    for fragment in fragments:
        found = haystack.find(fragment)
        if found < 0:
            return None
        spans.append((found, found + len(fragment)))
    return min(start for start, _ in spans), max(end for _, end in spans)


def _near_match(needle: str, haystack: str) -> tuple[int, int] | None:
    """Where the needle almost occurs: a dropped parenthesis is still a copy, a translated comment is not."""
    longest = SequenceMatcher(None, needle, haystack, autojunk=False).find_longest_match(
        0, len(needle), 0, len(haystack)
    )
    if longest.size == 0:
        return None
    start = max(0, longest.b - longest.a)
    end = min(len(haystack), start + len(needle))
    if SequenceMatcher(None, needle, haystack[start:end], autojunk=False).ratio() < _MIN_QUOTE_SIMILARITY:
        return None
    return start, end


def locate_quote(quote: str, content: str, first_line: int) -> tuple[int, int] | None:
    """The lines of ``content`` that ``quote`` was copied from, or None if it was not copied.

    This only finds which lines the model points at. It may be forgiving, because what is
    displayed afterwards is always the text of the source, never the model's copy.
    """
    characters: list[str] = []
    lines: list[int] = []
    for number, line in enumerate(content.split("\n"), start=first_line):
        for character in line:
            if not character.isspace():
                characters.append(character)
                lines.append(number)
    haystack = "".join(characters)

    cleaned = _clean_quote(quote)
    # Models re-indent code, so whitespace never counts. "..." marks an omission.
    parts = [compact for compact in ("".join(p.split()) for p in _ELISION.split(cleaned)) if compact]
    if not parts:
        return None
    position = 0
    first: int | None = None
    last = 0
    for part in parts:
        found = haystack.find(part, position)
        if found < 0:
            break
        first = found if first is None else first
        last = position = found + len(part)
    else:
        return lines[first], lines[last - 1]

    # With omissions, each part must be exact: "value_12" and "value_16" are almost alike.
    if len(parts) > 1:
        return None
    span = _fragments_found(cleaned, haystack) or _near_match(parts[0], haystack)
    if span is None:
        return None
    return lines[span[0]], lines[span[1] - 1]


def verify_reading(raw_text: str, source: ContextSource, git_check: GitCheck, question: str = "") -> Reading:
    parsed = _parse(raw_text)
    if parsed is None or not isinstance(parsed.get("relevant", False), bool):
        return Reading(source, "invalid", reason="the model did not return the required JSON")
    if parsed.get("relevant") is not True:
        return Reading(source, "not_relevant")

    statement = parsed.get("statement")
    quote = parsed.get("quote")
    if not isinstance(statement, str) or not statement.strip() or not isinstance(quote, str):
        return Reading(source, "invalid", reason="the model's reply has no statement or no quote")
    statement = _MARKER.sub("", statement).strip()

    def unsupported(reason: str) -> Reading:
        return Reading(source, "unsupported", reason=reason, claimed_statement=statement, claimed_quote=quote)

    if len(statement) > _MAX_STATEMENT_CHARACTERS:
        return unsupported("the statement is too long to be one claim")
    if _FOREIGN_SCRIPT.search(statement) and not _FOREIGN_SCRIPT.search(f"{question}\n{source.chunk.content}"):
        return unsupported("the statement drifted into a language that is neither the question's nor the code's")
    if len("".join(quote.split())) < _MIN_QUOTE_CHARACTERS or not _WORD_IN_QUOTE.search(quote):
        return unsupported("the quote is too short to prove anything")

    chunk = source.chunk
    located = locate_quote(quote, chunk.content, chunk.start_line)
    if located is None:
        return unsupported("the quote does not occur in the source")
    start, end = located
    if end - start + 1 > _MAX_QUOTE_LINES:
        return unsupported("the quote is too long to point at the proof")

    fabricated = fabricated_references(statement, [source], question)
    if fabricated:
        return unsupported("the statement names things that are not in the source: " + ", ".join(fabricated))

    check = git_check(source, start, end)
    if not check.valid:
        return unsupported(check.reason or "the lines do not match the commit")

    lines = chunk.content.split("\n")[start - chunk.start_line : end - chunk.start_line + 1]
    return Reading(
        source,
        "supported",
        evidence=Evidence(
            statement,
            "\n".join(lines),
            VerifiedCitation(source.id, chunk.repo, chunk.commit_sha, chunk.path, start, end),
        ),
        claimed_statement=statement,
        claimed_quote=quote,
    )
