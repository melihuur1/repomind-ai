"""Syntax-aware extraction of symbols, imports and chunks.

Chunk boundaries follow declarations wherever possible:

* a small class is one chunk, a larger one becomes a header plus member chunks;
* adjacent one-liners such as getters are grouped, so they do not flood the index;
* an oversized callable is split at statement boundaries, descending into nested
  blocks only when a single statement does not fit, and into raw lines last;
* comments and decorators directly above a declaration stay with it.

Imports are captured as structured data rather than as retrievable chunks: they
carry no behaviour, yet would rank highly for any query naming a library.

The source is parsed, never evaluated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from tree_sitter import Node

from repomind.ingest.filters import is_test_path
from repomind.parsing.languages import LanguageSpec, parser_for, spec_for
from repomind.parsing.models import Chunk, ImportInfo, ParsedFile, SymbolInfo
from repomind.parsing.structured import parse_sql, parse_yaml

_CLOSERS_ONLY = re.compile(r"^[\s})\];,]*$")
_WHITESPACE = re.compile(r"\s+")
_MAX_SIGNATURE_CHARS = 240
_DOMINANT_CHILD_RATIO = 0.8
_MAX_DESCENT = 12


@dataclass(frozen=True)
class ChunkingConfig:
    max_lines: int = 60
    max_chars: int = 1500
    small_container_lines: int = 40
    tiny_member_lines: int = 3
    named_variable_min_lines: int = 5


@dataclass
class _Declaration:
    role: str  # import | container | callable | atomic | statement
    kind: str
    name: str | None
    node: Node  # outermost node, which defines the line range
    decl: Node  # innermost node, which owns name and body


def parse_source(path: str, language: str, text: str, config: ChunkingConfig | None = None) -> ParsedFile:
    """Parse text produced by ``repomind.ingest.filters.normalize_source``."""
    if language == "sql":
        return parse_sql(path, text)
    if language == "yaml":
        return parse_yaml(path, text)
    return _FileChunker(path, language, text, config or ChunkingConfig()).run()


class _FileChunker:
    def __init__(self, path: str, language: str, text: str, config: ChunkingConfig) -> None:
        self.path = path
        self.language = language
        self.spec: LanguageSpec = spec_for(language)
        self.config = config
        self.source = text.encode("utf-8")
        self.lines = text.split("\n")
        self.char_offsets = [0]
        for line in self.lines:
            self.char_offsets.append(self.char_offsets[-1] + len(line) + 1)
        self.is_test = is_test_path(path)
        self.chunks: list[Chunk] = []
        self.symbols: list[SymbolInfo] = []
        self.imports: list[ImportInfo] = []

    # ------------------------------------------------------------------ driver

    def run(self) -> ParsedFile:
        root = parser_for(self.language).parse(self.source).root_node
        self._collect_symbols(root, None)
        self._walk_top_level(root)
        if not self.chunks and not self.imports and "".join(self.lines).strip():
            self._emit_ranges("lines", self._hard_split(1, len(self.lines)))
        chunks = [self._with_contained_symbols(chunk) for chunk in self.chunks]
        chunks.sort(key=lambda chunk: (chunk.start_line, chunk.end_line))
        return ParsedFile(
            path=self.path,
            language=self.language,
            line_count=len(self.lines) - (1 if self.lines and self.lines[-1] == "" else 0),
            has_syntax_errors=root.has_error,
            symbols=self.symbols,
            imports=self.imports,
            chunks=chunks,
        )

    def _with_contained_symbols(self, chunk: Chunk) -> Chunk:
        names = [
            symbol.name
            for symbol in self.symbols
            if chunk.start_line <= symbol.start_line <= chunk.end_line
        ]
        return replace(chunk, symbols=tuple(dict.fromkeys(names)))

    # --------------------------------------------------------------- utilities

    def _text(self, node: Node | None) -> str:
        if node is None:
            return ""
        return self.source[node.start_byte : node.end_byte].decode("utf-8", "replace")

    @staticmethod
    def _start(node: Node) -> int:
        return node.start_point[0] + 1

    @staticmethod
    def _end(node: Node) -> int:
        row, column = node.end_point
        if column == 0 and row > node.start_point[0]:
            row -= 1
        return row + 1

    def _span(self, node: Node) -> int:
        return self._end(node) - self._start(node) + 1

    def _chars(self, start: int, end: int) -> int:
        return self.char_offsets[end] - self.char_offsets[start - 1]

    def _fits(self, start: int, end: int) -> bool:
        return end - start + 1 <= self.config.max_lines and self._chars(start, end) <= self.config.max_chars

    def _signature(self, outer: Node, decl: Node) -> str:
        body = decl.child_by_field_name("body")
        stop = body.start_byte if body is not None else decl.end_byte
        raw = self.source[outer.start_byte : stop].decode("utf-8", "replace")
        return _WHITESPACE.sub(" ", raw).strip()[:_MAX_SIGNATURE_CHARS]

    @staticmethod
    def _qualify(parent: str | None, name: str | None) -> str | None:
        if name is None:
            return parent
        return f"{parent}.{name}" if parent else name

    # ---------------------------------------------------------- classification

    def _function_in_value(self, value: Node | None) -> Node | None:
        """The function behind ``x = () => {}`` or ``x = memo(() => {})``."""
        if value is None:
            return None
        if value.type in self.spec.function_value_types:
            return value
        if value.type in {"call_expression", "await_expression", "parenthesized_expression", "as_expression"}:
            arguments = value.child_by_field_name("arguments")
            candidates = arguments.named_children if arguments is not None else value.named_children
            for candidate in candidates:
                found = self._function_in_value(candidate) if candidate.type != "identifier" else None
                if found is not None:
                    return found
        return None

    def _classify(self, node: Node) -> _Declaration:
        spec = self.spec
        kind = node.type
        if kind in spec.import_types:
            return _Declaration("import", "import", None, node, node)
        if kind in spec.wrapper_types:
            return self._classify_wrapper(node)
        if kind in spec.container_kinds:
            return _Declaration("container", spec.container_kinds[kind], self._declared_name(node), node, node)
        if kind in spec.callable_kinds:
            return _Declaration("callable", spec.callable_kinds[kind], self._declared_name(node), node, node)
        if kind in spec.atomic_kinds:
            return _Declaration("atomic", spec.atomic_kinds[kind], self._declared_name(node), node, node)
        if kind in spec.variable_types:
            return self._classify_variable(node)
        return _Declaration("statement", "module", None, node, node)

    def _classify_wrapper(self, node: Node) -> _Declaration:
        declaration = node.child_by_field_name("declaration")
        if declaration is None:
            declaration = next(
                (child for child in node.named_children if child.type not in self.spec.decorator_types), None
            ) if node.type == "ambient_declaration" else None
        if declaration is not None:
            inner = self._classify(declaration)
            inner.node = node
            return inner
        if node.child_by_field_name("source") is not None:
            return _Declaration("import", "import", None, node, node)
        value = node.child_by_field_name("value")
        function = self._function_in_value(value)
        if function is not None:
            return _Declaration("callable", "function", "default", node, function)
        if value is not None and value.type == "class":
            return _Declaration("container", "class", self._declared_name(value) or "default", node, value)
        return _Declaration("statement", "module", None, node, node)

    def _classify_variable(self, node: Node) -> _Declaration:
        first_name: str | None = None
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            name = self._text(declarator.child_by_field_name("name"))
            first_name = first_name or name
            value = declarator.child_by_field_name("value")
            function = self._function_in_value(value)
            if function is not None:
                return _Declaration("callable", "function", name, node, function)
            if value is not None and value.type == "class":
                return _Declaration("container", "class", name, node, value)
        if first_name is not None and self._span(node) >= self.config.named_variable_min_lines:
            return _Declaration("atomic", "variable", first_name, node, node)
        return _Declaration("statement", "module", None, node, node)

    def _declared_name(self, node: Node) -> str | None:
        name = node.child_by_field_name("name")
        return self._text(name) if name is not None else None

    # ----------------------------------------------------------------- symbols

    def _collect_symbols(self, node: Node, parent: str | None) -> None:
        for child in node.named_children:
            declaration = self._classify(child)
            if declaration.role == "import":
                self._record_import(child)
                continue
            if declaration.role in {"container", "callable", "atomic"}:
                self._record_symbol(declaration, parent)
                # Descend from the inner declaration: restarting at an export
                # wrapper would classify and record the same declaration twice.
                self._collect_symbols(declaration.decl, self._qualify(parent, declaration.name))
                continue
            if child.type in self.spec.field_types:
                function = self._function_in_value(child.child_by_field_name("value"))
                name = self._member_name(child)
                if function is not None and name:
                    self._record_symbol(_Declaration("callable", "method", name, child, function), parent)
                    self._collect_symbols(function, self._qualify(parent, name))
                    continue
            self._collect_symbols(child, parent)

    def _record_symbol(self, declaration: _Declaration, parent: str | None) -> None:
        name = declaration.name or "<anonymous>"
        self.symbols.append(
            SymbolInfo(
                kind=declaration.kind,
                name=name,
                qualified_name=self._qualify(parent, name) or name,
                parent=parent,
                start_line=self._start(declaration.node),
                end_line=self._end(declaration.node),
                signature=self._signature(declaration.node, declaration.decl),
            )
        )

    def _member_name(self, node: Node) -> str | None:
        name = node.child_by_field_name("name") or node.child_by_field_name("property")
        if name is None:
            declarator = node.child_by_field_name("declarator")
            name = declarator.child_by_field_name("name") if declarator is not None else None
        return self._text(name) if name is not None else None

    def _record_import(self, node: Node) -> None:
        if self.language == "java":
            if node.type != "import_declaration":
                return
            target = next(
                (c for c in node.named_children if c.type in {"scoped_identifier", "identifier"}), None
            )
            if target is not None:
                module = self._text(target)
                self.imports.append(ImportInfo(module, (module.rsplit(".", 1)[-1],), self._start(node)))
            return
        source = node.child_by_field_name("source")
        if source is None:
            return
        names = tuple(
            self._text(descendant)
            for descendant in self._iter_descendants(node)
            if descendant.type in {"identifier", "type_identifier"} and descendant.parent != source
        )
        self.imports.append(ImportInfo(self._text(source).strip("'\"`"), tuple(dict.fromkeys(names)), self._start(node)))

    @staticmethod
    def _iter_descendants(node: Node):
        stack = list(node.named_children)
        while stack:
            current = stack.pop()
            yield current
            stack.extend(current.named_children)

    # ---------------------------------------------------------------- top level

    def _walk_top_level(self, root: Node) -> None:
        pending: tuple[int, int] | None = None  # comment run awaiting a declaration
        module_start: int | None = None
        module_end = 0
        module_nodes: list[Node] = []

        def flush_module() -> None:
            nonlocal module_start, module_nodes
            if module_start is not None:
                self._emit_ranges("module", self._split(module_start, module_end, module_nodes))
            module_start, module_nodes = None, []

        def add_to_module(start: int, end: int, node: Node | None) -> None:
            nonlocal module_start, module_end
            module_start = start if module_start is None else module_start
            module_end = end
            if node is not None:
                module_nodes.append(node)

        for node in root.named_children:
            start, end = self._start(node), self._end(node)
            if node.type in self.spec.comment_types or node.type in self.spec.decorator_types:
                if pending is not None and start - pending[1] > 1:
                    add_to_module(pending[0], pending[1], None)
                    pending = (start, end)
                else:
                    pending = (pending[0] if pending else start, end)
                continue

            declaration = self._classify(node)
            if pending is not None:
                if declaration.role != "import" and start - pending[1] <= 1:
                    start = pending[0]
                else:
                    if declaration.role != "import":
                        add_to_module(pending[0], pending[1], None)
                    else:
                        flush_module()
                pending = None

            if declaration.role == "import":
                flush_module()
            elif declaration.role == "statement":
                add_to_module(start, end, node)
            else:
                flush_module()
                self._emit_declaration(declaration, start, None)

        if pending is not None:
            add_to_module(pending[0], pending[1], None)
        flush_module()

    def _emit_declaration(self, declaration: _Declaration, start: int, parent: str | None) -> None:
        if declaration.role == "container":
            self._emit_container(declaration, start, parent)
        else:
            self._emit_callable_or_atomic(declaration, start, parent)

    def _emit_callable_or_atomic(self, declaration: _Declaration, start: int, parent: str | None) -> None:
        end = self._end(declaration.node)
        symbol = self._qualify(parent, declaration.name)
        signature = self._signature(declaration.node, declaration.decl)
        body = declaration.decl.child_by_field_name("body") or declaration.decl
        ranges = [(start, end)] if self._fits(start, end) else self._split(start, end, self._inner_nodes(body))
        self._emit_ranges(declaration.kind, ranges, symbol=symbol, parent=parent, signature=signature)

    # --------------------------------------------------------------- containers

    def _members(self, body: Node) -> list[Node]:
        members: list[Node] = []
        for child in body.named_children:
            if child.type == "enum_body_declarations":
                members.extend(child.named_children)
            else:
                members.append(child)
        return members

    def _emit_container(self, declaration: _Declaration, start: int, parent: str | None) -> None:
        end = self._end(declaration.node)
        symbol = self._qualify(parent, declaration.name)
        signature = self._signature(declaration.node, declaration.decl)
        config = self.config
        if self._fits(start, end) and end - start + 1 <= config.small_container_lines:
            self._emit_ranges(declaration.kind, [(start, end)], symbol=symbol, parent=parent, signature=signature)
            return

        body = declaration.decl.child_by_field_name("body")
        if body is None:
            ranges = self._split(start, end, self._inner_nodes(declaration.decl))
            self._emit_ranges(declaration.kind, ranges, symbol=symbol, parent=parent, signature=signature)
            return

        fields_start, fields_end = start, self._start(body)
        field_nodes: list[Node] = []
        fields_kind = "class_header"
        tiny: list[tuple[int, int, str | None]] = []
        pending: int | None = None

        def flush_fields() -> None:
            nonlocal fields_start, field_nodes, fields_kind
            if fields_start is not None:
                self._emit_ranges(
                    fields_kind,
                    self._split(fields_start, fields_end, field_nodes),
                    symbol=symbol,
                    parent=parent,
                    signature=signature,
                )
            fields_start, field_nodes, fields_kind = None, [], "fields"

        def flush_tiny() -> None:
            nonlocal tiny
            if len(tiny) == 1:
                first, last, name = tiny[0]
                self._emit_ranges("method", [(first, last)], symbol=self._qualify(symbol, name), parent=symbol)
            elif tiny:
                self._emit_ranges("methods", [(tiny[0][0], tiny[-1][1])], symbol=symbol, parent=symbol)
            tiny = []

        for member in self._members(body):
            member_start = self._start(member)
            if member.type in self.spec.comment_types or member.type in self.spec.decorator_types:
                pending = pending if pending is not None else member_start
                continue
            first_line = pending if pending is not None else member_start
            pending = None

            member_declaration = self._classify_member(member)
            if member_declaration.role == "statement":
                flush_tiny()
                fields_start = first_line if fields_start is None else fields_start
                fields_end = self._end(member)
                field_nodes.append(member)
                continue

            flush_fields()
            member_end = self._end(member)
            if (
                member_declaration.role == "callable"
                and member_end - first_line + 1 <= config.tiny_member_lines
            ):
                if tiny and not self._fits(tiny[0][0], member_end):
                    flush_tiny()
                tiny.append((first_line, member_end, member_declaration.name))
                continue
            flush_tiny()
            self._emit_declaration(member_declaration, first_line, symbol)

        flush_fields()
        flush_tiny()

    def _classify_member(self, member: Node) -> _Declaration:
        if member.type in self.spec.field_types:
            function = self._function_in_value(member.child_by_field_name("value"))
            name = self._member_name(member)
            if function is not None and name:
                return _Declaration("callable", "method", name, member, function)
            return _Declaration("statement", "fields", None, member, member)
        declaration = self._classify(member)
        if declaration.role == "atomic":
            return declaration
        if declaration.role not in {"container", "callable"}:
            return _Declaration("statement", "fields", None, member, member)
        return declaration

    # ---------------------------------------------------------------- splitting

    def _inner_nodes(self, node: Node, depth: int = 0) -> list[Node]:
        """Children to cut between. A child that spans almost the whole node, such
        as the callback in ``describe(..., () => {...})``, is descended into."""
        children = node.named_children
        if not children:
            return []
        span = self._span(node)
        if depth < _MAX_DESCENT:
            for child in children:
                if self._span(child) >= _DOMINANT_CHILD_RATIO * span and child.named_children:
                    return self._inner_nodes(child, depth + 1) or children
        return children

    def _split(self, start: int, end: int, nodes: list[Node]) -> list[tuple[int, int]]:
        if self._fits(start, end):
            return [(start, end)]

        ranges: list[tuple[int, int]] = []
        cursor = start
        comment_run_start: int | None = None
        comment_run_end = 0
        for node in nodes:
            node_start = max(self._start(node), cursor)
            node_end = min(self._end(node), end)
            if node_end < cursor:
                continue
            is_comment = node.type in self.spec.comment_types
            if self._fits(cursor, node_end):
                if is_comment:
                    if comment_run_start is None or node_start - comment_run_end > 1:
                        comment_run_start = node_start
                    comment_run_end = node_end
                else:
                    comment_run_start = None
                continue

            # A comment directly above a statement moves with that statement.
            cut = node_start
            if comment_run_start is not None and node_start - comment_run_end <= 1:
                cut = comment_run_start
            comment_run_start = None
            if cut > cursor:
                ranges.extend(self._hard_split(cursor, cut - 1))
                cursor = cut
            if self._fits(cursor, node_end):
                continue

            inner = self._inner_nodes(node)
            pieces = self._split(cursor, node_end, inner) if inner else self._hard_split(cursor, node_end)
            ranges.extend(pieces[:-1])
            cursor = pieces[-1][0]

        ranges.extend(self._hard_split(cursor, end))
        return self._merge_closers(ranges)

    def _hard_split(self, start: int, end: int) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        cursor = start
        for line in range(start, end + 1):
            if line > cursor and not self._fits(cursor, line):
                ranges.append((cursor, line - 1))
                cursor = line
        ranges.append((cursor, end))
        return ranges

    def _merge_closers(self, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Fold pieces holding nothing but closing brackets into the previous piece."""
        merged: list[tuple[int, int]] = []
        for start, end in ranges:
            only_closers = all(_CLOSERS_ONLY.match(line) for line in self.lines[start - 1 : end])
            if only_closers and merged:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        return merged

    # ----------------------------------------------------------------- emission

    def _emit_ranges(
        self,
        kind: str,
        ranges: list[tuple[int, int]],
        symbol: str | None = None,
        parent: str | None = None,
        signature: str | None = None,
    ) -> None:
        trimmed = [t for t in (self._trim_blank(start, end) for start, end in ranges) if t is not None]
        for index, (start, end) in enumerate(trimmed, start=1):
            self.chunks.append(
                Chunk(
                    path=self.path,
                    language=self.language,
                    kind=kind,
                    start_line=start,
                    end_line=end,
                    content="\n".join(self.lines[start - 1 : end]),
                    symbol=symbol,
                    parent=parent,
                    signature=signature,
                    part_index=index,
                    part_total=len(trimmed),
                    is_test=self.is_test,
                )
            )

    def _trim_blank(self, start: int, end: int) -> tuple[int, int] | None:
        while start <= end and not self.lines[start - 1].strip():
            start += 1
        while end >= start and not self.lines[end - 1].strip():
            end -= 1
        return (start, end) if start <= end else None
