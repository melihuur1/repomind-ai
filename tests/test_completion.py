"""Completing passages with the declaration of a constant they use."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from repomind.qa.completion import MAX_COMPLETIONS, SnapshotSources, complete, declarations, resolve, unshown_values
from repomind.qa.passages import Passage

JAVA = """package shop;

class Simulation {
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(60);
    private static final String PREFIX_NAME = "__sim-";

    Result reserve(String target) {
        int REQUEST_TIMEOUT_LOCAL = 1;
        return send(target, REQUEST_TIMEOUT);
    }
}
"""
TYPESCRIPT = """import { MAX_TITLE_LENGTH } from './limits';
import { SHARED_LIMIT } from '@acme/shared';

export function title(text: string): string {
  return text.slice(0, MAX_TITLE_LENGTH).padEnd(SHARED_LIMIT);
}
"""
LIMITS = """export const MAX_TITLE_LENGTH = 200;
export const DB_PASSWORD_HINT = 'password: hunter2';
"""


def chunk(path: str, start: int, end: int, text: str, language: str = "java"):
    lines = text.split("\n")[start - 1 : end]
    return SimpleNamespace(id=hash((path, start)), repo="shop", commit_sha="c1", path=path, language=language, symbol=None,
                           start_line=start, end_line=end, content="\n".join(lines))


class Files:
    """Indexed files with their chunks, imports and declared types, and a record of every file read."""

    def __init__(self, files: dict, chunks: dict | None = None, imports: dict | None = None, types: dict | None = None):
        self.files, self._chunks, self._imports, self._types = files, chunks or {}, imports or {}, types or {}
        self.read: list[str] = []

    def language(self, path):
        return self.files[path][0] if path in self.files else None

    def text(self, path):
        self.read.append(path)
        return self.files[path][1] if path in self.files else None

    def chunks(self, path):
        return self._chunks.get(path, [])

    def imports(self, path):
        return self._imports.get(path, [])

    def type_paths(self, name):
        return self._types.get(name, [])


def passage(path: str, start: int, end: int, text: str, language: str = "java") -> Passage:
    owner = chunk(path, start, end, text, language)
    return Passage("P1", owner, start, end, owner.content)


class TestDeclarations:
    def test_a_java_field_is_found_by_its_syntax(self):
        assert declarations("java", JAVA, "REQUEST_TIMEOUT") == [(4, 4)]

    def test_a_name_that_only_starts_the_same_is_not_a_declaration(self):
        assert declarations("java", JAVA, "REQUEST") == []
        assert declarations("java", JAVA, "REQUEST_TIMEOUT_LOCAL") == []  # a local variable, not a field

    def test_an_exported_typescript_constant_is_found_with_its_export(self):
        assert declarations("typescript", LIMITS, "MAX_TITLE_LENGTH") == [(1, 1)]

    def test_a_class_field_in_typescript_is_found(self):
        source = "export class Limits {\n  static readonly MAX_ITEMS = 50;\n}\n"
        assert declarations("typescript", source, "MAX_ITEMS") == [(2, 2)]

    def test_a_name_in_a_string_or_a_comment_is_not_a_declaration(self):
        source = '// REQUEST_TIMEOUT = 5\nclass A { String s = "REQUEST_TIMEOUT = 5"; }\n'
        assert declarations("java", source, "REQUEST_TIMEOUT") == []

    def test_an_unsupported_language_declares_nothing(self):
        assert declarations("yaml", "REQUEST_TIMEOUT: 5\n", "REQUEST_TIMEOUT") == []


JAVA_PATH = "src/Simulation.java"


def java_files(**extra):
    return Files({JAVA_PATH: ("java", JAVA)}, chunks={JAVA_PATH: [chunk(JAVA_PATH, 3, 5, JAVA), chunk(JAVA_PATH, 7, 10, JAVA)]}, **extra)


class TestComplete:
    def test_the_declaration_follows_the_passage_that_uses_it(self):
        use = passage(JAVA_PATH, 7, 10, JAVA)
        completed, record = complete([use], java_files())
        assert [(p.id, p.start_line, p.end_line) for p in completed] == [("P1", 7, 10), ("P2", 4, 4)]
        assert completed[1].text == "    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(60);"
        assert completed[1].chunk.start_line == 3, "taken from the indexed chunk that holds it"
        assert [(c.name, c.reason) for c in record if c.declaration] == [("REQUEST_TIMEOUT", "")]

    def test_the_passage_lines_are_kept_exactly(self):
        use = passage(JAVA_PATH, 7, 10, JAVA)
        completed, _ = complete([use], java_files())
        assert completed[0].text == use.text and (completed[0].start_line, completed[0].end_line) == (7, 10)

    def test_nothing_is_added_when_the_declaration_is_already_shown(self):
        shown = [passage(JAVA_PATH, 3, 5, JAVA), passage(JAVA_PATH, 7, 10, JAVA)]
        completed, record = complete(shown, java_files())
        assert len(completed) == 2
        assert ("REQUEST_TIMEOUT", "already shown") in [(c.name, c.reason) for c in record]

    def test_a_declaration_outside_every_indexed_chunk_is_not_added(self):
        files = java_files()
        files._chunks[JAVA_PATH] = [chunk(JAVA_PATH, 7, 10, JAVA)]  # lines 3-5 withheld, or never indexed
        completed, record = complete([passage(JAVA_PATH, 7, 10, JAVA)], files)
        assert len(completed) == 1
        assert ("REQUEST_TIMEOUT", "the declaration is not in the index") in [(c.name, c.reason) for c in record]

    def test_two_declarations_of_the_name_in_the_file_are_not_guessed_between(self):
        twice = JAVA.replace("class Simulation {", "class Simulation {\n    static class Inner { static final int REQUEST_TIMEOUT = 5; }")
        files = Files({JAVA_PATH: ("java", twice)}, chunks={JAVA_PATH: [chunk(JAVA_PATH, 1, 12, twice)]})
        _, record = complete([passage(JAVA_PATH, 9, 11, twice)], files)
        assert any(c.name == "REQUEST_TIMEOUT" and c.declaration is None and "declared 2 times" in c.reason for c in record)

    def test_a_declaration_holding_a_credential_is_not_added(self):
        source = 'class A {\n    static final String DB_URL_VALUE = "postgresql://app:hunter2@db/app";\n    void f() { connect(DB_URL_VALUE); }\n}\n'
        files = Files({"A.java": ("java", source)}, chunks={"A.java": [chunk("A.java", 1, 4, source)]})
        completed, record = complete([passage("A.java", 3, 3, source)], files)
        assert len(completed) == 1 and "hunter2" not in "\n".join(p.text for p in completed)
        assert ("DB_URL_VALUE", "the declaration may hold a credential") in [(c.name, c.reason) for c in record]

    def test_at_most_two_declarations_are_added(self):
        names = [f"LIMIT_{n}" for n in range(4)]
        source = "class A {\n" + "".join(f"    static final int {name} = {n};\n" for n, name in enumerate(names)) + f"    int f() {{ return {' + '.join(names)}; }}\n}}\n"
        files = Files({"A.java": ("java", source)}, chunks={"A.java": [chunk("A.java", 1, 7, source)]})
        completed, _ = complete([passage("A.java", 6, 6, source)], files)
        assert len(completed) == 1 + MAX_COMPLETIONS


class TestAcrossFiles:
    def files(self):
        return Files(
            {"src/title.ts": ("typescript", TYPESCRIPT), "src/limits.ts": ("typescript", LIMITS), "src/other.ts": ("typescript", "export const SHARED_LIMIT = 9;\n")},
            chunks={"src/limits.ts": [chunk("src/limits.ts", 1, 2, LIMITS, "typescript")], "src/other.ts": [chunk("src/other.ts", 1, 1, "export const SHARED_LIMIT = 9;\n", "typescript")]},
            imports={"src/title.ts": [("./limits", ["MAX_TITLE_LENGTH"]), ("@acme/shared", ["SHARED_LIMIT"])]},
        )

    def test_a_constant_imported_from_a_relative_module_is_found_there(self):
        files = self.files()
        declaration, reason = resolve(files, "MAX_TITLE_LENGTH", "src/title.ts", TYPESCRIPT)
        assert (declaration.path, declaration.start_line, reason) == ("src/limits.ts", 1, "")

    def test_a_same_named_constant_in_a_file_it_does_not_import_is_never_used(self):
        files = self.files()
        declaration, reason = resolve(files, "SHARED_LIMIT", "src/title.ts", TYPESCRIPT)
        assert declaration is None and "@acme/shared" in reason, "src/other.ts also declares it, and is not what the file imports"

    def test_java_follows_a_qualified_constant_only_to_the_one_type_of_that_name(self):
        holder = "class Limits {\n    static final int MAX_ITEMS = 50;\n}\n"
        user = "class Cart {\n    boolean full(int n) { return n >= Limits.MAX_ITEMS; }\n}\n"
        files = Files({"a/Cart.java": ("java", user), "a/Limits.java": ("java", holder)}, types={"Limits": ["a/Limits.java"]})
        assert resolve(files, "MAX_ITEMS", "a/Cart.java", user)[0].path == "a/Limits.java"
        files._types["Limits"] = ["a/Limits.java", "b/Limits.java"]
        declaration, reason = resolve(files, "MAX_ITEMS", "a/Cart.java", user)
        assert declaration is None and reason == "2 types are called Limits"

    def test_an_enum_value_behind_a_qualifier_is_not_a_declaration_and_says_so(self):
        holder = "enum Action {\n    PRODUCT_CREATED, PRODUCT_RENAMED\n}\n"
        user = "class Audit {\n    void on() { record(Action.PRODUCT_CREATED); }\n}\n"
        files = Files({"a/Audit.java": ("java", user), "a/Action.java": ("java", holder)}, types={"Action": ["a/Action.java"]})
        assert resolve(files, "PRODUCT_CREATED", "a/Audit.java", user) == (None, "no declaration of PRODUCT_CREATED was found")

    def test_an_unqualified_java_constant_from_elsewhere_is_left_alone(self):
        user = "class Cart {\n    boolean full(int n) { return n >= MAX_ITEMS; }\n}\n"
        files = Files({"a/Cart.java": ("java", user), "a/Limits.java": ("java", "class Limits { static final int MAX_ITEMS = 50; }\n")}, types={"Limits": ["a/Limits.java"]})
        assert resolve(files, "MAX_ITEMS", "a/Cart.java", user)[0] is None


class TestSnapshotSources:
    def test_a_file_outside_the_index_is_never_read_from_git(self):
        store = SimpleNamespace(indexed_file=lambda snapshot, path: {"language": "java"} if path == "A.java" else None)

        class Git:
            calls: list = []

            def read_file(self, commit, path):
                self.calls.append(path)
                return b"class A {}"

        git = Git()
        sources = SnapshotSources(store, 1, git, "c1")
        assert sources.text(".env") is None and sources.text("A.java") == "class A {}"
        assert git.calls == ["A.java"]


class TestTellingTheReader:
    def test_a_value_offered_but_not_chosen_is_named(self):
        use = passage(JAVA_PATH, 7, 10, JAVA)
        completed, record = complete([use], java_files())
        notes = unshown_values([completed[0]], record)
        assert notes == [f"The chosen lines use REQUEST_TIMEOUT, whose value is at {JAVA_PATH}:4, which was not chosen."]
        assert unshown_values(completed, record) == []

    def test_an_unresolvable_value_is_said_to_be_unknown_and_an_unknown_name_is_not(self):
        twice = JAVA.replace("class Simulation {", "class Simulation {\n    static class Inner { static final int REQUEST_TIMEOUT = 5; }")
        files = Files({JAVA_PATH: ("java", twice)}, chunks={JAVA_PATH: [chunk(JAVA_PATH, 1, 12, twice)]})
        use = passage(JAVA_PATH, 9, 11, twice)
        completed, record = complete([use], files)
        notes = unshown_values([completed[0]], record)
        assert len(notes) == 1 and re.search(r"REQUEST_TIMEOUT, whose value is not shown: .*declared 2 times", notes[0])
        enum_like = passage("B.java", 1, 1, "return Status.ORDER_PLACED;")
        assert unshown_values([enum_like], complete([enum_like], Files({"B.java": ("java", "class B {}\n")}))[1]) == []


@pytest.mark.db
def test_in_ask_the_declaration_is_offered_checked_against_git_and_named_when_not_chosen(store, tmp_path, fake_embedder):
    import json

    from repomind.indexing import index_repository
    from repomind.qa.engine import QAEngine
    from repomind.search.engine import SearchEngine
    from tests.conftest import commit_all, git, write
    from tests.test_qa import LOCAL, FakeClient

    root = tmp_path / "sim"
    root.mkdir()
    git(root, "init", "-b", "main")
    filler = "".join(f"    void helper{n}() {{ log(\"step {n}\"); }}\n" for n in range(25))
    write(root, "src/Client.java",
          # The declaration shares no word with the question, so only completion can bring it.
          "class Client {\n    private static final Duration HTTP_LIMIT = Duration.ofSeconds(60);\n" + filler
          + "    /** Which timeout the simulation uses for each reserve request. */\n"
          + "    Result reserve(String target) {\n        return http.send(request(target).timeout(HTTP_LIMIT));\n    }\n}\n")
    commit_all(root, "sim")
    index_repository(store, str(root), fake_embedder, name="sim")

    def choose_the_use(user: str) -> str:
        use = re.search(r'id="(P\d+)"[^>]*>[^<]*\.timeout\(HTTP_LIMIT\)', user)
        return json.dumps({"chosen": [use.group(1)] if use else [], "note": "It times out."})

    model = FakeClient(choose_the_use)
    # Every word is in the code, so the refusal check lets the question reach the passages.
    result = QAEngine(store, SearchEngine(store, fake_embedder), LOCAL, model).ask("Which timeout does the simulation use for each reserve request?", ["sim"])
    shown = {(p.start_line, p.end_line): p.text for p in result.passages}
    assert result.passages, f"{result.status}: {result.warnings}"
    assert (2, 2) in shown and "Duration.ofSeconds(60)" in shown[(2, 2)], "the declaration is offered, verified against Git"
    assert "HTTP_LIMIT = Duration.ofSeconds(60)" in model.calls[-1]["user"]
    assert result.status == "answered"
    assert any("HTTP_LIMIT, whose value is at src/Client.java:2, which was not chosen" in w for w in result.warnings)
    assert [(c.name, c.reason) for c in result.completions if c.declaration] == [("HTTP_LIMIT", "")]
