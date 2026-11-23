"""A credential in a compose file stays out of every path to the model: search, passages and prompts.

The compose file below holds its credentials beside settings that answer questions, which is how
the YAML chunker came to withhold less than a whole service. These tests index it for real and
follow a question through search, passage selection and the prompts a model would receive.
"""

from __future__ import annotations

import pytest

from repomind.indexing import index_repository
from repomind.qa.engine import QAEngine
from repomind.qa.passages import select_passages
from repomind.search.engine import SearchEngine
from tests.conftest import commit_all, git, write
from tests.test_qa import LOCAL, FakeClient

SECRETS = ("URL_SECRET_VALUE", "JWT_DEFAULT_SECRET", "PG_SECRET_VALUE")
COMPOSE = (
    "services:\n"
    "  postgres:\n"
    "    image: postgres:17-alpine\n"
    "    environment:\n"
    "      POSTGRES_USER: app\n"
    "      POSTGRES_PASSWORD: PG_SECRET_VALUE\n"
    "  migrate:\n"
    "    build: { context: ., target: migrate }\n"
    "    environment:\n"
    "      DATABASE_URL: postgresql://app:URL_SECRET_VALUE@postgres:5432/app\n"
    "    depends_on:\n"
    "      postgres: { condition: service_healthy }\n"
    "  api:\n"
    "    build: { context: ., target: api }\n"
    "    environment:\n"
    "      API_PORT: 4000\n"
    "      JWT_SECRET: ${JWT_SECRET:-JWT_DEFAULT_SECRET}\n"
    "    depends_on:\n"
    "      migrate: { condition: service_completed_successfully }\n"
)
# Every word of the English one is in the code, so the refusal check lets it reach the model.
QUESTIONS = (
    "Which condition does the api service depend on for the migrate service?",
    "API başlamadan önce migrate servisini ve veritabanı parolasını nasıl bekliyor?",
)


@pytest.fixture
def search(store, tmp_path, fake_embedder):
    root = tmp_path / "compose-repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    write(root, "docker-compose.yml", COMPOSE)
    write(root, "src/server.ts", "export function start(port: number) {\n  return listen(port);\n}\n")
    commit_all(root, "compose")
    report = index_repository(store, str(root), fake_embedder, name="compose-repo")
    assert report.config_chunks_withheld == {"docker-compose.yml": 3}
    return SearchEngine(store, fake_embedder)


def holds_a_secret(text: str) -> bool:
    return any(secret in text for secret in SECRETS)


@pytest.mark.db
class TestCredentialsNeverReachTheModel:
    def test_the_settings_beside_them_can_be_found(self, search):
        hits = search.search(QUESTIONS[0], "hybrid", ["compose-repo"], 50).hits
        assert any("service_completed_successfully" in hit.chunk.content for hit in hits)
        assert any("target: migrate" in hit.chunk.content for hit in hits)

    @pytest.mark.parametrize("question", QUESTIONS)
    def test_no_search_candidate_holds_one(self, search, question):
        for mode in ("keyword", "semantic", "hybrid"):
            hits = search.search(question, mode, ["compose-repo"], 50, glossary_seats=5).hits
            assert hits and not any(holds_a_secret(hit.chunk.content) for hit in hits)

    @pytest.mark.parametrize("question", QUESTIONS)
    def test_no_passage_holds_one(self, search, fake_embedder, question):
        chunks = [hit.chunk for hit in search.search(question, "hybrid", ["compose-repo"], 50).hits]
        passages = select_passages(chunks, [question], fake_embedder)
        assert passages and not any(holds_a_secret(passage.text) for passage in passages)

    @pytest.mark.parametrize("question", QUESTIONS)
    def test_no_prompt_holds_one(self, store, search, question):
        model = FakeClient(lambda user: '{"english": "How does the api wait for the migrate service?"}' if "Passage" not in user and "<passage" not in user else '{"chosen": ["P1"], "note": "It waits."}')
        result = QAEngine(store, search, LOCAL, model).ask(question, ["compose-repo"])
        assert model.calls, "the question has to reach the model for this to mean anything"
        for call in model.calls:
            assert not holds_a_secret(call["system"]) and not holds_a_secret(call["user"])
        shown = [result.answer or "", result.note or "", *(p.text for p in result.passages), *(h.chunk.content for h in result.hits)]
        assert not any(holds_a_secret(text) for text in shown)

    def test_a_question_about_a_withheld_value_has_nothing_to_answer_from(self, store, search):
        # The word only appears on the withheld lines, so the code that is searched never mentions it.
        model = FakeClient('{"chosen": ["P1"], "note": "x"}')
        result = QAEngine(store, search, LOCAL, model).ask("What is the postgres password?", ["compose-repo"])
        assert result.status == "insufficient"
        assert model.calls == []
        assert not any(holds_a_secret(h.chunk.content) for h in result.hits)
