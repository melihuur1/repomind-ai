"""Completes the passages with the declaration of a constant they use. No LLM.

A passage that reads `.timeout(REQUEST_TIMEOUT)` does not say how long the timeout is. The value
is where the constant is declared: thirty lines up in the same file, or in a file this one imports.
The file is read from Git at the indexed commit and parsed with Tree-sitter, so a declaration is
found by its syntax, not by a matching line of text.

Which declaration a name means is decided where it is used, by `repomind.qa.declarations`: by
lexical scope in TypeScript and JavaScript, and by enclosing types, imports and the package in
Java. A same-named constant in another function, another file or another package is never taken,
and what cannot be decided from the syntax is left alone.

The declaration has to lie inside an indexed chunk, so lines withheld for holding a credential are
never added, and it is checked for a credential again before it is shown. Constants are written
in capitals with an underscore, ``MAX_TITLE_LENGTH``, by convention in all three languages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Sequence

from repomind.ingest.filters import find_credential
from repomind.qa.declarations import Declaration, Resolver, Sources, resolve  # noqa: F401
from repomind.qa.passages import Passage
from repomind.search.tokenizer import tokenize
from repomind.store.database import ChunkRow

CONSTANT = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
# A few more lines than a passage, so that a short object literal still fits.
MAX_DECLARATION_LINES = 4
# On top of the passages search chose, so that they are completed and not crowded out.
MAX_COMPLETIONS = 2


class SnapshotSources:
    """One indexed commit: chunks and types from the index, and file text from Git.

    Only files in the index are ever read, so a file left out of it, such as `.env`, is never opened.
    """

    def __init__(self, store, snapshot_id: int, git, commit: str) -> None:
        self._store, self._snapshot, self._git, self._commit = store, snapshot_id, git, commit
        self._languages: dict[str, str | None] = {}
        self._texts: dict[str, str | None] = {}

    def language(self, path: str) -> str | None:
        if path not in self._languages:
            row = self._store.indexed_file(self._snapshot, path)
            self._languages[path] = row["language"] if row else None
        return self._languages[path]

    def text(self, path: str) -> str | None:
        if self.language(path) is None:
            return None
        if path not in self._texts:
            raw = self._git.read_file(self._commit, path)
            self._texts[path] = raw.decode("utf-8", "replace") if raw is not None else None
        return self._texts[path]

    def chunks(self, path: str) -> Sequence[ChunkRow]:
        return self._store.file_chunks(self._snapshot, path)

    def type_paths(self, name: str) -> Sequence[str]:
        return self._store.type_paths(self._snapshot, name)


@dataclass(frozen=True)
class Completion:
    """A declaration added for a constant some passage uses, or why none was."""

    name: str
    used_in: str
    declaration: Declaration | None
    reason: str = ""


def _covering_chunk(sources: Sources, declaration: Declaration) -> ChunkRow | None:
    for chunk in sources.chunks(declaration.path):
        if chunk.start_line <= declaration.start_line and declaration.end_line <= chunk.end_line:
            return chunk
    return None


def _lines(chunk: ChunkRow, start: int, end: int) -> str:
    return "\n".join(chunk.content.split("\n")[start - chunk.start_line : end - chunk.start_line + 1])


def _shown(passages: Sequence[Passage], path: str, start: int, end: int) -> bool:
    return any(p.path == path and p.start_line <= end and start <= p.end_line for p in passages)


def complete(passages: Sequence[Passage], sources: Sources, limit: int = MAX_COMPLETIONS) -> tuple[list[Passage], list[Completion]]:
    """The passages, each followed by the declarations of constants it uses and does not show.

    Returns the new passages, numbered again, and a record of every constant considered.
    """
    resolver = Resolver(sources)
    added: dict[int, list[Passage]] = {}
    record: list[Completion] = []
    taken: list[Passage] = list(passages)
    for index, passage in enumerate(passages):
        for name in dict.fromkeys(CONSTANT.findall(passage.text)):
            where = f"{passage.path}:{passage.start_line}-{passage.end_line}"
            if sum(len(items) for items in added.values()) >= limit:
                break
            declaration, reason = resolver.resolve(name, passage.path, passage.start_line, passage.end_line)
            if declaration is None:
                record.append(Completion(name, where, None, reason))
                continue
            if _shown(taken, declaration.path, declaration.start_line, declaration.end_line):
                record.append(Completion(name, where, declaration, "already shown"))
                continue
            if declaration.end_line - declaration.start_line + 1 > MAX_DECLARATION_LINES:
                record.append(Completion(name, where, declaration, "the declaration is too long to add"))
                continue
            chunk = _covering_chunk(sources, declaration)
            if chunk is None:
                record.append(Completion(name, where, declaration, "the declaration is not in the index"))
                continue
            text = _lines(chunk, declaration.start_line, declaration.end_line)
            if find_credential(text, chunk.language):
                record.append(Completion(name, where, declaration, "the declaration may hold a credential"))
                continue
            completion = Passage("", chunk, declaration.start_line, declaration.end_line, text)
            added.setdefault(index, []).append(completion)
            taken.append(completion)
            record.append(Completion(name, where, declaration))
    ordered = [item for index, passage in enumerate(passages) for item in (passage, *added.get(index, []))]
    return [replace(item, id=f"P{number}") for number, item in enumerate(ordered, start=1)], record


def _key(passage: Passage) -> str:
    return f"{passage.path}:{passage.start_line}-{passage.end_line}"


def linked_use(declaration: Passage, record: Sequence[Completion], offered: Sequence[Passage]) -> tuple[Passage, str] | None:
    """The offered passage that uses the constant this passage declares, when completion added it for that use.

    Only a declaration completion itself added has one: it resolved to exactly one declaration, inside
    an indexed chunk, checked for a credential. Both have to be from the same repository and commit.
    """
    for item in record:
        found = item.declaration
        if found is None or item.reason or (found.path, found.start_line, found.end_line) != (declaration.path, declaration.start_line, declaration.end_line):
            continue
        use = next((p for p in offered if _key(p) == item.used_in and p is not declaration), None)
        if use is not None and (use.chunk.repo, use.chunk.commit_sha) == (declaration.chunk.repo, declaration.chunk.commit_sha):
            return use, item.name
    return None


def declared_values(chosen: Sequence[Passage], record: Sequence[Completion], offered: Sequence[Passage]) -> list[Passage]:
    """The offered passages that declare a constant the chosen lines use, when they are not chosen themselves.

    A question about a value needs two places: the line that uses the constant and the line that
    gives it its value. The model usually chooses the first. The second was found from the first by
    the syntax, for exactly these lines, lies in the index, holds no credential and was offered to
    the model too. Showing it beside the lines that name it makes the answer whole.

    This goes one way only. The chosen lines were accepted on their own, and the declaration is
    shown as what a name in them stands for. A declaration the model chose never brings in a line
    that uses it: that is linked evidence, which is experimental and off.
    """
    found: list[Passage] = []
    for passage in chosen:
        for item in record:
            declaration = item.declaration
            if item.used_in != _key(passage) or declaration is None or item.reason not in ("", "already shown"):
                continue
            holder = next(
                (
                    other
                    for other in offered
                    if other.path == declaration.path
                    and other.start_line <= declaration.start_line
                    and declaration.end_line <= other.end_line
                    and (other.chunk.repo, other.chunk.commit_sha) == (passage.chunk.repo, passage.chunk.commit_sha)
                ),
                None,
            )
            if holder is not None and all(holder is not other for other in (*chosen, *found)):
                found.append(holder)
    return found[:MAX_COMPLETIONS]


def uses_to_review(
    review: Sequence[Passage],
    chosen: Sequence[Passage],
    offered: Sequence[Passage],
    record: Sequence[Completion],
    counts_as_evidence: Callable[[Passage], bool],
) -> list[Passage]:
    """Lines that use a constant whose declaration the model chose and the evidence rule turned down.

    They are shown beside that declaration for the reader to check, and never as an answer: accepting a
    declaration through a line that uses it was measured on 342 questions, and about two in three such
    acceptances were a wrong place or an unanswerable question. A use is taken only when completion
    resolved the constant in it to exactly this declaration, the use was offered to the model, which
    means it passed the credential and Git checks, it is from the same repository and commit, and it
    passes the evidence rule on its own. A passage counts as a declaration only if it is no longer than a
    declaration can be. Nothing already shown is shown twice, and at most MAX_COMPLETIONS are added.
    """
    shown = {_key(passage) for passage in (*review, *chosen)}
    found: list[Passage] = []
    for passage in review:
        if passage.end_line - passage.start_line + 1 > MAX_DECLARATION_LINES:
            continue
        for item in record:
            declaration = item.declaration
            if declaration is None or item.reason not in ("", "already shown") or declaration.path != passage.path:
                continue
            if not (passage.start_line <= declaration.start_line and declaration.end_line <= passage.end_line):
                continue
            use = next((other for other in offered if _key(other) == item.used_in), None)
            if use is None or (use.chunk.repo, use.chunk.commit_sha) != (passage.chunk.repo, passage.chunk.commit_sha):
                continue
            if _key(use) in shown or not counts_as_evidence(use):
                continue
            shown.add(_key(use))
            found.append(use)
    return found[:MAX_COMPLETIONS]


def linked_evidence(
    chosen: Sequence[Passage],
    offered: Sequence[Passage],
    record: Sequence[Completion],
    wording: str,
    counts_as_evidence: Callable[[Passage], bool],
) -> tuple[list[Passage], list[Passage], list[str]]:
    """Which of the model's choices are evidence, with the use shown before a declaration accepted through it.

    A choice is evidence when it passes the evidence rule, as before. A declaration that does not is
    evidence only as a pair: the line that uses it was offered, passes the rule itself, and the
    declaration shares at least one word with the question, so that a constant beside the point is not
    taken in with a line that is. Returns the evidence, the choices turned down, and what to tell the
    reader. A use the model chose never brings in its declaration unasked.
    """
    terms = set(tokenize(wording))
    accepted: list[Passage] = []
    turned_down: list[Passage] = []
    notes: list[str] = []

    def add(passage: Passage) -> None:
        if passage not in accepted:
            accepted.append(passage)

    for passage in chosen:
        if counts_as_evidence(passage):
            add(passage)
            continue
        link = linked_use(passage, record, offered)
        if link is not None and counts_as_evidence(link[0]) and terms & set(tokenize(passage.text)):
            use, name = link
            add(use)
            add(passage)
            chose_the_use = any(use is item for item in chosen)
            notes.append(
                f"The model chose {_key(passage)}, which declares {name}. "
                + (f"It also chose {_key(use)}, which uses it. " if chose_the_use else f"{_key(use)}, which uses it, was added by RepoMind and not chosen by the model. ")
                + "Both exist in Git at this commit. Whether they answer the question is the model's reading."
            )
        else:
            turned_down.append(passage)
    return accepted, turned_down, notes


# Unresolved for these reasons, the constant is real and its value unknown. "No declaration" is left
# out: an enum value or a constant of a library is its own meaning, and warning about each would be noise.
_UNRESOLVED = ("is declared", "is not indexed", "is imported from", "types are called", "different declarations", "not in the index", "may hold a credential", "too long")


def unshown_values(chosen: Sequence[Passage], record: Sequence[Completion]) -> list[str]:
    """What the reader should know about constants the chosen lines use and whose value they do not show."""
    notes = []
    for passage in chosen:
        where = f"{passage.path}:{passage.start_line}-{passage.end_line}"
        for item in record:
            if item.used_in != where:
                continue
            declaration = item.declaration
            if declaration is not None and item.reason in ("", "already shown"):
                if not _shown(chosen, declaration.path, declaration.start_line, declaration.end_line):
                    notes.append(f"The chosen lines use {item.name}, whose value is at {declaration.path}:{declaration.start_line}, which was not chosen.")
            elif any(part in item.reason for part in _UNRESOLVED):
                notes.append(f"The chosen lines use {item.name}, whose value is not shown: {item.reason}.")
    return list(dict.fromkeys(notes))
