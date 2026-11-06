"""Chunk boundaries, symbol extraction and line-reference exactness."""

from __future__ import annotations

import textwrap

import pytest

from repomind.ingest.filters import normalize_source
from repomind.parsing.chunker import ChunkingConfig, parse_source

JAVA_SERVICE = textwrap.dedent(
    """\
    package com.example.orders;

    import java.util.List;
    import static java.util.Objects.requireNonNull;

    /**
     * Cancels orders.
     */
    @Service
    public class OrderService {
        private final OrderRepository repository;
        private final Clock clock;

        public OrderService(OrderRepository repository, Clock clock) {
            this.repository = requireNonNull(repository);
            this.clock = clock;
        }

        public String getName() { return "orders"; }
        public int getSize() { return 1; }

        /** Cancels one order. */
        @Transactional
        public void cancel(long id) {
            Order order = repository.findById(id).orElseThrow();
            order.markCancelled(clock.instant());
            repository.save(order);
        }

        public void placeholder01() { work(); work(); work(); work(); }

        public void placeholder02() {
            work();
            work();
            work();
            work();
            work();
            work();
            work();
            work();
            work();
            work();
            work();
            work();
            work();
        }

        record Summary(long id, String state) {
            String label() { return id + state; }
        }
    }
    """
)

TS_MODULE = textwrap.dedent(
    """\
    import { Injectable } from '@nestjs/common';
    import type { Note } from './note';
    export * from './reexported';

    const LIMIT = 10;

    /** Loads notes. */
    export const loadNotes = async (id: string): Promise<Note[]> => {
      return fetchNotes(id, LIMIT);
    };

    export function helper(a: number): number {
      return a + 1;
    }

    export interface Options {
      limit: number;
    }

    export type Alias = string | number;

    export enum Mode { Read, Write }

    @Injectable()
    export class NotesService {
      constructor(private readonly db: Db) {}

      @Roles('editor')
      async update(id: string): Promise<void> {
        await this.db.update(id);
      }

      private handle = async (event: Event): Promise<void> => {
        await this.db.log(event);
      };
    }
    """
)


def _assert_lines_are_exact(text: str, parsed) -> None:
    lines = text.split("\n")
    for chunk in parsed.chunks:
        assert chunk.start_line >= 1
        assert chunk.end_line <= len(lines)
        assert chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])


def _symbol(parsed, qualified_name):
    matches = [s for s in parsed.symbols if s.qualified_name == qualified_name]
    assert matches, f"{qualified_name} not in {[s.qualified_name for s in parsed.symbols]}"
    return matches[0]


class TestJava:
    # A container limit below the class size forces member-level chunks here.
    parsed = parse_source("src/main/java/OrderService.java", "java", JAVA_SERVICE, ChunkingConfig(small_container_lines=10))

    def test_extracts_classes_methods_constructors_and_nested_types(self):
        kinds = {s.qualified_name: s.kind for s in self.parsed.symbols}
        assert kinds["OrderService"] == "class"
        assert kinds["OrderService.OrderService"] == "constructor"
        assert kinds["OrderService.cancel"] == "method"
        assert kinds["OrderService.Summary"] == "record"
        assert kinds["OrderService.Summary.label"] == "method"

    def test_extracts_imports_but_does_not_make_them_retrievable(self):
        assert [i.module for i in self.parsed.imports] == ["java.util.List", "java.util.Objects.requireNonNull"]
        assert all("import " not in c.content for c in self.parsed.chunks)

    def test_symbol_line_numbers_point_at_the_declaration(self):
        lines = JAVA_SERVICE.split("\n")
        cancel = _symbol(self.parsed, "OrderService.cancel")
        assert "public void cancel" in "\n".join(lines[cancel.start_line - 1 : cancel.end_line])
        assert lines[cancel.end_line - 1].strip() == "}"

    def test_method_chunk_keeps_its_javadoc_and_annotation(self):
        chunk = next(c for c in self.parsed.chunks if c.symbol == "OrderService.cancel")
        assert chunk.kind == "method"
        assert chunk.content.lstrip().startswith("/** Cancels one order. */")
        assert "@Transactional" in chunk.content
        assert chunk.parent == "OrderService"

    def test_class_header_contains_doc_annotation_and_fields(self):
        header = next(c for c in self.parsed.chunks if c.kind == "class_header")
        assert header.content.startswith("/**")
        assert "@Service" in header.content
        assert "private final Clock clock;" in header.content
        assert "public OrderService(" not in header.content

    def test_adjacent_one_liners_are_grouped(self):
        grouped = next(c for c in self.parsed.chunks if c.kind == "methods")
        assert {"getName", "getSize"} <= set(grouped.symbols)
        assert grouped.symbol == "OrderService"

    def test_lines_are_exact(self):
        _assert_lines_are_exact(JAVA_SERVICE, self.parsed)

    def test_small_class_is_a_single_chunk(self):
        source = "public class Small {\n  int a;\n  void f() { a = 1; }\n}\n"
        parsed = parse_source("Small.java", "java", source)
        assert [(c.kind, c.symbol, c.start_line, c.end_line) for c in parsed.chunks] == [("class", "Small", 1, 4)]
        assert {"Small", "f"} <= set(parsed.chunks[0].symbols)


class TestTypeScript:
    parsed = parse_source("src/notes.ts", "typescript", TS_MODULE, ChunkingConfig(small_container_lines=5))

    def test_extracts_all_declaration_kinds(self):
        kinds = {s.qualified_name: s.kind for s in self.parsed.symbols}
        assert kinds["loadNotes"] == "function"
        assert kinds["helper"] == "function"
        assert kinds["Options"] == "interface"
        assert kinds["Alias"] == "type"
        assert kinds["Mode"] == "enum"
        assert kinds["NotesService"] == "class"
        assert kinds["NotesService.update"] == "method"
        assert kinds["NotesService.handle"] == "method"

    def test_imports_and_reexports_are_captured(self):
        modules = [i.module for i in self.parsed.imports]
        assert modules == ["@nestjs/common", "./note", "./reexported"]
        assert "Injectable" in self.parsed.imports[0].names

    def test_arrow_function_keeps_leading_comment(self):
        chunk = next(c for c in self.parsed.chunks if c.symbol == "loadNotes")
        assert chunk.content.startswith("/** Loads notes. */")

    def test_decorator_stays_with_class_and_method(self):
        header = next(c for c in self.parsed.chunks if c.kind == "class_header")
        assert header.content.startswith("@Injectable()")
        update = next(c for c in self.parsed.chunks if c.symbol == "NotesService.update")
        assert update.content.lstrip().startswith("@Roles('editor')")

    def test_lines_are_exact(self):
        _assert_lines_are_exact(TS_MODULE, self.parsed)


def test_javascript_and_jsx():
    source = textwrap.dedent(
        """\
        const express = require('express');

        class Router {
          route(path) {
            return path;
          }
        }

        function App() {
          return <div className="app">hello</div>;
        }

        module.exports = { Router, App };
        """
    )
    parsed = parse_source("src/app.jsx", "javascript", source)
    names = {s.qualified_name for s in parsed.symbols}
    assert {"Router", "Router.route", "App"} <= names
    _assert_lines_are_exact(source, parsed)
    assert not parsed.has_syntax_errors


def test_tsx_component():
    source = "export function Panel({ title }: Props) {\n  return <h1>{title}</h1>;\n}\n"
    parsed = parse_source("src/panel.tsx", "tsx", source)
    assert parsed.chunks[0].symbol == "Panel"
    assert not parsed.has_syntax_errors


class TestLargeFunctions:
    config = ChunkingConfig(max_lines=20, max_chars=600)

    @staticmethod
    def _java_method(statements: int) -> str:
        body = "\n".join(f"        step{i}(argument, otherArgument);" for i in range(statements))
        return f"class Big {{\n    void run(int argument) {{\n{body}\n    }}\n}}\n"

    def test_parts_are_contiguous_and_cover_the_whole_method(self):
        source = self._java_method(120)
        parsed = parse_source("Big.java", "java", source, self.config)
        parts = [c for c in parsed.chunks if c.symbol == "Big.run"]
        assert len(parts) > 1
        assert [p.part_index for p in parts] == list(range(1, len(parts) + 1))
        assert all(p.part_total == len(parts) for p in parts)
        method = _symbol(parsed, "Big.run")
        assert parts[0].start_line == method.start_line
        assert parts[-1].end_line == method.end_line
        for previous, current in zip(parts, parts[1:]):
            assert current.start_line == previous.end_line + 1
        _assert_lines_are_exact(source, parsed)

    def test_parts_respect_the_budget(self):
        parsed = parse_source("Big.java", "java", self._java_method(120), self.config)
        for chunk in parsed.chunks:
            assert chunk.end_line - chunk.start_line + 1 <= self.config.max_lines
            assert len(chunk.content) <= self.config.max_chars

    def test_every_part_keeps_the_signature_for_context(self):
        parsed = parse_source("Big.java", "java", self._java_method(120), self.config)
        parts = [c for c in parsed.chunks if c.symbol == "Big.run"]
        assert all("void run(int argument)" in p.signature for p in parts)

    def test_cuts_fall_on_statement_boundaries(self):
        source = "class S {\n  void run() {\n"
        for i in range(30):
            source += f"    if (c{i}) {{\n      a{i}();\n      b{i}();\n    }}\n"
        source += "  }\n}\n"
        parsed = parse_source("S.java", "java", source, ChunkingConfig(max_lines=20, max_chars=5000))
        parts = [c for c in parsed.chunks if c.symbol == "S.run"]
        assert len(parts) > 1
        for part in parts[1:]:
            assert part.content.lstrip().startswith("if (c")

    def test_single_oversized_statement_is_split_inside_its_block(self):
        inner = "\n".join(f"      call{i}();" for i in range(80))
        source = f"class S {{\n  void run() {{\n    try {{\n{inner}\n    }} finally {{\n      done();\n    }}\n  }}\n}}\n"
        parsed = parse_source("S.java", "java", source, ChunkingConfig(max_lines=20, max_chars=5000))
        parts = [c for c in parsed.chunks if c.symbol == "S.run"]
        assert len(parts) >= 4
        assert all(p.end_line - p.start_line + 1 <= 21 for p in parts)
        _assert_lines_are_exact(source, parsed)

    def test_no_part_consists_only_of_closing_brackets(self):
        parsed = parse_source("Big.java", "java", self._java_method(120), self.config)
        for chunk in parsed.chunks:
            assert any(ch not in " \n})];," for ch in chunk.content)

    def test_oversized_spec_callback_is_split(self):
        cases = "\n".join(f"  it('case {i}', () => {{\n    expect(run({i})).toBe(true);\n  }});" for i in range(80))
        source = f"describe('suite', () => {{\n{cases}\n}});\n"
        parsed = parse_source("src/x.spec.ts", "typescript", source, self.config)
        assert len(parsed.chunks) > 1
        assert all(c.is_test for c in parsed.chunks)
        assert all(c.end_line - c.start_line + 1 <= self.config.max_lines for c in parsed.chunks)
        _assert_lines_are_exact(source, parsed)

    def test_a_single_very_long_line_stays_intact(self):
        source = "const data = '" + "x" * 3000 + "';\n"
        parsed = parse_source("d.js", "javascript", source, self.config)
        assert [(c.start_line, c.end_line) for c in parsed.chunks] == [(1, 1)]


class TestLineNumbers:
    def test_crlf_does_not_shift_lines(self):
        crlf = JAVA_SERVICE.replace("\n", "\r\n").encode()
        text = normalize_source(crlf)
        parsed = parse_source("O.java", "java", text, ChunkingConfig(small_container_lines=10))
        expected = parse_source("O.java", "java", JAVA_SERVICE, ChunkingConfig(small_container_lines=10))
        assert [(c.start_line, c.end_line, c.content) for c in parsed.chunks] == [
            (c.start_line, c.end_line, c.content) for c in expected.chunks
        ]

    @pytest.mark.parametrize("separator", ["\f", "\v", " ", "\x85"])
    def test_unusual_separators_are_not_line_breaks(self, separator):
        source = f"// a{separator}b\nfunction first() {{ return 1; }}\n\nfunction second() {{ return 2; }}\n"
        parsed = parse_source("s.js", "javascript", source)
        assert next(c for c in parsed.chunks if c.symbol == "second").start_line == 4
        _assert_lines_are_exact(source, parsed)

    def test_multibyte_characters_do_not_shift_lines(self):
        source = "// Türkçe açıklama: şğıöç 🚀\nfunction işlem() {\n  return 'ğ';\n}\n"
        parsed = parse_source("t.js", "javascript", source)
        chunk = next(c for c in parsed.chunks if c.symbol == "işlem")
        assert (chunk.start_line, chunk.end_line) == (1, 4)
        assert "🚀" in chunk.content

    def test_bom_is_removed_before_parsing(self):
        assert normalize_source(b"\xef\xbb\xbfclass A {}\n") == "class A {}\n"

    def test_missing_trailing_newline(self):
        parsed = parse_source("a.js", "javascript", "function f() {\n  return 1;\n}")
        assert (parsed.chunks[0].start_line, parsed.chunks[0].end_line) == (1, 3)


class TestRobustness:
    def test_syntax_errors_are_flagged_and_code_is_kept(self):
        source = "function ok() { return 1; }\n\nfunction broken( { return\n\nfunction after() { return 2; }\n"
        parsed = parse_source("b.js", "javascript", source)
        assert parsed.has_syntax_errors
        assert parsed.chunks
        _assert_lines_are_exact(source, parsed)

    def test_empty_file_has_no_chunks(self):
        assert parse_source("e.ts", "typescript", "").chunks == []

    def test_comment_only_file_is_kept(self):
        parsed = parse_source("c.ts", "typescript", "// only a comment\n")
        assert parsed.chunks[0].content == "// only a comment"

    def test_test_paths_are_flagged(self):
        assert parse_source("src/test/java/A.java", "java", "class A {}\n").chunks[0].is_test
        assert not parse_source("src/main/java/A.java", "java", "class A {}\n").chunks[0].is_test

    def test_source_is_parsed_not_executed(self, tmp_path):
        marker = tmp_path / "executed"
        source = f"require('fs').writeFileSync({str(marker)!r}, 'x');\nprocess.exit(1);\n"
        parse_source("evil.js", "javascript", source)
        assert not marker.exists()
