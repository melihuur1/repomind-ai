"""Completes the passages with the declaration of a constant they use. No LLM.

A passage that reads `.timeout(REQUEST_TIMEOUT)` does not say how long the timeout is. The value
is where the constant is declared: thirty lines up in the same file, or in a file this one imports.
The file is read from Git at the indexed commit and parsed with Tree-sitter, so a declaration is
found by its syntax, not by a matching line of text.

Only what resolves to exactly one declaration is followed:

* a constant declared in the passage's own file;
* in TypeScript or JavaScript, a constant the file imports by name from a relative module;
* in Java, ``Name.CONSTANT``, when exactly one class or interface in the repository is called ``Name``.

Anything else, such as a workspace package, a second declaration with the same name, or a
constant declared in a file that is not indexed, is left alone. The declaration has to lie inside
an indexed chunk, so lines withheld for holding a credential are never added, and it is checked
for a credential again before it is shown. Constants are written in capitals with an underscore,
``MAX_TITLE_LENGTH``, by convention in all three languages.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, replace
from typing import Protocol, Sequence

from repomind.ingest.filters import find_config_secret, find_secret_rules
from repomind.parsing.languages import parser_for
from repomind.qa.passages import Passage
from repomind.store.database import ChunkRow

CONSTANT = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
# A few more lines than a passage, so that a short object literal still fits.
MAX_DECLARATION_LINES = 4
# On top of the passages search chose, so that they are completed and not crowded out.
MAX_COMPLETIONS = 2
_LANGUAGES = {"java", "typescript", "tsx", "javascript"}
_MODULE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js")


class Sources(Protocol):
    """What completion may read, all at the commit the passages come from."""

    def text(self, path: str) -> str | None: ...

    def language(self, path: str) -> str | None: ...

    def chunks(self, path: str) -> Sequence[ChunkRow]: ...

    def imports(self, path: str) -> Sequence[tuple[str, Sequence[str]]]: ...

    def type_paths(self, name: str) -> Sequence[str]: ...


class SnapshotSources:
    """One indexed commit: chunks, imports and types from the index, and file text from Git.

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

    def imports(self, path: str) -> Sequence[tuple[str, Sequence[str]]]:
        return self._store.file_imports(self._snapshot, path)

    def type_paths(self, name: str) -> Sequence[str]:
        return self._store.type_paths(self._snapshot, name)


@dataclass(frozen=True)
class Declaration:
    name: str
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Completion:
    """A declaration added for a constant some passage uses, or why none was."""

    name: str
    used_in: str
    declaration: Declaration | None
    reason: str = ""


def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def declarations(language: str, source: str, name: str) -> list[tuple[int, int]]:
    """Lines of every declaration of ``name`` as a field or a variable, by Tree-sitter."""
    if language not in _LANGUAGES:
        return []
    data = source.encode("utf-8")
    found: list[tuple[int, int]] = []
    stack = [parser_for(language).parse(data).root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        names: list = []
        if node.type in ("field_declaration", "constant_declaration", "lexical_declaration", "variable_declaration"):
            names = [d.child_by_field_name("name") for d in node.named_children if d.type == "variable_declarator"]
        elif node.type in ("public_field_definition", "field_definition"):
            names = [node.child_by_field_name("name") or node.child_by_field_name("property")]
        if any(n is not None and _text(n, data) == name for n in names):
            outer = node.parent if node.parent is not None and node.parent.type == "export_statement" else node
            found.append((outer.start_point[0] + 1, outer.end_point[0] + 1))
    return sorted(set(found))


def _relative_module(path: str, module: str) -> list[str]:
    if not module.startswith("."):
        return []
    base = posixpath.normpath(posixpath.join(posixpath.dirname(path), module))
    return [base, *(base + suffix for suffix in _MODULE_SUFFIXES)]


def _only(sources: Sources, path: str, name: str) -> tuple[Declaration | None, str]:
    language, text = sources.language(path), sources.text(path)
    if language is None or text is None:
        return None, f"{path} is not indexed"
    spans = declarations(language, text, name)
    if len(spans) > 1:
        return None, f"{name} is declared {len(spans)} times in {path}"
    if not spans:
        return None, ""
    return Declaration(name, path, *spans[0]), ""


def resolve(sources: Sources, name: str, path: str, used_in: str) -> tuple[Declaration | None, str]:
    """The one declaration of ``name`` that the passage at ``path`` can mean, or why there is none."""
    found, reason = _resolve(sources, name, path, used_in)
    return found, reason or ("" if found else f"no declaration of {name} was found")


def _resolve(sources: Sources, name: str, path: str, used_in: str) -> tuple[Declaration | None, str]:
    found, reason = _only(sources, path, name)
    if found is not None or reason:
        return found, reason
    language = sources.language(path)
    if language in ("typescript", "tsx", "javascript"):
        for module, imported in sources.imports(path):
            if name not in imported:
                continue
            for candidate in _relative_module(path, module):
                if sources.language(candidate) is not None:
                    return _only(sources, candidate, name)
            return None, f"{name} is imported from {module}, which is not a file of this repository"
    if language == "java":
        qualifier = re.search(rf"\b([A-Z]\w*)\.{re.escape(name)}\b", used_in)
        if qualifier:
            paths = list(dict.fromkeys(sources.type_paths(qualifier.group(1))))
            if len(paths) == 1:
                return _only(sources, paths[0], name)
            if paths:
                return None, f"{len(paths)} types are called {qualifier.group(1)}"
    return None, ""


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
    added: dict[int, list[Passage]] = {}
    record: list[Completion] = []
    taken: list[Passage] = list(passages)
    for index, passage in enumerate(passages):
        for name in dict.fromkeys(CONSTANT.findall(passage.text)):
            where = f"{passage.path}:{passage.start_line}-{passage.end_line}"
            if sum(len(items) for items in added.values()) >= limit:
                break
            declaration, reason = resolve(sources, name, passage.path, passage.text)
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
            if find_config_secret(text) or find_secret_rules(text):
                record.append(Completion(name, where, declaration, "the declaration may hold a credential"))
                continue
            completion = Passage("", chunk, declaration.start_line, declaration.end_line, text)
            added.setdefault(index, []).append(completion)
            taken.append(completion)
            record.append(Completion(name, where, declaration))
    ordered = [item for index, passage in enumerate(passages) for item in (passage, *added.get(index, []))]
    return [replace(item, id=f"P{number}") for number, item in enumerate(ordered, start=1)], record


# Unresolved for these reasons, the constant is real and its value unknown. "No declaration" is left
# out: an enum value or a constant of a library is its own meaning, and warning about each would be noise.
_UNRESOLVED = ("is declared", "is not indexed", "is imported from", "types are called", "not in the index", "may hold a credential", "too long")


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
