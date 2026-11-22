"""Completing passages with the declaration of a constant they use."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from dataclasses import replace

from repomind.qa.completion import MAX_COMPLETIONS, SnapshotSources, complete, linked_evidence, unshown_values
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
    """Indexed files with their chunks and declared types, and a record of every file read."""

    def __init__(self, files: dict, chunks: dict | None = None, types: dict | None = None):
        self.files, self._chunks, self._types = files, chunks or {}, types or {}
        self.read: list[str] = []

    def language(self, path):
        return self.files[path][0] if path in self.files else None

    def text(self, path):
        self.read.append(path)
        return self.files[path][1] if path in self.files else None

    def chunks(self, path):
        return self._chunks.get(path, [])

    def type_paths(self, name):
        return self._types.get(name, [])


def passage(path: str, start: int, end: int, text: str, language: str = "java") -> Passage:
    owner = chunk(path, start, end, text, language)
    return Passage("P1", owner, start, end, owner.content)


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
        # The local variable is declared in the passage itself, so there is nothing to add for it.
        assert [(c.name, c.reason) for c in record if c.declaration] == [("REQUEST_TIMEOUT_LOCAL", "already shown"), ("REQUEST_TIMEOUT", "")]

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

    def test_two_declarations_in_one_scope_are_not_guessed_between(self):
        twice = "var RETRY_LIMIT = 1;\nvar RETRY_LIMIT = 2;\nexport const tries = () => RETRY_LIMIT;\n"
        files = Files({"a.js": ("javascript", twice)}, chunks={"a.js": [chunk("a.js", 1, 3, twice, "javascript")]})
        completed, record = complete([passage("a.js", 3, 3, twice, "javascript")], files)
        assert len(completed) == 1
        assert [(c.name, c.declaration, c.reason) for c in record] == [("RETRY_LIMIT", None, "RETRY_LIMIT is declared 2 times in a.js")]

    def test_a_same_named_constant_of_another_function_is_never_added(self):
        source = (
            "function first() {\n  const PAGE_SIZE = 5;\n  return PAGE_SIZE;\n}\n\n"
            "export function second(rows: string[]) {\n  return rows.slice(0, PAGE_SIZE);\n}\n"
        )
        files = Files({"a.ts": ("typescript", source)}, chunks={"a.ts": [chunk("a.ts", 1, 8, source, "typescript")]})
        completed, record = complete([passage("a.ts", 6, 8, source, "typescript")], files)
        assert len(completed) == 1, "the constant of first() is not what second() means"
        assert [(c.name, c.declaration) for c in record] == [("PAGE_SIZE", None)]

    def test_each_passage_gets_the_declaration_its_own_file_imports(self):
        user = "import { PAGE_SIZE } from './limits';\nexport const page = (rows: string[]) => rows.slice(0, PAGE_SIZE);\n"
        sources = {
            "web/list.ts": ("typescript", user), "web/limits.ts": ("typescript", "export const PAGE_SIZE = 20;\n"),
            "admin/list.ts": ("typescript", user), "admin/limits.ts": ("typescript", "export const PAGE_SIZE = 500;\n"),
        }
        files = Files(sources, chunks={path: [chunk(path, 1, 1, text, "typescript")] for path, (_, text) in sources.items() if path.endswith("limits.ts")})
        completed, _ = complete([passage("web/list.ts", 2, 2, user, "typescript"), passage("admin/list.ts", 2, 2, user, "typescript")], files)
        assert [(p.path, p.text) for p in completed] == [
            ("web/list.ts", user.split("\n")[1]), ("web/limits.ts", "export const PAGE_SIZE = 20;"),
            ("admin/list.ts", user.split("\n")[1]), ("admin/limits.ts", "export const PAGE_SIZE = 500;"),
        ]

    def test_a_declaration_holding_a_credential_is_not_added(self):
        source = 'class A {\n    static final String DB_URL_VALUE = "postgresql://app:hunter2@db/app";\n    void f() { connect(DB_URL_VALUE); }\n}\n'
        files = Files({"A.java": ("java", source)}, chunks={"A.java": [chunk("A.java", 1, 4, source)]})
        completed, record = complete([passage("A.java", 3, 3, source)], files)
        assert len(completed) == 1 and "hunter2" not in "\n".join(p.text for p in completed)
        assert ("DB_URL_VALUE", "the declaration may hold a credential") in [(c.name, c.reason) for c in record]

    @pytest.mark.parametrize(
        ("language", "source", "use_line"),
        [
            ("java", 'class A {\n    private static final String JWT_SECRET = "fake-signing-value";\n    String sign(String u) { return signWith(u, JWT_SECRET); }\n}\n', 3),
            ("typescript", "export const API_TOKEN = 'fake-token-value';\nexport function call() {\n  return send(API_TOKEN);\n}\n", 3),
            ("typescript", "const DB_PASSWORD =\n  'fake-password-value';\nexport const pool = () => connect(DB_PASSWORD);\n", 3),
        ],
    )
    def test_a_credential_named_constant_given_a_literal_is_never_added(self, language, source, use_line):
        path = "A.java" if language == "java" else "a.ts"
        files = Files({path: (language, source)}, chunks={path: [chunk(path, 1, len(source.split("\n")) - 1, source, language)]})
        completed, record = complete([passage(path, use_line, use_line, source, language)], files)
        assert len(completed) == 1 and "fake-" not in "\n".join(p.text for p in completed)
        assert [c.reason for c in record if c.declaration] == ["the declaration may hold a credential"]
        assert "fake-" not in " ".join(f"{c.name} {c.used_in} {c.reason}" for c in record), "what is recorded names it, and never quotes it"

    def test_at_most_two_declarations_are_added(self):
        names = [f"LIMIT_{n}" for n in range(4)]
        source = "class A {\n" + "".join(f"    static final int {name} = {n};\n" for n, name in enumerate(names)) + f"    int f() {{ return {' + '.join(names)}; }}\n}}\n"
        files = Files({"A.java": ("java", source)}, chunks={"A.java": [chunk("A.java", 1, 7, source)]})
        completed, _ = complete([passage("A.java", 6, 6, source)], files)
        assert len(completed) == 1 + MAX_COMPLETIONS


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
        files = Files({"src/title.ts": ("typescript", TYPESCRIPT), "src/limits.ts": ("typescript", LIMITS)},
                      chunks={"src/limits.ts": [chunk("src/limits.ts", 1, 2, LIMITS, "typescript")]})
        use = passage("src/title.ts", 4, 6, TYPESCRIPT, "typescript")
        completed, record = complete([use], files)
        assert [(p.path, p.start_line) for p in completed] == [("src/title.ts", 4), ("src/limits.ts", 1)]
        notes = unshown_values(completed, record)
        assert len(notes) == 1 and re.search(r"SHARED_LIMIT, whose value is not shown: .*imported from @acme/shared", notes[0])
        enum_like = passage("B.java", 1, 1, "return Status.ORDER_PLACED;")
        assert unshown_values([enum_like], complete([enum_like], Files({"B.java": ("java", "class B {}\n")}))[1]) == []


class TestLinkedEvidence:
    """A declaration completion added counts as evidence only beside a use that does."""

    WORDING = "How long may a simulation request take before its timeout?"

    def pair(self, **declaration_changes):
        use = passage(JAVA_PATH, 7, 10, JAVA)
        completed, record = complete([use], java_files())
        use, declaration = completed
        if declaration_changes:
            declaration = Passage(declaration.id, SimpleNamespace(**{**vars(declaration.chunk), **declaration_changes}), declaration.start_line, declaration.end_line, declaration.text)
        return use, declaration, record

    @staticmethod
    def rule(*passing):
        return lambda p: any(p is item for item in passing)

    def test_a_declaration_chosen_alone_is_shown_after_the_use_that_passes_the_rule(self):
        use, declaration, record = self.pair()
        accepted, turned_down, notes = linked_evidence([declaration], [use, declaration], record, self.WORDING, self.rule(use))
        assert accepted == [use, declaration] and turned_down == []
        assert len(notes) == 1 and "declares REQUEST_TIMEOUT" in notes[0]
        assert "exist in Git" in notes[0] and "the model's reading" in notes[0], "the lines are checked, not what they mean"

    def test_a_use_chosen_alone_does_not_bring_its_declaration_in(self):
        use, declaration, record = self.pair()
        accepted, turned_down, notes = linked_evidence([use], [use, declaration], record, self.WORDING, self.rule(use))
        assert accepted == [use] and turned_down == [] and notes == []

    @pytest.mark.parametrize("order", ["use first", "declaration first"])
    def test_both_chosen_are_shown_once_each_use_first(self, order):
        use, declaration, record = self.pair()
        chosen = [use, declaration] if order == "use first" else [declaration, use]
        accepted, _, _ = linked_evidence(chosen, [use, declaration], record, self.WORDING, self.rule(use))
        assert accepted == [use, declaration]

    def test_a_use_that_fails_the_rule_lets_neither_in(self):
        use, declaration, record = self.pair()
        accepted, turned_down, _ = linked_evidence([declaration, use], [use, declaration], record, self.WORDING, self.rule())
        assert accepted == [] and turned_down == [declaration, use]

    def test_a_declaration_sharing_no_word_with_the_question_is_turned_down(self):
        use, declaration, record = self.pair()
        accepted, turned_down, _ = linked_evidence([declaration], [use, declaration], record, "Which prefix names the simulated users?", self.rule(use))
        assert accepted == [] and turned_down == [declaration]

    def test_a_use_that_was_not_offered_links_nothing(self):
        use, declaration, record = self.pair()
        accepted, turned_down, _ = linked_evidence([declaration], [declaration], record, self.WORDING, self.rule(use))
        assert accepted == [] and turned_down == [declaration]

    def test_a_declaration_from_another_commit_links_nothing(self):
        use, declaration, record = self.pair(commit_sha="c2")
        accepted, _, _ = linked_evidence([declaration], [use, declaration], record, self.WORDING, self.rule(use))
        assert accepted == []

    def test_a_same_named_declaration_search_found_elsewhere_links_nothing(self):
        use, _, record = self.pair()
        elsewhere = passage("src/Other.java", 4, 4, "class Other {\n\n\n    static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(5);\n}")
        accepted, turned_down, _ = linked_evidence([elsewhere], [use, elsewhere], record, self.WORDING, self.rule(use))
        assert accepted == [] and turned_down == [elsewhere]

    @pytest.mark.parametrize("reason", ["REQUEST_TIMEOUT is declared 2 times in src/Simulation.java", "the declaration may hold a credential",
                                        "the declaration is not in the index", "already shown"])
    def test_a_declaration_completion_did_not_add_links_nothing(self, reason):
        use, declaration, record = self.pair()
        refused = [replace(item, reason=reason) for item in record]
        accepted, _, _ = linked_evidence([declaration], [use, declaration], refused, self.WORDING, self.rule(use))
        assert accepted == []


@pytest.mark.db
def test_in_ask_the_declaration_is_offered_checked_against_git_and_shown_with_the_use_the_model_chose(store, tmp_path, fake_embedder):
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
    assert [(c.name, c.reason) for c in result.completions if c.declaration] == [("HTTP_LIMIT", "")]
    # The model chose the line that names the constant. The line that gives it its value is shown with it,
    # marked as added, so the answer holds both places a question about a value needs.
    assert [c.start_line for c in result.citations] == [use_line(result), 2]
    assert [(p.start_line, p.end_line) for p in result.declared_values] == [(2, 2)]
    assert [(s.passage.start_line, s.declares_a_value) for s in result.sources() if s.answer_check.value == "passed"] == [(use_line(result), False), (2, True)]
    assert not any("which was not chosen" in w for w in result.warnings), "the value is shown, so nothing is missing"
    assert result.review_candidates == []


def use_line(result) -> int:
    return next(p.start_line for p in result.passages if ".timeout(HTTP_LIMIT)" in p.text)


LINKED = (
    "class Client {\n"
    "    private static final Duration REQUEST_LIMIT = Duration.ofSeconds(60);\n"  # line 2: shares one word, "request"
    + "".join(f"    void helper{n}() {{ log(\"step {n}\"); }}\n" for n in range(25))
    + "    /** The simulation uses this timeout for each reserve request. */\n"  # line 28
    "    Result reserve(String target) {\n"
    "        return http.send(request(target).timeout(REQUEST_LIMIT));\n"  # line 30: passes the rule
    "    }\n"
    "    /** Sends one step. */\n"  # line 32
    "    void step() { http.send(REQUEST_LIMIT); }\n"  # line 33: shares one word, "request", from the constant's name
    "}\n"
)


@pytest.fixture
def linked(store, tmp_path, fake_embedder, monkeypatch):
    """A repository with a constant, a use that passes the rule and one that does not, and a way to choose what search offers."""
    from repomind.indexing import index_repository
    from repomind.qa import engine as engine_module
    from tests.conftest import commit_all, git, write

    root = tmp_path / "linked"
    root.mkdir()
    git(root, "init", "-b", "main")
    write(root, "src/Client.java", LINKED)
    commit_all(root, "linked")
    index_repository(store, str(root), fake_embedder, name="linked")
    snapshot = store.active_snapshots(["linked"])[0]
    chunks = store.file_chunks(snapshot.id, "src/Client.java")

    def offer(*spans):
        found = []
        for number, (start, end) in enumerate(spans, start=1):
            owner = next(c for c in chunks if c.start_line <= start and end <= c.end_line)
            found.append(Passage(f"P{number}", owner, start, end, "\n".join(owner.content.split("\n")[start - owner.start_line : end - owner.start_line + 1])))
        # Search and passage selection stay real everywhere else. Here they offer exactly these lines.
        monkeypatch.setattr(engine_module, "select_passages", lambda *args, **kwargs: found)

    return offer


def choose(*patterns: str):
    import json

    def reply(user: str) -> str:
        ids = [m.group(1) for pattern in patterns if (m := re.search(rf'id="(P\d+)"[^>]*>[^<]*{pattern}', user))]
        return json.dumps({"chosen": ids, "note": "Sixty seconds."})

    return reply


QUESTION = "Which timeout does the simulation use for each reserve request?"


@pytest.mark.db
class TestLinkedEvidenceInAsk:
    def ask(self, store, fake_embedder, model, question=QUESTION, linked_evidence=True):
        from repomind.qa.engine import QAEngine
        from repomind.search.engine import SearchEngine
        from tests.test_qa import LOCAL

        config = replace(LOCAL, linked_evidence=linked_evidence)
        return QAEngine(store, SearchEngine(store, fake_embedder), config, model).ask(question, ["linked"])

    def test_it_is_off_unless_it_is_asked_for(self, monkeypatch):
        from repomind.qa.llm import LLMConfig, load_llm_config

        assert LLMConfig(base_url="http://127.0.0.1:1/v1", model="fake").linked_evidence is False
        monkeypatch.setenv("REPOMIND_LLM_BASE_URL", "http://127.0.0.1:11535/v1")
        monkeypatch.setenv("REPOMIND_LLM_MODEL", "m")
        monkeypatch.delenv("REPOMIND_LLM_LINKED_EVIDENCE", raising=False)
        assert load_llm_config().linked_evidence is False
        for value, expected in (("1", True), ("0", False), ("", False), ("true", False), ("yes", False)):
            monkeypatch.setenv("REPOMIND_LLM_LINKED_EVIDENCE", value)
            assert load_llm_config().linked_evidence is expected, f"only 1 turns it on, not {value!r}"

    # The next test and the one named "...is_shown_with_the_use..." are the shape of sim-timeout in
    # eval/EVIDENCE_COMPLETION.md: the model chose only the declaration that holds the value. Off, it is
    # a source to review, as in that record. On, it is shown with the use.
    def test_off_a_declaration_chosen_alone_is_only_something_to_review(self, store, fake_embedder, linked):
        from tests.test_qa import FakeClient

        linked((28, 31))  # the use passes the rule and was offered, which is all the link needs when it is on
        result = self.ask(store, fake_embedder, FakeClient(choose("REQUEST_LIMIT = Duration")), linked_evidence=False)
        assert result.status == "insufficient"
        assert result.citations == [] and result.linked_uses == []
        assert [(p.start_line, p.end_line) for p in result.review_candidates] == [(2, 2)]
        assert [c.used_in for c in result.completions if c.declaration and not c.reason] == ["src/Client.java:28-31"], "completion still works"

    def test_off_both_chosen_show_the_use_and_its_value(self, store, fake_embedder, linked):
        from tests.test_qa import FakeClient

        linked((28, 31))
        result = self.ask(store, fake_embedder, FakeClient(choose("REQUEST_LIMIT = Duration", r"\.timeout\(REQUEST_LIMIT")), linked_evidence=False)
        assert result.status == "answered"
        # The use passes the evidence rule and is the answer. The declaration does not pass it, and is
        # shown as the value of a constant the use names, not as an answer of its own or as something to review.
        assert [(c.start_line, c.end_line) for c in result.citations] == [(28, 31), (2, 2)]
        assert [(p.start_line, p.end_line) for p in result.declared_values] == [(2, 2)]
        assert result.review_candidates == [] and result.linked_uses == []

    def test_on_the_use_the_system_added_is_marked_as_not_chosen_by_the_model(self, store, fake_embedder, linked):
        from tests.test_qa import FakeClient

        linked((28, 31))
        result = self.ask(store, fake_embedder, FakeClient(choose("REQUEST_LIMIT = Duration")))
        assert [(p.start_line, p.end_line) for p in result.linked_uses] == [(28, 31)]
        assert [s.added_by_link for s in result.sources() if s.answer_check.value == "passed"] == [True, False]
        assert any("was added by RepoMind and not chosen by the model" in w for w in result.warnings)
        both = self.ask(store, fake_embedder, FakeClient(choose("REQUEST_LIMIT = Duration", r"\.timeout\(REQUEST_LIMIT")))
        assert both.linked_uses == [] and not any(s.added_by_link for s in both.sources())

    def test_the_declaration_chosen_alone_is_shown_with_the_use_both_checked_against_git(self, store, fake_embedder, linked):
        from tests.test_qa import FakeClient

        linked((28, 31))
        result = self.ask(store, fake_embedder, FakeClient(choose("REQUEST_LIMIT = Duration")))
        assert result.status == "answered", f"{result.warnings} {result.rejected}"
        assert [(c.start_line, c.end_line) for c in result.citations] == [(28, 31), (2, 2)]
        assert result.review_candidates == []
        assert all(source.exists_in_git for source in result.sources())
        assert any("declares REQUEST_LIMIT" in w and "the model's reading" in w for w in result.warnings)

    @pytest.mark.parametrize(
        ("on", "roles"),
        # Off, the use is shown to review beside the declaration, and the verdict is still not an answer.
        [(True, [("answer", 2), ("linked_use", 28)]), (False, [("review_candidate", 2), ("review_use", 28)])],
    )
    def test_the_api_never_calls_the_added_use_the_models_choice(self, store, fake_embedder, linked, on, roles):
        from fastapi.testclient import TestClient

        from repomind.api.app import create_app
        from tests.test_api import _services
        from tests.test_qa import FakeClient

        linked((28, 31))
        services = _services(fake_embedder, FakeClient(choose("REQUEST_LIMIT = Duration")))
        services.llm_config = replace(services.llm_config, linked_evidence=on)
        with TestClient(create_app(services)) as client:
            body = client.post("/api/ask", json={"repo": "linked", "question": QUESTION}).json()
        assert [(s["role"], s["start_line"]) for s in body["sources"]] == roles
        assert body["verdict"] == ("lines_chosen" if on else "not_enough_evidence")
        assert any("was added by RepoMind and not chosen by the model" in w for w in body["warnings"]) is on

    def test_the_api_shows_the_value_of_a_constant_beside_the_use_the_model_chose(self, store, fake_embedder, linked):
        from fastapi.testclient import TestClient

        from repomind.api.app import create_app
        from tests.test_api import _services
        from tests.test_qa import FakeClient

        linked((28, 31))
        services = _services(fake_embedder, FakeClient(choose(r"\.timeout\(REQUEST_LIMIT")))
        with TestClient(create_app(services)) as client:
            body = client.post("/api/ask", json={"repo": "linked", "question": QUESTION}).json()
        assert body["verdict"] == "lines_chosen"
        assert [(s["role"], s["start_line"]) for s in body["sources"]] == [("answer", 28), ("declaration", 2)]
        assert not any("which was not chosen" in w for w in body["warnings"])

    def test_a_use_that_fails_the_rule_does_not_carry_its_declaration(self, store, fake_embedder, linked):
        from tests.test_qa import FakeClient

        linked((28, 31), (32, 33))  # the first passes the rule, so the model is asked; the second uses the constant weakly
        result = self.ask(store, fake_embedder, FakeClient(choose("REQUEST_LIMIT = Duration")))
        added = [c for c in result.completions if c.declaration and not c.reason]
        assert [c.used_in for c in added] == ["src/Client.java:28-31"], "completion links the declaration to the first use it met"
        linked((32, 33), (28, 31))  # now the weak use comes first, and the declaration is added for it
        result = self.ask(store, fake_embedder, FakeClient(choose("REQUEST_LIMIT = Duration")))
        assert [c.used_in for c in result.completions if c.declaration and not c.reason] == ["src/Client.java:32-33"]
        assert result.status == "insufficient"
        assert result.citations == []
        assert [(p.start_line, p.end_line) for p in result.review_candidates] == [(2, 2)]

    def test_a_declaration_beside_the_point_of_the_question_is_not_taken_in(self, store, fake_embedder, linked):
        from tests.test_qa import FakeClient

        linked((28, 31))
        result = self.ask(store, fake_embedder, FakeClient(choose("REQUEST_LIMIT = Duration")), "Which timeout does the simulation use for each reserve?")
        assert result.status == "insufficient", "the declaration shares no word with this question"
        assert [(p.start_line, p.end_line) for p in result.review_candidates] == [(2, 2)]

    def test_both_chosen_are_shown_once_and_a_use_chosen_alone_brings_its_value(self, store, fake_embedder, linked):
        from tests.test_qa import FakeClient

        linked((28, 31))
        both = self.ask(store, fake_embedder, FakeClient(choose("REQUEST_LIMIT = Duration", r"\.timeout\(REQUEST_LIMIT")))
        assert [(c.start_line, c.end_line) for c in both.citations] == [(28, 31), (2, 2)]
        alone = self.ask(store, fake_embedder, FakeClient(choose(r"\.timeout\(REQUEST_LIMIT")))
        assert [(c.start_line, c.end_line) for c in alone.citations] == [(28, 31), (2, 2)]
        assert [(p.start_line, p.end_line) for p in alone.declared_values] == [(2, 2)] and alone.linked_uses == []


ROLES = "export const NOTE_ROLE_KEY = 'app:noteRole';\n"
SHARE = (
    "import { NOTE_ROLE_KEY } from './roles';\n"
    "\n"
    "/** The role a shared note was opened with. */\n"
    "export function rememberRole(noteId: string, role: string) {\n"
    "  localStorage.setItem(`${NOTE_ROLE_KEY}:${noteId}`, role);\n"  # line 5: shares "shared" and "note", so it passes the rule
    "}\n"
)
TASKS = "/** A reminder kept for a task. */\nexport function taskReminder(task: string) {\n  return schedule(task);\n}\n"
# Nothing in this repository keeps a reminder for a note. The constant's own name shares one word with the question.
REMINDER_QUESTION = "Is a reminder kept for a shared note?"


@pytest.fixture
def reminder(store, tmp_path, fake_embedder, monkeypatch):
    """The shape of v5-sn-neg-reminder: a question the code does not answer, and a constant named after a word in it."""
    from repomind.indexing import index_repository
    from repomind.qa import engine as engine_module
    from tests.conftest import commit_all, git, write

    root = tmp_path / "notes"
    root.mkdir()
    git(root, "init", "-b", "main")
    write(root, "src/roles.ts", ROLES)
    write(root, "src/share.ts", SHARE)
    write(root, "src/tasks.ts", TASKS)
    commit_all(root, "notes")
    index_repository(store, str(root), fake_embedder, name="notes")
    snapshot = store.active_snapshots(["notes"])[0]
    use = next(c for c in store.file_chunks(snapshot.id, "src/share.ts") if c.start_line <= 5 <= c.end_line)
    # Search stays real. Passage selection offers the function that uses the constant, as it did in the record.
    monkeypatch.setattr(engine_module, "select_passages", lambda *args, **kwargs: [Passage("P1", use, use.start_line, use.end_line, use.content)])
    return use


@pytest.mark.db
class TestAQuestionTheCodeDoesNotAnswer:
    """The model picks the declaration of NOTE_ROLE_KEY for a question about reminders, as it did for v5-sn-neg-reminder."""

    def ask(self, store, fake_embedder, linked_evidence):
        from repomind.qa.engine import QAEngine
        from repomind.search.engine import SearchEngine
        from tests.test_qa import LOCAL, FakeClient

        model = FakeClient(choose("NOTE_ROLE_KEY = "))
        config = replace(LOCAL, linked_evidence=linked_evidence)
        result = QAEngine(store, SearchEngine(store, fake_embedder), config, model).ask(REMINDER_QUESTION, ["notes"])
        assert model.calls, f"the question has to reach the model: {result.status} {result.warnings}"
        assert "NOTE_ROLE_KEY = " in model.calls[-1]["user"], "completion offered the declaration either way"
        return result

    def test_off_the_declaration_is_not_an_answer(self, store, fake_embedder, reminder):
        result = self.ask(store, fake_embedder, linked_evidence=False)
        assert result.status == "insufficient"
        assert result.citations == [] and result.linked_uses == []
        assert [(p.path, p.start_line) for p in result.review_candidates] == [("src/roles.ts", 1)]

    def test_on_the_same_choice_is_shown_as_an_answer_which_is_why_it_is_off(self, store, fake_embedder, reminder):
        result = self.ask(store, fake_embedder, linked_evidence=True)
        assert result.status == "answered", "one shared word, from the constant's own name, is all the link asks of the declaration"
        assert [(c.path, c.start_line) for c in result.citations] == [("src/share.ts", reminder.start_line), ("src/roles.ts", 1)]
        assert [p.path for p in result.linked_uses] == ["src/share.ts"], "and the use is marked as added, not chosen"