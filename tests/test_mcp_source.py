"""get_verified_source, and how much any tool returns.

Lines are read from Git at the indexed commit, only for files in the index, with credential lines hidden
as the web app's file view hides them. The database tests use RepoMind's test database, and the last one
reads the trial database. No model is asked, and nothing is fetched from the network.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from repomind import PIPELINE_VERSION
from repomind.api.app import create_app
from repomind.indexing import index_repository
from repomind.ingest.git_source import GitRepository
from repomind.mcp_server.config import McpConfig, _with_database, load_config
from repomind.mcp_server.search import MAX_RESULTS_CHARACTERS
from repomind.mcp_server.server import build_server
from repomind.mcp_server.source import MAX_LINES, MAX_SOURCE_CHARACTERS
from repomind.store.database import Store
from tests.conftest import commit_all, git, write
from tests.test_api import _services
from tests.test_mcp_search import (
    _NO_INTERNET,
    _WATCHED_SERVER,
    FILES,
    INJECTION,
    SECRET_PARTS,
    _check_the_process,
    _conversation,
    error_of,
    text_of,
)
from tests.test_mcp_server import (  # noqa: F401  (mcp_settings is a fixture)
    BASE_URL,
    PYTHON,
    TRIAL_DATABASE,
    _Server,
    _trial_database_exists,
    call,
    leaks,
    mcp_settings,
    tools,
)

TOOLS = ["get_verified_source", "list_repositories", "search_code"]
TRIAL_COMMIT = "a265b9255a3c39e203156a0120356202d26fa6d1"
ENV_SECRET = "sk-live-5f4e3d2c1b0a99887766"
_LONG = "".join(hashlib.sha256(str(number).encode()).hexdigest() for number in range(70))
# Three lines of one statement, each long enough that the three together would pass the size limit.
VAULT_PARTS = (_LONG[:4400], _LONG[::-1][:4400], _LONG[100:4500])


def _report(number: int) -> str:
    body = "".join(f"  total += entries[{line}].amount * {number}; // overdue ledger report, line {line:02d}\n" for line in range(24))
    return (
        f"/** Overdue ledger report {number}. */\n"
        f"export function overdueLedgerReport{number:02d}(entries: Entry[]): number {{\n"
        f"  let total = 0;\n{body}  return total;\n}}\n"
    )


EXTRA = {
    ".env": f"LIBRARY_API_KEY={ENV_SECRET}\n",
    "src/counter.ts": "let v = 0;\n" + "v += 1;\n" * 249,
    "src/ledger.ts": "".join(f'export const entry{number:03d} = "{"a ledger entry, " * 5}";\n' for number in range(1, 201)),
    "src/vault.ts": (
        f'const clientSecret = "{VAULT_PARTS[0]}" +\n'
        f'  "{VAULT_PARTS[1]}" +\n'
        f'  "{VAULT_PARTS[2]}";\n'
        "\n"
        "export function sign(body: string): string {\n"
        "  return body + clientSecret.length;\n"
        "}\n"
        # Enough ordinary lines that indexing does not take the file for minified code.
        + "".join(f"export const shelf{number:02d} = {number};\n" for number in range(1, 41))
    ),
    **{f"src/reports/report{number:02d}.ts": _report(number) for number in range(1, 13)},
}


@pytest.fixture
def library(store, tmp_path, fake_embedder, sample_repo):
    root = tmp_path / "library"
    root.mkdir()
    git(root, "init", "-b", "main")
    for path, content in {**FILES, **EXTRA}.items():
        write(root, path, content)
    commit_all(root, "A library with a ledger")
    index_repository(store, str(root), fake_embedder, name="library")
    # Indexed, but not listed: the server must never show it.
    index_repository(store, str(sample_repo), fake_embedder, name="other")
    return root


@pytest.fixture
def server(library, fake_embedder, mcp_settings):
    mcp_settings(repositories="library")
    return build_server(load_config(), embedder_factory=lambda: fake_embedder)


def read(server, **arguments):
    return call(server, "get_verified_source", {"repo": "library", **arguments}, raise_exceptions=False)


def search(server, **arguments):
    return call(server, "search_code", {"repo": "library", **arguments}, raise_exceptions=False)


def lines_of(body) -> list[str]:
    return [line["text"] for line in body["lines"]]


def committed(root, path: str, commit: str = "HEAD") -> list[str]:
    """The file's lines as Git holds them at ``commit``."""
    return git(root, "show", f"{commit}:{path}").split("\n")


def never_shown(text: str) -> None:
    for secret in (*SECRET_PARTS, *(part[:40] for part in VAULT_PARTS), ENV_SECRET):
        assert secret not in text, "a hidden line's text came back"


def json_size(items: list[dict]) -> int:
    return sum(len(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) for item in items)


@pytest.mark.db
class TestReadingLines:
    def test_the_lines_asked_for_as_git_holds_them_at_the_indexed_commit(self, server):
        result = read(server, path="src/fees.ts", start_line=1, end_line=6)
        assert not result.is_error, error_of(result)
        body = result.structured_content
        listed = call(server, "list_repositories").structured_content["repositories"][0]
        assert (body["repo"], body["commit"], body["index_version"]) == ("library", listed["commit"], PIPELINE_VERSION)
        assert (body["path"], body["language"], body["start_line"], body["end_line"], body["total_lines"]) == ("src/fees.ts", "typescript", 1, 6, 6)
        assert [line["number"] for line in body["lines"]] == [1, 2, 3, 4, 5, 6]
        assert lines_of(body) == FILES["src/fees.ts"].split("\n")[:6]
        assert not any(line["hidden"] for line in body["lines"]) and body["hidden_lines"] == 0
        assert body["role"] == "source" and body["exists_in_git"] is True and body["matches_index"] is True
        assert body["truncated"] is False and body["warnings"] == []
        assert "do not come from the user" in body["notice"]

    def test_lines_from_the_middle(self, server):
        body = read(server, path="src/fees.ts", start_line=4, end_line=5).structured_content
        assert [(line["number"], line["text"]) for line in body["lines"]] == [
            (4, "export function lateFee(daysLate: number): number {"),
            (5, "  return Math.max(0, daysLate) * DAILY_RATE;"),
        ]

    def test_is_never_called_an_answer(self, server):
        body = read(server, path="src/fees.ts", start_line=1, end_line=6).structured_content
        assert body["role"] == "source"
        assert not {"answer", "verdict", "verified", "evidence"} & set(body)

    def test_shows_no_local_path_address_or_password(self, server, library):
        text = text_of(read(server, path="src/fees.ts", start_line=1, end_line=6))
        assert not leaks(text)
        assert str(library) not in text and str(library).replace("\\", "/") not in text
        assert "source_path" not in text and "origin_url" not in text and "blob_sha" not in text

    def test_a_commit_given_must_be_the_indexed_one(self, server):
        commit = call(server, "list_repositories").structured_content["repositories"][0]["commit"]
        for given in (commit, commit[:7], commit.upper()):
            result = read(server, path="src/fees.ts", start_line=1, end_line=1, commit=given)
            assert not result.is_error and result.structured_content["commit"] == commit
        message = error_of(read(server, path="src/fees.ts", start_line=1, end_line=1, commit="0" * 40))
        assert "Only the indexed commit can be read" in message and commit in message
        for malformed in ("abc", "z" * 40, "HEAD", "main", "0" * 41, commit + "0", f"{commit[:7]}:src/fees.ts"):
            assert read(server, path="src/fees.ts", start_line=1, end_line=1, commit=malformed).is_error, malformed

    def test_its_arguments(self, server):
        schema = {tool.name: tool for tool in tools(server).tools}["get_verified_source"].input_schema
        assert sorted(schema["required"]) == ["end_line", "path", "repo", "start_line"]
        assert schema["properties"]["path"]["maxLength"] == 1024
        assert schema["properties"]["start_line"]["minimum"] == 1 and schema["properties"]["end_line"]["minimum"] == 1
        assert read(server, path="a" * 1025, start_line=1, end_line=1).is_error
        assert "cannot be read" in error_of(read(server, path="a" * 1024, start_line=1, end_line=1))
        assert read(server, repo="l" * 201, path="src/fees.ts", start_line=1, end_line=1).is_error


@pytest.mark.db
class TestWhatMayBeRead:
    def test_only_a_listed_repository(self, server):
        unlisted = error_of(read(server, repo="other", path="src/payment.ts", start_line=1, end_line=3))
        unknown = error_of(read(server, repo="nothing-like-it", path="src/payment.ts", start_line=1, end_line=3))
        assert "not one this server may show" in unlisted
        assert unlisted == unknown, "whether an unlisted repository exists is not said"

    def test_listed_but_not_in_this_database(self, library, fake_embedder, mcp_settings):
        mcp_settings(repositories="library,missing")
        server = build_server(load_config(), embedder_factory=lambda: fake_embedder)
        assert "not indexed in the database" in error_of(read(server, repo="missing", path="src/fees.ts", start_line=1, end_line=1))

    def test_not_an_index_this_version_made(self, server, store):
        store.connection.execute("UPDATE snapshots SET pipeline_version = '2'")
        message = error_of(read(server, path="src/fees.ts", start_line=1, end_line=6))
        assert "older version" in message and "indexed again" in message

    @pytest.mark.parametrize(
        "path",
        [".env", ".git/config", ".git/HEAD", "SRC/fees.ts", "src/fees", "src", "src/reports", "src/fees.ts ", "HEAD:src/fees.ts",
         "%2e%2e%2f.env", "~/.ssh/id_rsa", "src/payment.ts"],
    )
    def test_a_file_the_index_does_not_hold_is_never_read(self, server, path, monkeypatch):
        monkeypatch.setattr(GitRepository, "read_file", lambda *args: pytest.fail("Git was asked for a file the index does not hold"))
        message = error_of(read(server, path=path, start_line=1, end_line=1))
        assert "cannot be read. Only files in the repository's index can be" in message
        never_shown(message)

    @pytest.mark.parametrize(
        "path",
        ["../../../etc/passwd", "..\\..\\windows\\win.ini", "src/../.env", "./src/fees.ts", "src/./fees.ts", "/etc/passwd",
         "/src/fees.ts", "C:\\Windows\\win.ini", "C:/Windows/win.ini", "c:src/fees.ts", "\\\\server\\share\\fees.ts",
         "src//fees.ts", "src/fees.ts/", "src/fees.ts\x00.png", "src/fees.ts\n", "..", "."],
    )
    def test_a_path_that_reaches_outside_is_refused_before_anything_is_looked_up(self, server, path, monkeypatch):
        monkeypatch.setattr(Store, "indexed_file", lambda *args: pytest.fail("the index was asked about this path"))
        monkeypatch.setattr(GitRepository, "read_file", lambda *args: pytest.fail("Git was asked for this path"))
        message = error_of(read(server, path=path, start_line=1, end_line=1))
        assert any(reason in message for reason in ("relative to the repository", "inside the repository", "control character")), message
        never_shown(message)


@pytest.mark.db
class TestLineRanges:
    def test_the_end_cannot_come_before_the_start(self, server):
        assert "comes before" in error_of(read(server, path="src/fees.ts", start_line=4, end_line=3))

    @pytest.mark.parametrize(("start", "end"), [(0, 1), (-1, 3), (1, 0), (1, -5)])
    def test_lines_count_from_one(self, server, start, end):
        assert read(server, path="src/fees.ts", start_line=start, end_line=end).is_error

    def test_at_most_200_lines_at_a_time(self, server):
        for start, end in ((1, MAX_LINES), (51, 50 + MAX_LINES)):
            body = read(server, path="src/counter.ts", start_line=start, end_line=end).structured_content
            assert (len(body["lines"]), body["end_line"], body["truncated"]) == (MAX_LINES, end, False)
        message = error_of(read(server, path="src/counter.ts", start_line=1, end_line=MAX_LINES + 1))
        assert "At most 200 lines" in message and "1 to 200" in message

    def test_no_line_past_the_end(self, server):
        assert "has 6 lines, so there is no line 7" in error_of(read(server, path="src/fees.ts", start_line=7, end_line=9))
        assert "has 6 lines" in error_of(read(server, path="src/fees.ts", start_line=10**12, end_line=10**12 + 5))

    def test_an_end_past_the_end_stops_at_the_last_line_and_says_so(self, server):
        body = read(server, path="src/fees.ts", start_line=5, end_line=50).structured_content
        assert (body["end_line"], body["total_lines"], body["truncated"]) == (6, 6, False)
        assert lines_of(body) == ["  return Math.max(0, daysLate) * DAILY_RATE;", "}"]
        assert body["warnings"] == ["The file has 6 lines, so the lines end at line 6."]


@pytest.mark.db
class TestFromGitNotFromTheWorkingFolder:
    def test_a_change_not_committed_is_never_shown(self, server, library):
        write(library, "src/fees.ts", "export const DAILY_RATE = 99; // edited, not committed\n")
        write(library, "src/draft.ts", "export const draft = 1;\n")
        result = read(server, path="src/fees.ts", start_line=1, end_line=6)
        assert lines_of(result.structured_content) == FILES["src/fees.ts"].split("\n")[:6]
        assert "edited, not committed" not in text_of(result)
        assert "cannot be read" in error_of(read(server, path="src/draft.ts", start_line=1, end_line=1))

    def test_a_commit_made_after_indexing_is_not_read(self, server, library):
        indexed = git(library, "rev-parse", "HEAD")
        write(library, "src/fees.ts", "export const DAILY_RATE = 99; // committed after indexing\n")
        write(library, "src/later.ts", "export const later = 1;\n")
        newer = commit_all(library, "After indexing")
        body = read(server, path="src/fees.ts", start_line=1, end_line=6).structured_content
        assert body["commit"] == indexed and lines_of(body) == FILES["src/fees.ts"].split("\n")[:6]
        assert "cannot be read" in error_of(read(server, path="src/later.ts", start_line=1, end_line=1))
        assert "Only the indexed commit" in error_of(read(server, path="src/fees.ts", start_line=1, end_line=1, commit=newer))


@pytest.mark.db
class TestGit:
    def test_without_the_local_copy_nothing_is_read(self, server, library):
        (library / ".git").rename(library / "git-moved-away")
        message = error_of(read(server, path="src/fees.ts", start_line=1, end_line=6))
        assert "cannot be read" in message and "checked against Git" in message
        assert str(library) not in message and not leaks(message)

    def test_a_commit_the_local_copy_does_not_hold(self, server, store):
        store.connection.execute("UPDATE snapshots SET commit_sha = repeat('f', 40)")
        message = error_of(read(server, path="src/fees.ts", start_line=1, end_line=6))
        assert "checked against Git" in message and not leaks(message)

    def test_the_index_records_the_object_git_gives(self, library, store):
        recorded = store.connection.execute("SELECT blob_sha FROM files WHERE path = 'src/fees.ts'").fetchone()["blob_sha"]
        assert recorded == git(library, "rev-parse", "HEAD:src/fees.ts")

    def test_a_file_that_is_not_the_one_indexed_is_not_returned(self, server, store):
        other = store.connection.execute("SELECT blob_sha FROM files WHERE path = 'src/notes.ts'").fetchone()["blob_sha"]
        store.connection.execute("UPDATE files SET blob_sha = %s WHERE path = 'src/fees.ts'", (other,))
        message = error_of(read(server, path="src/fees.ts", start_line=1, end_line=6))
        assert "not the one RepoMind indexed" in message and "DAILY_RATE" not in message

    def test_when_the_index_holds_no_object_id_it_says_so(self, server, store):
        store.connection.execute("UPDATE files SET blob_sha = '' WHERE path = 'src/fees.ts'")
        body = read(server, path="src/fees.ts", start_line=1, end_line=6).structured_content
        assert body["matches_index"] is None, "not checked is never shown as checked"
        assert any("could not be checked" in warning for warning in body["warnings"])
        assert body["exists_in_git"] is True and lines_of(body) == FILES["src/fees.ts"].split("\n")[:6]


@pytest.mark.db
class TestCredentials:
    def test_a_secret_over_two_lines_is_hidden_whole(self, server):
        result = read(server, path="src/signing.ts", start_line=1, end_line=6)
        body = result.structured_content
        assert [(line["number"], line["hidden"]) for line in body["lines"][:3]] == [(1, True), (2, True), (3, False)]
        assert body["lines"][0]["text"] == body["lines"][1]["text"] == ""
        assert body["lines"][3]["text"] == "export function signRequest(body: string): string {"
        assert body["hidden_lines"] == 2 and any("2 line(s) are hidden" in warning for warning in body["warnings"])
        never_shown(text_of(result))

    @pytest.mark.parametrize(("start", "end"), [(1, 1), (2, 2), (2, 3), (1, 2), (2, 6)])
    def test_a_hidden_line_never_comes_back_whatever_is_asked(self, server, start, end):
        commit = call(server, "list_repositories").structured_content["repositories"][0]["commit"]
        for given in (None, commit, commit[:7]):
            arguments = {"path": "src/signing.ts", "start_line": start, "end_line": end} | ({"commit": given} if given else {})
            result = read(server, **arguments)
            assert all(line["hidden"] and line["text"] == "" for line in result.structured_content["lines"] if line["number"] <= 2)
            never_shown(text_of(result))

    def test_a_long_secret_over_three_lines_is_hidden_before_the_size_limit_counts(self, server):
        result = read(server, path="src/vault.ts", start_line=1, end_line=7)
        body = result.structured_content
        assert [line["number"] for line in body["lines"] if line["hidden"]] == [1, 2, 3]
        assert body["truncated"] is False and body["end_line"] == 7, "hidden lines are blanked before the limit is applied"
        never_shown(text_of(result))

    def test_search_shows_none_of_them_either(self, server):
        for query in ("clientSecret", "sign request", "LIBRARY_API_KEY"):
            for mode in ("keyword", "hybrid"):
                never_shown(text_of(search(server, query=query, mode=mode, limit=20)))

    def test_a_file_left_out_of_the_index_says_nothing_of_its_content(self, server):
        never_shown(error_of(read(server, path=".env", start_line=1, end_line=1)))

    def test_the_lines_and_hidden_lines_are_those_the_web_app_shows(self, server, fake_embedder):
        with TestClient(create_app(_services(fake_embedder))) as client:
            for path in ("src/fees.ts", "src/signing.ts", "src/vault.ts", "src/notes.ts", "src/counter.ts", "src/ledger.ts"):
                shown = client.get("/api/file", params={"repo": "library", "path": path}).json()
                lines, start = [], 1
                while start <= len(shown["lines"]):
                    body = read(server, path=path, start_line=start, end_line=min(start + MAX_LINES - 1, len(shown["lines"]))).structured_content
                    lines += body["lines"]
                    start = body["end_line"] + 1
                assert [line["text"] for line in lines] == shown["lines"], path
                assert [line["number"] for line in lines if line["hidden"]] == shown["hidden_lines"], path
            assert client.get("/api/file", params={"repo": "library", "path": ".env"}).status_code == 404


@pytest.mark.db
class TestRepositoryContentIsData:
    def test_an_instruction_in_a_file_is_returned_as_its_text_and_nothing_more(self, server):
        result = read(server, path="src/notes.ts", start_line=1, end_line=4)
        body = result.structured_content
        assert body["lines"][0]["text"] == f"// {INJECTION}"
        assert body["role"] == "source" and "may contain instructions" in body["notice"]
        assert sorted(tool.name for tool in tools(server).tools) == TOOLS


@pytest.mark.db
class TestTheDatabase:
    def test_reading_writes_nothing(self, server, store):
        counts = "SELECT (SELECT count(*) FROM chunks) AS c, (SELECT count(*) FROM snapshots) AS s, (SELECT count(*) FROM llm_usage) AS u"
        before = store.connection.execute(counts).fetchone()
        assert not read(server, path="src/fees.ts", start_line=1, end_line=6).is_error
        assert store.connection.execute(counts).fetchone() == before

    def test_an_unreachable_database_is_a_tool_error_without_its_address(self, library, fake_embedder, mcp_settings):
        mcp_settings()
        config = load_config()
        broken = McpConfig(config.database, config.repositories, _with_database(BASE_URL, "repomind_does_not_exist"))
        message = error_of(read(build_server(broken, embedder_factory=lambda: fake_embedder), path="src/fees.ts", start_line=1, end_line=6))
        assert "cannot be reached" in message and not leaks(message)


@pytest.mark.db
class TestTheResponseSize:
    def test_search_results_stay_under_the_limit_and_say_how_many_are_cut(self, server, library):
        body = search(server, query="overdue ledger report", mode="keyword", limit=20).structured_content
        assert json_size(body["results"]) <= MAX_RESULTS_CHARACTERS
        assert body["truncated"] is True and body["cut_for_size"] >= 1 and body["left_out"] == 0
        assert any("only to keep all results under 12,000 characters" in warning for warning in body["warnings"])
        assert not any("did not match Git" in warning for warning in body["warnings"]), "a cut for size is not called a refusal"
        assert [item["rank"] for item in body["results"]] == list(range(1, len(body["results"]) + 1))
        for item in body["results"]:
            assert item["exists_in_git"] is True
            assert item["preview"].split("\n") == committed(library, item["path"])[item["start_line"] - 1 : item["preview_end_line"]]

    def test_what_is_cut_is_the_end_of_the_list_in_search_order(self, server):
        cut = search(server, query="overdue ledger report", mode="keyword", limit=20).structured_content
        kept = len(cut["results"])
        whole = search(server, query="overdue ledger report", mode="keyword", limit=kept).structured_content
        assert whole["truncated"] is False and whole["cut_for_size"] == 0
        assert [(item["path"], item["start_line"]) for item in whole["results"]] == [(item["path"], item["start_line"]) for item in cut["results"]]

    def test_a_cut_for_size_and_a_result_left_out_for_safety_are_counted_apart(self, server, store):
        store.connection.execute("UPDATE chunks SET content = content || E'\\n// added outside Git' WHERE path = 'src/reports/report01.ts'")
        body = search(server, query="overdue ledger report", mode="keyword", limit=20).structured_content
        assert body["left_out"] >= 1 and body["cut_for_size"] >= 1
        assert sum("did not match Git" in warning for warning in body["warnings"]) == 1
        assert sum("only to keep all results under" in warning for warning in body["warnings"]) == 1

    def test_a_small_search_is_not_cut(self, server):
        body = search(server, query="late fee", mode="keyword", limit=3).structured_content
        assert (body["truncated"], body["cut_for_size"]) == (False, 0)
        assert not any("only to keep" in warning for warning in body["warnings"])

    def test_source_lines_stay_under_the_limit_and_say_where_to_go_on(self, server, library):
        body = read(server, path="src/ledger.ts", start_line=1, end_line=MAX_LINES).structured_content
        assert body["truncated"] is True and 1 < body["end_line"] < MAX_LINES
        assert json_size(body["lines"]) <= MAX_SOURCE_CHARACTERS
        assert [line["number"] for line in body["lines"]] == list(range(1, body["end_line"] + 1))
        assert any(f"Ask for them from line {body['end_line'] + 1}." in warning for warning in body["warnings"])
        assert body["matches_index"] is True and body["exists_in_git"] is True
        lines = body["lines"]
        while lines[-1]["number"] < MAX_LINES:
            lines += read(server, path="src/ledger.ts", start_line=lines[-1]["number"] + 1, end_line=MAX_LINES).structured_content["lines"]
        assert [line["text"] for line in lines] == committed(library, "src/ledger.ts")[:MAX_LINES], "page by page, every line comes back"

    def test_the_text_copy_is_the_same_json_without_indentation(self, server):
        for result in (
            read(server, path="src/ledger.ts", start_line=1, end_line=MAX_LINES),
            search(server, query="overdue ledger report", mode="keyword", limit=20),
        ):
            [content] = result.content
            assert json.loads(content.text) == result.structured_content
            assert "\n " not in content.text and len(content.text) < 14_000


@pytest.mark.db
class TestTheServerProcess:
    def test_reads_lines_over_stdio_without_the_network(self, library):
        server = _Server([PYTHON, "-c", _WATCHED_SERVER], REPOMIND_MCP_DATABASE="repomind_test", REPOMIND_MCP_REPOSITORIES="library", **_NO_INTERNET)
        answers = _conversation(
            server,
            [
                ("search_code", {"repo": "library", "query": "late fee", "mode": "keyword"}),
                ("get_verified_source", {"repo": "library", "path": "src/fees.ts", "start_line": 1, "end_line": 6}),
                ("get_verified_source", {"repo": "library", "path": "src/signing.ts", "start_line": 1, "end_line": 6}),
                ("get_verified_source", {"repo": "library", "path": "src/vault.ts", "start_line": 1, "end_line": 7}),
                ("get_verified_source", {"repo": "library", "path": ".env", "start_line": 1, "end_line": 1}),
                ("get_verified_source", {"repo": "library", "path": "../../.env", "start_line": 1, "end_line": 1}),
                ("get_verified_source", {"repo": "other", "path": "src/payment.ts", "start_line": 1, "end_line": 3}),
                ("get_verified_source", {"repo": "library", "path": "src/counter.ts", "start_line": 1, "end_line": 201}),
            ],
        )
        stderr = _check_the_process(server)
        found, lines, signing, vault, *refused = answers
        assert found["structuredContent"]["results"][0]["path"] == "src/fees.ts"
        assert lines_of(lines["structuredContent"]) == FILES["src/fees.ts"].split("\n")[:6]
        assert signing["structuredContent"]["hidden_lines"] == 2 and vault["structuredContent"]["hidden_lines"] == 3
        assert all(answer["isError"] for answer in refused)
        everything = json.dumps(answers) + stderr
        never_shown(everything)
        assert str(library) not in everything and str(library).replace("\\", "/") not in everything


@pytest.mark.db
@pytest.mark.skipif(not _trial_database_exists(), reason="the trial database repomind_openai_trial is not here")
class TestTheTrialDatabase:
    def test_search_finds_src_fees_ts_and_its_lines_are_read_from_the_indexed_commit(self):
        server = _Server([PYTHON, "-c", _WATCHED_SERVER], REPOMIND_MCP_DATABASE=TRIAL_DATABASE, REPOMIND_MCP_REPOSITORIES="library", **_NO_INTERNET)
        [found] = _conversation(server, [("search_code", {"repo": "library", "query": "late fee", "mode": "hybrid", "limit": 3})])
        first = found["structuredContent"]["results"][0]
        arguments = {"repo": "library", "path": first["path"], "start_line": first["start_line"], "end_line": first["end_line"], "commit": TRIAL_COMMIT}
        server.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_verified_source", "arguments": arguments}})
        answer = server.receive()
        _check_the_process(server)
        assert found["structuredContent"]["commit"] == TRIAL_COMMIT and first["path"] == "src/fees.ts"
        body = answer["result"]["structuredContent"]
        assert not answer["result"].get("isError") and body["commit"] == TRIAL_COMMIT and body["index_version"] == PIPELINE_VERSION
        assert body["role"] == "source" and body["exists_in_git"] is True and body["matches_index"] is True
        assert "\n".join(lines_of(body)) == first["preview"]
