"""The local MCP server: what it refuses to start with, what it reads, what it shows, and what it never writes.

The database tests use RepoMind's test database, and one reads the trial database, both read-only for the
server. No model is asked, and the model settings are never read.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import psycopg
import pytest
from mcp import Client

from repomind import PIPELINE_VERSION
from repomind.config import Settings
from repomind.indexing import index_repository
from repomind.mcp_server.config import ConfigurationError, McpConfig, _with_database, load_config
from repomind.mcp_server.database import DatabaseUnavailable, check_database, read_only_store
from repomind.mcp_server.repositories import list_allowed
from repomind.mcp_server.server import build_server
from tests.conftest import TEST_DATABASE, commit_all, git, write

PROJECT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
TRIAL_DATABASE = "repomind_openai_trial"
# What must never appear in anything the server returns or logs.
BASE_URL = Settings.load().database_url
_PARTS = urlsplit(BASE_URL)
# The local container's password can be an ordinary word, so it is looked for as a credential: after the
# user name and before the host.
CREDENTIAL = f"{_PARTS.username}:{_PARTS.password}@" if _PARTS.password else ""
ADDRESS = f"{_PARTS.hostname}:{_PARTS.port}"


def leaks(text: str) -> list[str]:
    """Names what leaked, never the secret itself, so that a failing test does not print it."""
    found = []
    if CREDENTIAL and CREDENTIAL in text:
        found.append("the database user and password")
    if _PARTS.password and len(_PARTS.password) >= 12 and _PARTS.password in text:
        found.append("the database password")
    if "postgresql://" in text or "postgres://" in text:
        found.append("a database address")
    if ADDRESS in text:
        found.append("the database host and port")
    return found


@pytest.fixture
def mcp_settings(monkeypatch):
    def apply(database: str | None = TEST_DATABASE, repositories: str | None = "library") -> None:
        for name, value in (("REPOMIND_MCP_DATABASE", database), ("REPOMIND_MCP_REPOSITORIES", repositories)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
                monkeypatch.setenv(name, "")
            else:
                monkeypatch.setenv(name, value)

    return apply


def call(server, tool: str, arguments: dict | None = None, raise_exceptions: bool = True):
    async def run():
        async with Client(server, raise_exceptions=raise_exceptions) as client:
            return await client.call_tool(tool, arguments or {})

    return anyio.run(run)


def tools(server):
    async def run():
        async with Client(server, raise_exceptions=True) as client:
            return await client.list_tools()

    return anyio.run(run)


class TestTheServerRefusesToStartWithout:
    def test_a_database(self, mcp_settings):
        mcp_settings(database=None)
        with pytest.raises(ConfigurationError, match="REPOMIND_MCP_DATABASE is not set"):
            load_config()

    def test_a_list_of_repositories(self, mcp_settings):
        mcp_settings(repositories=None)
        with pytest.raises(ConfigurationError, match="REPOMIND_MCP_REPOSITORIES is not set"):
            load_config()
        mcp_settings(repositories=" , ,")
        with pytest.raises(ConfigurationError, match="is not set"):
            load_config()

    @pytest.mark.parametrize("name", ["Repomind", "repomind-test", "repomind;drop", "a b", "x" * 64, "../repomind", "1repomind", "repomind_test?sslmode=disable"])
    def test_a_database_name_it_can_use_as_it_is(self, mcp_settings, name):
        mcp_settings(database=name)
        with pytest.raises(ConfigurationError, match="must be a database name"):
            load_config()

    @pytest.mark.parametrize("names", ["../etc", "library,a b", "C:\\repos\\library", "library,/abs", "a//b", "made-up key with spaces"])
    def test_repository_names_that_are_names(self, mcp_settings, names):
        mcp_settings(repositories=names)
        with pytest.raises(ConfigurationError, match="not a repository name") as caught:
            load_config()
        assert names not in str(caught.value), "a wrong value is not repeated"

    def test_too_many_repositories(self, mcp_settings):
        mcp_settings(repositories=",".join(f"repo{n}" for n in range(21)))
        with pytest.raises(ConfigurationError, match="more than 20"):
            load_config()

    def test_a_usable_database_address(self, mcp_settings, monkeypatch):
        mcp_settings()
        monkeypatch.setenv("REPOMIND_DATABASE_URL", "not an address at all")
        with pytest.raises(ConfigurationError) as caught:
            load_config()
        assert "not an address at all" not in str(caught.value)


class TestTheConfiguration:
    def test_names_are_read_and_only_the_database_name_changes(self, mcp_settings):
        mcp_settings(database="repomind_openai_trial", repositories=" Library , other,library ")
        config = load_config()
        assert config.database == "repomind_openai_trial"
        assert config.repositories == ("library", "other")
        target, base = urlsplit(config.database_url), urlsplit(BASE_URL)
        assert (target.path, target.hostname, target.port, target.username) == ("/repomind_openai_trial", base.hostname, base.port, base.username)

    def test_the_address_is_never_in_its_repr(self, mcp_settings):
        mcp_settings()
        config = load_config()
        assert not leaks(repr(config)) and not leaks(str(config))


@pytest.mark.db
class TestTheDatabaseSession:
    def test_it_refuses_every_write(self, store, mcp_settings):
        mcp_settings()
        before = store.connection.execute("SELECT count(*) AS n FROM llm_usage").fetchone()["n"]
        with read_only_store(load_config()) as readonly:
            for statement in (
                "INSERT INTO llm_usage (base_url, model, input_tokens, output_tokens, cost_usd) VALUES ('u', 'm', 1, 1, 0)",
                "UPDATE snapshots SET pipeline_version = '9'",
                "DELETE FROM chunks",
                "CREATE TABLE made_up (id int)",
                "TRUNCATE llm_usage",
            ):
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                    readonly.connection.execute(statement)
        assert store.connection.execute("SELECT count(*) AS n FROM llm_usage").fetchone()["n"] == before
        assert store.connection.execute("SELECT to_regclass('made_up') AS t").fetchone()["t"] is None

    def test_it_checks_that_it_is_on_the_named_database(self, store):
        # Named one database, connected to another: the session says which it is on, and that is checked.
        elsewhere = McpConfig("repomind_somewhere_else", ("library",), _with_database(BASE_URL, TEST_DATABASE))
        with pytest.raises(DatabaseUnavailable, match="not on the database"):
            check_database(elsewhere)

    def test_a_database_that_does_not_exist_is_said_without_its_address(self, store, mcp_settings):
        mcp_settings(database="repomind_does_not_exist")
        with pytest.raises(DatabaseUnavailable) as caught:
            check_database(load_config())
        assert "cannot be reached" in str(caught.value)
        assert not leaks(str(caught.value)) and caught.value.__cause__ is None


@pytest.fixture
def two_repositories(store, sample_repo, fake_embedder):
    index_repository(store, str(sample_repo), fake_embedder, name="library")
    index_repository(store, str(sample_repo), fake_embedder, name="other")
    return sample_repo


@pytest.mark.db
class TestListRepositories:
    def test_its_tools_are_these_and_all_read_only(self, store, mcp_settings):
        mcp_settings()
        listed = {tool.name: tool for tool in tools(build_server(load_config())).tools}
        assert sorted(listed) == ["list_repositories", "search_code"]
        for tool in listed.values():
            assert tool.annotations.read_only_hint is True and tool.annotations.open_world_hint is False
        assert listed["list_repositories"].output_schema and "repositories" in listed["list_repositories"].output_schema["properties"]

    def test_only_the_listed_repositories_are_shown(self, two_repositories, mcp_settings):
        mcp_settings(repositories="library")
        result = call(build_server(load_config()), "list_repositories")
        body = result.structured_content
        assert [entry["name"] for entry in body["repositories"]] == ["library"]
        entry = body["repositories"][0]
        assert len(entry["commit"]) == 40 and entry["index_version"] == PIPELINE_VERSION and entry["available"] is True
        assert entry["reason"] is None and entry["files_indexed"] > 0 and entry["chunks"] > 0
        assert body["current_index_version"] == PIPELINE_VERSION and body["not_found"] == []
        assert "do not come from the user" in body["notice"]
        assert "other" not in json.dumps(body)

    def test_a_listed_name_this_database_does_not_hold_is_said(self, two_repositories, mcp_settings):
        mcp_settings(repositories="library,missing")
        body = call(build_server(load_config()), "list_repositories").structured_content
        assert body["not_found"] == ["missing"]

    def test_an_index_made_by_an_older_version_is_not_available(self, two_repositories, store, mcp_settings):
        store.connection.execute("UPDATE snapshots SET pipeline_version = '2' FROM repositories r WHERE r.id = snapshots.repository_id AND r.name = 'library'")
        mcp_settings(repositories="library,other")
        body = call(build_server(load_config()), "list_repositories").structured_content
        by_name = {entry["name"]: entry for entry in body["repositories"]}
        assert by_name["library"]["available"] is False and by_name["library"]["index_version"] == "2"
        assert "older version" in by_name["library"]["reason"] and "indexed again" in by_name["library"]["reason"]
        assert by_name["other"]["available"] is True

    def test_no_local_path_address_or_password_is_returned(self, two_repositories, mcp_settings):
        mcp_settings(repositories="library,other")
        result = call(build_server(load_config()), "list_repositories")
        text = json.dumps(result.structured_content) + " ".join(getattr(item, "text", "") for item in result.content)
        assert not leaks(text)
        assert str(two_repositories) not in text and str(two_repositories).replace("\\", "/") not in text
        assert "source_path" not in text and "origin_url" not in text

    def test_a_commit_subject_that_may_hold_a_credential_is_withheld(self, store, tmp_path, fake_embedder, mcp_settings):
        root = tmp_path / "subjects"
        root.mkdir()
        git(root, "init", "-b", "main")
        write(root, "src/a.ts", "export const a = 1;\n")
        commit_all(root, "Rotate AKIAIOSFODNN7EXAMPLE in the deploy notes")
        index_repository(store, str(root), fake_embedder, name="library")
        mcp_settings()
        entry = call(build_server(load_config()), "list_repositories").structured_content["repositories"][0]
        assert entry["subject"] is None and entry["subject_withheld"] is True
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(entry)

    def test_a_commit_subject_loses_control_characters_and_local_folders(self, store, tmp_path, fake_embedder, mcp_settings):
        root = tmp_path / "subjects"
        root.mkdir()
        git(root, "init", "-b", "main")
        write(root, "src/a.ts", "export const a = 1;\n")
        commit_all(root, "Move \x1b[31mthe build\x1b[0m out of C:\\Users\\someone\\work")
        index_repository(store, str(root), fake_embedder, name="library")
        mcp_settings()
        subject = call(build_server(load_config()), "list_repositories").structured_content["repositories"][0]["subject"]
        assert "\x1b" not in subject and "someone" not in subject and "<local path>" in subject

    def test_an_unreachable_database_is_a_tool_error_without_its_address(self, store, mcp_settings):
        mcp_settings()
        config = load_config()
        broken = McpConfig(config.database, config.repositories, _with_database(BASE_URL, "repomind_does_not_exist"))
        result = call(build_server(broken), "list_repositories", raise_exceptions=False)
        text = " ".join(getattr(item, "text", "") for item in result.content)
        assert result.is_error and "cannot be reached" in text
        assert not leaks(text)

    def test_the_model_settings_are_never_read(self, two_repositories, mcp_settings, monkeypatch):
        import repomind.qa.llm as llm

        monkeypatch.setattr(llm, "load_llm_config", lambda: pytest.fail("the MCP server read the model settings"))
        mcp_settings()
        assert list_allowed(load_config()).repositories


class TestWhatTheServerLoads:
    def test_neither_the_question_engine_the_web_api_nor_a_model_client(self):
        # Search is loaded, for search_code. The question engine, a model client or a translator never is.
        script = (
            "import sys\n"
            "import repomind.mcp_server.__main__, repomind.mcp_server.server, repomind.mcp_server.repositories\n"
            "print(sorted(m for m in sys.modules if m.startswith(('repomind.qa', 'repomind.api', 'openai', 'ollama', 'ctranslate2'))))\n"
        )
        loaded = subprocess.run([PYTHON, "-c", script], capture_output=True, text=True, cwd=PROJECT, timeout=60)
        assert loaded.returncode == 0, loaded.stderr
        assert loaded.stdout.strip() == "[]"


def _environment(**settings: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("REPOMIND_LLM_", "REPOMIND_MCP_", "OPENAI_"))}
    return {**env, **settings, "PYTHONIOENCODING": "utf-8"}


class _Server:
    """The real server in its own process, spoken to in raw JSON-RPC. Every stdout line is kept."""

    def __init__(self, command: list[str] | None = None, **settings: str) -> None:
        self.process = subprocess.Popen(
            command or [PYTHON, "-m", "repomind.mcp_server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=PROJECT, env=_environment(**settings),
        )
        self.lines: queue.Queue[bytes] = queue.Queue()
        self.stdout: list[bytes] = []
        self.stderr: list[bytes] = []
        threading.Thread(target=self._read, args=(self.process.stdout, self.stdout, True), daemon=True).start()
        threading.Thread(target=self._read, args=(self.process.stderr, self.stderr, False), daemon=True).start()

    def _read(self, stream, kept, queued) -> None:
        for line in stream:
            kept.append(line)
            if queued:
                self.lines.put(line)

    def send(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def receive(self) -> dict:
        return json.loads(self.lines.get(timeout=30))

    def close(self) -> int:
        self.process.stdin.close()
        try:
            return self.process.wait(timeout=20)
        finally:
            if self.process.poll() is None:
                self.process.kill()


def _converse(server: _Server) -> dict:
    server.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
    initialized = server.receive()
    server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    server.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    listed = server.receive()
    server.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_repositories", "arguments": {}}})
    called = server.receive()
    return {"initialized": initialized, "listed": listed, "called": called}


@pytest.mark.db
class TestStdio:
    def test_stdout_carries_only_mcp_messages_and_the_server_stops_cleanly(self, two_repositories):
        server = _Server(REPOMIND_MCP_DATABASE=TEST_DATABASE, REPOMIND_MCP_REPOSITORIES="library")
        answers = _converse(server)
        code = server.close()
        assert code == 0, b"".join(server.stderr).decode("utf-8", "replace")
        assert answers["initialized"]["result"]["serverInfo"]["name"] == "repomind"
        assert sorted(tool["name"] for tool in answers["listed"]["result"]["tools"]) == ["list_repositories", "search_code"]
        assert [entry["name"] for entry in answers["called"]["result"]["structuredContent"]["repositories"]] == ["library"]
        for line in server.stdout:
            message = json.loads(line)
            assert message.get("jsonrpc") == "2.0", "every stdout line is a JSON-RPC message"
        everything = b"".join(server.stdout + server.stderr).decode("utf-8", "replace")
        assert not leaks(everything)
        assert str(two_repositories) not in everything

    def test_without_settings_it_does_not_start_and_writes_nothing_to_stdout(self):
        process = subprocess.run([PYTHON, "-m", "repomind.mcp_server"], capture_output=True, cwd=PROJECT, env=_environment(), timeout=60)
        assert process.returncode == 2 and process.stdout == b""
        assert b"REPOMIND_MCP_DATABASE is not set" in process.stderr

    def test_with_a_database_that_does_not_exist_it_does_not_start(self, store):
        process = subprocess.run(
            [PYTHON, "-m", "repomind.mcp_server"], capture_output=True, cwd=PROJECT, timeout=60,
            env=_environment(REPOMIND_MCP_DATABASE="repomind_does_not_exist", REPOMIND_MCP_REPOSITORIES="library"),
        )
        assert process.returncode == 2 and process.stdout == b""
        assert b"cannot be reached" in process.stderr
        assert not leaks(process.stderr.decode("utf-8", "replace"))


def _trial_database_exists() -> bool:
    try:
        with psycopg.connect(_with_database(BASE_URL, TRIAL_DATABASE), connect_timeout=3):
            return True
    except psycopg.Error:
        return False


@pytest.mark.db
@pytest.mark.skipif(not _trial_database_exists(), reason="the trial database repomind_openai_trial is not here")
class TestTheTrialDatabase:
    def test_it_shows_library_and_only_library(self):
        server = _Server(REPOMIND_MCP_DATABASE=TRIAL_DATABASE, REPOMIND_MCP_REPOSITORIES="library")
        answers = _converse(server)
        assert server.close() == 0
        [entry] = answers["called"]["result"]["structuredContent"]["repositories"]
        assert (entry["name"], entry["index_version"], entry["available"]) == ("library", PIPELINE_VERSION, True)
        assert "fake-token" not in json.dumps(answers) and "fake-password" not in json.dumps(answers)
