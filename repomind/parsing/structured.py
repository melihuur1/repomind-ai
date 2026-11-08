"""Chunks SQL and YAML by their own structure. Neither is ever executed or evaluated.

SQL is split by a small lexer rather than a grammar: it only needs statement boundaries,
which stay the same across dialects whose full syntax differs a lot. It understands
comments, quoted strings and dollar quoting, so a ``;`` inside a function body does not end
the statement.

YAML goes through Tree-sitter, which yields syntax nodes only. Tags such as
``!!python/object`` remain plain text, unlike with a loader that constructs objects.

A YAML chunk holding a literal credential is withheld on its own, so that a compose file
with one development password does not hide everything else in the file. An entry holding one
is taken apart down to the smallest entry that does, so that a service's ``depends_on`` is not
withheld because its ``environment`` holds a password.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tree_sitter_yaml
from tree_sitter import Language, Node, Parser

from repomind.ingest.filters import find_config_secret, is_test_path
from repomind.parsing.models import Chunk, ParsedFile, SymbolInfo

_MAX_LINES = 60
_MAX_CHARS = 1500
_YAML_SPLIT_LINES = 15
_YAML_SCALAR_GROUP_LINES = 2

_YAML_PARSER = Parser(Language(tree_sitter_yaml.language()))

_SQL_OBJECT = re.compile(
    r"""^\s*(?:CREATE|ALTER|DROP|COMMENT\s+ON)\s+
        (?:OR\s+REPLACE\s+)?(?:UNIQUE\s+)?(?:TEMP(?:ORARY)?\s+)?(?:MATERIALIZED\s+)?
        (TABLE|INDEX|VIEW|FUNCTION|PROCEDURE|TRIGGER|TYPE|SEQUENCE|EXTENSION|SCHEMA|DATABASE)\s+
        (?:CONCURRENTLY\s+)?(?:IF\s+(?:NOT\s+)?EXISTS\s+)?
        ([\w."$]+)""",
    re.IGNORECASE | re.VERBOSE,
)
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")


@dataclass
class _Builder:
    path: str
    language: str
    lines: list[str]

    def __post_init__(self) -> None:
        self.is_test = is_test_path(self.path)
        self.chunks: list[Chunk] = []
        self.symbols: list[SymbolInfo] = []

    def _fits(self, start: int, end: int) -> bool:
        return end - start + 1 <= _MAX_LINES and sum(len(l) + 1 for l in self.lines[start - 1 : end]) <= _MAX_CHARS

    def _ranges(self, start: int, end: int) -> list[tuple[int, int]]:
        ranges = []
        cursor = start
        for line in range(start, end + 1):
            if line > cursor and not self._fits(cursor, line):
                ranges.append((cursor, line - 1))
                cursor = line
        ranges.append((cursor, end))
        return ranges

    def trim(self, start: int, end: int) -> tuple[int, int] | None:
        while start <= end and not self.lines[start - 1].strip():
            start += 1
        while end >= start and not self.lines[end - 1].strip():
            end -= 1
        return (start, end) if start <= end else None

    def emit(self, kind: str, start: int, end: int, symbol: str | None = None, parent: str | None = None) -> None:
        trimmed = self.trim(start, end)
        if trimmed is None:
            return
        ranges = self._ranges(*trimmed)
        for index, (first, last) in enumerate(ranges, start=1):
            self.chunks.append(
                Chunk(
                    path=self.path,
                    language=self.language,
                    kind=kind,
                    start_line=first,
                    end_line=last,
                    content="\n".join(self.lines[first - 1 : last]),
                    symbol=symbol,
                    parent=parent,
                    signature=symbol,
                    symbols=(symbol,) if symbol else (),
                    part_index=index,
                    part_total=len(ranges),
                    is_test=self.is_test,
                )
            )

    def symbol(self, kind: str, name: str, parent: str | None, start: int, end: int) -> None:
        qualified = f"{parent}.{name}" if parent else name
        self.symbols.append(SymbolInfo(kind, name, qualified, parent, start, end, qualified))

    def parsed(self, has_errors: bool, withheld: int = 0) -> ParsedFile:
        count = len(self.lines) - (1 if self.lines and self.lines[-1] == "" else 0)
        result = ParsedFile(self.path, self.language, count, has_errors, self.symbols, [], self.chunks)
        result.withheld_chunks = withheld
        return result


# ------------------------------------------------------------------------------- SQL


def _sql_statements(text: str) -> list[tuple[int, int, bool]]:
    """(first line, last line, is comment-only) for each statement or comment run.

    Works on characters and keeps ``;`` inside strings, comments and dollar quotes
    from ending a statement.
    """
    statements: list[tuple[int, int, bool]] = []
    position, line = 0, 1
    start_line: int | None = None
    length = len(text)

    def advance_to(index: int) -> None:
        nonlocal position, line
        line += text.count("\n", position, index)
        position = index

    while position < length:
        character = text[position]
        if character.isspace():
            advance_to(position + 1)
        elif text.startswith("--", position):
            end = text.find("\n", position)
            end = length if end == -1 else end
            if start_line is None:
                statements.append((line, line, True))
            advance_to(end)
        elif text.startswith("/*", position):
            end = text.find("*/", position + 2)
            end = length if end == -1 else end + 2
            first = line
            advance_to(end)
            if start_line is None:
                statements.append((first, line, True))
        elif character in "'\"`":
            start_line = line if start_line is None else start_line
            index = position + 1
            while index < length:
                if text[index] == character:
                    if index + 1 < length and text[index + 1] == character:
                        index += 2
                        continue
                    break
                index += 1
            advance_to(min(index + 1, length))
        elif character == "$" and (tag := _DOLLAR_TAG.match(text, position)):
            start_line = line if start_line is None else start_line
            end = text.find(tag.group(0), tag.end())
            advance_to(length if end == -1 else end + len(tag.group(0)))
        elif character == ";":
            start_line = line if start_line is None else start_line
            statements.append((start_line, line, False))
            start_line = None
            advance_to(position + 1)
        else:
            start_line = line if start_line is None else start_line
            advance_to(position + 1)
    if start_line is not None:
        statements.append((start_line, line, False))
    return statements


def parse_sql(path: str, text: str) -> ParsedFile:
    lines = text.split("\n")
    builder = _Builder(path, "sql", lines)
    comment_start: int | None = None
    comment_end = 0

    for first, last, is_comment in _sql_statements(text):
        if is_comment:
            if comment_start is not None and first - comment_end > 1:
                builder.emit("comment", comment_start, comment_end)
                comment_start = None
            comment_start = first if comment_start is None else comment_start
            comment_end = last
            continue
        start = first
        if comment_start is not None:
            if first - comment_end <= 1:
                start = comment_start
            else:
                builder.emit("comment", comment_start, comment_end)
            comment_start = None
        statement = "\n".join(lines[first - 1 : last])
        match = _SQL_OBJECT.match(statement)
        if match:
            kind = match.group(1).lower()
            name = match.group(2).strip('"')
            builder.symbol(kind, name, None, first, last)
            builder.emit(kind, start, last, symbol=name)
        else:
            builder.emit("statement", start, last)
    if comment_start is not None:
        builder.emit("comment", comment_start, comment_end)
    return builder.parsed(False)


# ------------------------------------------------------------------------------ YAML


def _start(node: Node) -> int:
    return node.start_point[0] + 1


def _end(node: Node) -> int:
    row, column = node.end_point
    if column == 0 and row > node.start_point[0]:
        row -= 1
    return row + 1


def _block_mapping(node: Node | None) -> Node | None:
    while node is not None:
        if node.type == "block_mapping":
            return node
        children = [c for c in node.named_children if c.type != "comment"]
        node = children[0] if len(children) == 1 else None
    return None


def _flat(mapping: Node) -> bool:
    """Whether no entry of this mapping holds entries of its own."""
    return all(
        _block_mapping(child.child_by_field_name("value")) is None
        for child in mapping.named_children
        if child.type == "block_mapping_pair"
    )


def _yaml_key(pair: Node, source: bytes) -> str:
    key = pair.child_by_field_name("key")
    if key is None:
        return "<key>"
    return source[key.start_byte : key.end_byte].decode("utf-8", "replace").strip("'\" ")


class _YamlChunker:
    def __init__(self, builder: _Builder, source: bytes) -> None:
        self.builder = builder
        self.source = source
        self.withheld = 0

    def _emit(self, start: int, end: int, symbol: str | None, parent: str | None) -> None:
        before = len(self.builder.chunks)
        self.builder.emit("config", start, end, symbol=symbol, parent=parent)
        kept = self.builder.chunks[:before]
        for chunk in self.builder.chunks[before:]:
            if find_config_secret(chunk.content):
                self.withheld += 1
            else:
                kept.append(chunk)
        self.builder.chunks = kept

    def collect_symbols(self, mapping: Node, parent: str | None) -> None:
        for child in mapping.named_children:
            if child.type != "block_mapping_pair":
                continue
            name = _yaml_key(child, self.source)
            self.builder.symbol("config_key", name, parent, _start(child), _end(child))
            nested = _block_mapping(child.child_by_field_name("value"))
            if nested is not None:
                self.collect_symbols(nested, f"{parent}.{name}" if parent else name)

    def _with_leading_comments(self, line: int) -> int:
        """Comments are outside the mapping at the top of a file, so go by the lines themselves."""
        while line > 1 and self.builder.lines[line - 2].lstrip().startswith("#"):
            line -= 1
        return line

    def mapping(self, mapping: Node, parent: str | None) -> None:
        group: list[tuple[int, int]] = []

        def flush() -> None:
            if group:
                self._emit(group[0][0], group[-1][1], parent, None)
                group.clear()

        for child in mapping.named_children:
            if child.type != "block_mapping_pair":
                continue
            start = self._with_leading_comments(_start(child))
            end = _end(child)
            name = _yaml_key(child, self.source)
            qualified = f"{parent}.{name}" if parent else name
            nested = _block_mapping(child.child_by_field_name("value"))
            size = end - start + 1
            if find_config_secret("\n".join(self.builder.lines[start - 1 : end])):
                # Only the part holding it is withheld. Its neighbours stay out of any group with it,
                # and an entry with nested entries is taken apart until the part holding it is a flat
                # list of settings, such as `environment`, which is withheld whole: one variable alone
                # says little, and would only crowd search. The check goes line by line, so each line
                # is judged the same in any chunk.
                flush()
                if nested is not None and not _flat(nested):
                    self.mapping(nested, qualified)
                else:
                    self._emit(start, end, qualified, parent)
            elif nested is not None and size > _YAML_SPLIT_LINES:
                flush()
                self.mapping(nested, qualified)
            elif size <= _YAML_SCALAR_GROUP_LINES and parent is not None:
                group.append((start, end))
            else:
                flush()
                self._emit(start, end, qualified, parent)
        flush()


def parse_yaml(path: str, text: str) -> ParsedFile:
    lines = text.split("\n")
    builder = _Builder(path, "yaml", lines)
    source = text.encode("utf-8")
    root = _YAML_PARSER.parse(source).root_node
    chunker = _YamlChunker(builder, source)

    mappings = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "block_mapping":
            mappings.append(node)
        elif node.type in {"stream", "document", "block_node"}:
            stack.extend(node.named_children)
    for mapping in sorted(mappings, key=_start):
        chunker.collect_symbols(mapping, None)
        chunker.mapping(mapping, None)

    if not mappings and text.strip():
        chunker._emit(1, len(lines), None, None)
    builder.chunks.sort(key=lambda chunk: chunk.start_line)
    return builder.parsed(root.has_error, chunker.withheld)
