"""A credential written into code stays out of search, passages, prompts, API replies and the file view.

Every value here is made up. The repository below gives a credential-named constant a literal in
Java and in TypeScript, uses each from another line, and passes one inline to a call. It is indexed
twice: as the current pipeline does it, and as an index made before code credentials were withheld,
which still holds those lines. Nothing may show them either way.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from repomind.api.app import create_app
from repomind.indexing import index_repository
from repomind.ingest.filters import find_credential
from repomind.parsing import chunker
from repomind.qa.engine import QAEngine
from repomind.qa.passages import select_passages
from repomind.search.engine import SearchEngine
from tests.conftest import commit_all, git, write
from tests.test_api import _services
from tests.test_qa import LOCAL, FakeClient

FAKES = ("FAKE_JWT_SIGNING_VALUE", "FAKE_API_TOKEN_VALUE", "FAKE_DB_PASSWORD_VALUE")
TOKENS = """package shop;

class Tokens {
    private static final String ISSUER = "shop";
    private static final String JWT_SECRET = "FAKE_JWT_SIGNING_VALUE";

    /** Which issuer and subject sign uses for the token. */
    String sign(String subject) {
        return Jwts.builder().subject(subject).issuer(ISSUER).signWith(JWT_SECRET).compact();
    }
}
"""
SETTINGS = "export const DEFAULT_REGION = 'eu';\nexport const API_TOKEN = 'FAKE_API_TOKEN_VALUE';\n"
CLIENT = """import { API_TOKEN, DEFAULT_REGION } from './settings';

/** The client sends the region and the token with each request. */
export function client() {
  return create({ region: DEFAULT_REGION, token: API_TOKEN });
}
"""
DATABASE = """export function pool() {
  return connect({ user: 'app', password: 'FAKE_DB_PASSWORD_VALUE', database: 'shop' });
}
"""
QUESTION = "Which issuer and subject does sign use for the token?"
# Every word is in client.ts, which holds no credential itself, so this one reaches the model either way.
CLIENT_QUESTION = "Which region and token does the client send with each request?"


def holds_a_fake(text: str) -> bool:
    return any(fake in text for fake in FAKES)


@pytest.fixture(params=["indexed now", "indexed before code credentials were withheld"])
def repo(request, store, tmp_path, fake_embedder, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    git(root, "init", "-b", "main")
    write(root, "src/main/java/shop/Tokens.java", TOKENS)
    write(root, "src/settings.ts", SETTINGS)
    write(root, "src/client.ts", CLIENT)
    write(root, "src/database.ts", DATABASE)
    commit_all(root, "vault")
    if request.param == "indexed now":
        index_repository(store, str(root), fake_embedder, name="vault")
    else:
        with monkeypatch.context() as older:
            older.setattr(chunker, "hidden_lines", lambda language, text: [])
            older.setattr(chunker, "find_credential", lambda text, language: None)
            index_repository(store, str(root), fake_embedder, name="vault")
        stored = store.connection.execute("SELECT content FROM chunks").fetchall()
        assert holds_a_fake("\n".join(row["content"] for row in stored)), "this index has to hold them for the test to mean anything"
    return request.param


def choose_first(user: str) -> str:
    first = re.search(r'id="(P\d+)"', user)
    return json.dumps({"chosen": [first.group(1)] if first else [], "note": "It signs the token."})


@pytest.mark.db
class TestACredentialInCodeIsNeverShown:
    def test_no_search_result_holds_one_in_any_mode(self, repo, store, fake_embedder):
        search = SearchEngine(store, fake_embedder)
        for question in (QUESTION, "password database connect pool", "API_TOKEN client region", "JWT_SECRET"):
            for mode in ("keyword", "semantic", "hybrid"):
                for hit in search.search(question, mode, ["vault"], 50).hits:
                    assert not holds_a_fake(hit.chunk.content)
                    assert find_credential(hit.chunk.content, hit.chunk.language) is None

    def test_the_line_that_uses_one_is_evidence_and_its_declaration_is_never_fetched(self, repo, store, fake_embedder):
        search = SearchEngine(store, fake_embedder)
        result = QAEngine(store, search, None, None).ask(QUESTION, ["vault"])
        shown = [*result.warnings, *result.rejected, *(p.text for p in result.passages), *(h.chunk.content for h in result.hits)]
        assert not any(holds_a_fake(text) for text in shown)
        if repo == "indexed now":
            assert any("signWith(JWT_SECRET)" in passage.text for passage in result.passages), "the line that uses it is still evidence"
            asked_for = [c for c in result.completions if c.name == "JWT_SECRET"]
            assert asked_for and all(c.reason for c in asked_for), "its declaration was looked for, and not added"
        else:
            # The older index holds the small class as one chunk, with the credential, so all of it is left out.
            assert result.status == "insufficient" and not result.passages

    def test_no_prompt_and_nothing_in_the_answer_hold_one(self, repo, store, fake_embedder):
        model = FakeClient(choose_first)
        result = QAEngine(store, SearchEngine(store, fake_embedder), LOCAL, model).ask(CLIENT_QUESTION, ["vault"])
        assert model.calls, f"the question has to reach the model for this to mean anything: {result.status} {result.warnings}"
        for call in model.calls:
            assert not holds_a_fake(call["system"]) and not holds_a_fake(call["user"])
        shown = [result.answer or "", result.note or "", *result.warnings, *result.rejected, *(p.text for p in result.passages), *(h.chunk.content for h in result.hits)]
        assert not any(holds_a_fake(text) for text in shown)
        # The client's lines use both constants. The harmless one is completed, and the credential is not.
        added = {c.name for c in result.completions if c.declaration and not c.reason}
        refused = {c.name for c in result.completions if c.reason and c.name == "API_TOKEN"}
        assert "API_TOKEN" not in added and refused == {"API_TOKEN"}
        assert "DEFAULT_REGION = 'eu'" in "\n".join(p.text for p in result.passages)
        chunks = [hit.chunk for hit in SearchEngine(store, fake_embedder).search("API_TOKEN client region", "hybrid", ["vault"], 50).hits]
        assert not any(holds_a_fake(p.text) for p in select_passages(chunks, ["API_TOKEN client region"], fake_embedder))

    def test_no_api_reply_holds_one(self, repo, fake_embedder):
        model = FakeClient(choose_first)
        with TestClient(create_app(_services(fake_embedder, model))) as client:
            asked = client.post("/api/ask", json={"repo": "vault", "question": CLIENT_QUESTION})
            assert asked.status_code == 200 and asked.json()["ai_used"]
            assert not holds_a_fake(asked.text)
            assert not holds_a_fake(client.post("/api/ask", json={"repo": "vault", "question": QUESTION}).text)
            for query in ("JWT_SECRET", "password database connect", "API_TOKEN"):
                for mode in ("keyword", "semantic", "hybrid"):
                    found = client.get("/api/search", params={"repo": "vault", "q": query, "mode": mode})
                    assert found.status_code == 200 and not holds_a_fake(found.text)
        for call in model.calls:
            assert not holds_a_fake(call["user"])

    @pytest.mark.parametrize(
        ("path", "hidden"),
        [("src/main/java/shop/Tokens.java", [5]), ("src/settings.ts", [2]), ("src/database.ts", [2]), ("src/client.ts", [])],
    )
    def test_the_file_view_hides_the_statement_and_shows_the_rest(self, repo, fake_embedder, path, hidden):
        with TestClient(create_app(_services(fake_embedder))) as client:
            reply = client.get("/api/file", params={"repo": "vault", "path": path})
        assert reply.status_code == 200
        assert not holds_a_fake(reply.text)
        body = reply.json()
        assert body["hidden_lines"] == hidden
        assert all(body["lines"][number - 1] == "" for number in hidden)
        assert any(line.strip() for line in body["lines"]), "the rest of the file is still shown"


@pytest.mark.db
def test_nothing_the_current_pipeline_stores_holds_one(store, tmp_path, fake_embedder):
    root = tmp_path / "vault"
    root.mkdir()
    git(root, "init", "-b", "main")
    write(root, "src/main/java/shop/Tokens.java", TOKENS)
    write(root, "src/settings.ts", SETTINGS)
    write(root, "src/client.ts", CLIENT)
    write(root, "src/database.ts", DATABASE)
    commit_all(root, "vault")
    report = index_repository(store, str(root), fake_embedder, name="vault")
    rows = store.connection.execute(
        """
        SELECT path, content AS text FROM chunks
        UNION ALL SELECT path, coalesce(signature, '') FROM chunks
        UNION ALL SELECT path, signature FROM symbols
        """
    ).fetchall()
    everything = "\n".join(row["text"] for row in rows) + "\n".join(fake_embedder.embedded)
    assert not holds_a_fake(everything)
    assert "issuer(ISSUER)" in everything and "DEFAULT_REGION = 'eu'" in everything, "the lines beside them are indexed"
    assert report.config_chunks_withheld == {"src/main/java/shop/Tokens.java": 1, "src/settings.ts": 1, "src/database.ts": 1}
