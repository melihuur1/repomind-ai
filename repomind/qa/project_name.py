"""A question can name the repository it is about, which says nothing about where in it to look.

Asked inside one repository, "How does ShopGuard prevent negative stock?" made search favour whatever
was named after the project, and those lines passed the evidence rule on the project's name alone. So a
name standing alone in a question leaves the search, and it can no longer vote for lines.

It stays whenever it may be what is being asked about:
- when it is part of an identifier, a config key, a path or a quoted term;
- when too little of the question is left without it.

One passage seat also goes to lines that name the project, found by the question as asked, so that a
question really about its configuration or its main class can still see them.

Nothing here knows any repository's name, and the model still reads the whole question. The comparison
behind this, and the case it still gets wrong, are in docs/architecture.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Sequence

from repomind.qa.passages import MAX_PASSAGES, Passage, is_evidence
from repomind.search.tokenizer import tokenize

# Fewer words than this left over, and the name is what the question is about: "What is ShopGuard?"
MIN_WORDS_LEFT = 2
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class Wording:
    question: str
    translated: str | None = None
    second: str | None = None


def _name(repo: str) -> str:
    return re.escape(repo.split("/")[-1])


def _standing_alone(repo: str) -> re.Pattern:
    # Not inside `shopguard.security`, SHOPGUARD_DB_PORT, com.shopguard, shopguard-db, a path or quotes.
    # A suffix after an apostrophe goes with the name: ShopGuard'ın, ShopGuard's.
    return re.compile(
        rf"(?<![\w.\-/`'\"“‘@$:#]){_name(repo)}(?:['’][^\W\d_]+)?(?![\w\-/`\"”]|[.:]\w)",
        re.IGNORECASE,
    )


def stands_alone(repo: str, question: str) -> bool:
    return _standing_alone(repo).search(question) is not None


def without_project_name(repo: str, asked: Wording) -> Wording | None:
    """The wordings to search with, or None when the name has to stay.

    The question as asked decides alone, because a translator can break a name apart.
    """
    pattern = _standing_alone(repo)
    if not pattern.search(asked.question):
        return None
    # Also named in another form, such as `shopguard` or ShopGuardProperties: that is the topic.
    if re.search(_name(repo), pattern.sub(" ", asked.question), re.IGNORECASE):
        return None

    def leave_out(text: str | None) -> str | None:
        return _SPACES.sub(" ", pattern.sub(" ", text)).strip() if text else text

    searched = Wording(leave_out(asked.question), leave_out(asked.translated), leave_out(asked.second))
    for before, after in ((asked.question, searched.question), (asked.translated, searched.translated), (asked.second, searched.second)):
        if before and after != before and len(set(tokenize(after))) < MIN_WORDS_LEFT:
            return None
    return searched


def names_the_project(repo: str, text: str) -> bool:
    """As `name: shopguard` or ShopGuardProperties do. Not `import com.shopguard.x`, which says nothing."""
    return re.search(rf"(?<![\w./\\-]){_name(repo)}", text, re.IGNORECASE) is not None


def counts_with_name(repo: str, passage: Passage, wording: str, asked_wording: str) -> bool:
    """Lines naming the project count the name as one shared word, and still need another.

    It also has to pass the evidence rule for the question as asked, so this never accepts lines
    that the rule would have turned down had the name stayed in the search.
    """
    return (
        names_the_project(repo, passage.text)
        and bool(set(tokenize(wording)) & set(tokenize(passage.text)))
        and is_evidence(passage, asked_wording)
    )


def with_name_seat(repo: str, found: Sequence[Passage], passages: Sequence[Passage], wording: str, asked_wording: str) -> list[Passage]:
    """Adds the best passage naming the project that is not offered yet, after the others, whose order stays."""

    def offered(candidate: Passage) -> bool:
        return any(
            other.chunk.id == candidate.chunk.id and other.start_line <= candidate.end_line and candidate.start_line <= other.end_line
            for other in passages
        )

    seat = next((p for p in found if counts_with_name(repo, p, wording, asked_wording) and not offered(p)), None)
    if seat is None:
        return list(passages)
    # Ids follow the position, so that a passage from the other search cannot share an id.
    return [replace(p, id=f"P{number}") for number, p in enumerate((*passages[: MAX_PASSAGES - 1], seat), start=1)]
