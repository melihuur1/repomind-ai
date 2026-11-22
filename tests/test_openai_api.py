"""What the web API tells the page about the model: its provider, its spending, what was measured, and never the key.

Over a real database and a real Git repository, with a stand-in for the model. No request leaves this machine.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from repomind.api.app import create_app
from repomind.api.schemas import AiStatus
from repomind.api.services import Services
from repomind.config import Settings
from repomind.indexing import index_repository
from repomind.qa.llm import OPENAI_BASE_URL, LLMConfig, LLMResponse, cost_usd
from repomind.store.database import _SCHEMA_PATH as SCHEMA

KEY = "made-up-key-for-tests-0001"
OPENAI = LLMConfig(
    OPENAI_BASE_URL, "gpt-x-mini", api_key=KEY, allow_remote=True, input_usd_per_mtok=0.4,
    output_usd_per_mtok=1.6, max_total_cost_usd=1.0, provider="openai",
)
LOCAL = LLMConfig("http://127.0.0.1:11535/v1", "qwen2.5:7b")
READY = AiStatus(state="ready", detail="ready")


class Model:
    """Chooses the first passage, and reports the token counts it is given."""

    def __init__(self, input_tokens: int | None = 1500, output_tokens: int | None = 40) -> None:
        self.tokens = (input_tokens, output_tokens)

    def complete(self, system, user, max_output_tokens):
        if "<passage" not in user:
            return LLMResponse('{"english": "the question"}', *self.tokens)
        return LLMResponse('{"chosen": ["P1"], "note": "It cancels the order."}', *self.tokens)


def services(fake_embedder, config: LLMConfig, model=None) -> Services:
    url = Settings.load().database_url.rsplit("/", 1)[0] + "/repomind_test"
    return Services(url, fake_embedder, None, config, model or Model(), ai_probe=lambda: READY, free_memory=lambda: 0.1)


@pytest.fixture
def indexed(store, sample_repo, fake_embedder):
    index_repository(store, str(sample_repo), fake_embedder, name="shop")
    return sample_repo


@pytest.mark.db
class TestWhatThePageIsTold:
    def test_openai_is_named_with_its_host_and_spending_and_no_figure_that_was_not_measured(self, indexed, fake_embedder):
        with TestClient(create_app(services(fake_embedder, OPENAI))) as client:
            response = client.get("/api/health")
        ai = response.json()["ai"]
        assert (ai["state"], ai["provider"], ai["model"], ai["remote"], ai["endpoint"]) == ("ready", "openai", "gpt-x-mini", True, "api.openai.com")
        assert ai["accuracy"] is None
        assert ai["spending"] == {
            "budget_usd": 1.0, "recorded_usd": 0.0, "left_usd": 1.0, "worst_case_usd": 0.0, "estimated_usd": 0.0, "legacy_usd": 0.0,
            "input_usd_per_mtok": 0.4, "cached_input_usd_per_mtok": 0.4, "cache_write_usd_per_mtok": 0.4, "output_usd_per_mtok": 1.6,
        }
        assert KEY not in response.text and "api_key" not in response.text

    def test_the_measured_model_gets_its_measurement_and_a_free_model_no_spending(self, indexed, fake_embedder):
        with TestClient(create_app(services(fake_embedder, LOCAL))) as client:
            ai = client.get("/api/health").json()["ai"]
        assert (ai["provider"], ai["remote"], ai["endpoint"], ai["spending"]) == ("ollama", False, None, None)
        assert ai["accuracy"] == {
            "model": "qwen2.5:7b", "fully_right": 13, "wrong": 6, "questions": 32, "measured_on": "2026-11-15",
            "record": "eval/V2_EVALUATION.md",
        }

    @pytest.mark.parametrize("model", ["qwen2.5-coder:7b", "qwen2.5:3b", "fake"])
    def test_any_other_local_model_gets_no_figure(self, indexed, fake_embedder, model):
        with TestClient(create_app(services(fake_embedder, LLMConfig(LOCAL.base_url, model)))) as client:
            assert client.get("/api/health").json()["ai"]["accuracy"] is None

    def test_an_answer_gives_its_cost_and_the_spending_follows_it(self, indexed, fake_embedder):
        with TestClient(create_app(services(fake_embedder, OPENAI))) as client:
            answer = client.post("/api/ask", json={"repo": "shop", "question": "cancel order"})
            health = client.get("/api/health")
        body = answer.json()
        assert answer.status_code == 200 and body["ai_used"] is True
        assert body["cost_usd"] == pytest.approx(cost_usd(OPENAI, 1500, 40), abs=1e-6)
        assert body["tokens_are_estimated"] is False and (body["input_tokens"], body["output_tokens"]) == (1500, 40)
        spending = health.json()["ai"]["spending"]
        assert spending["recorded_usd"] == pytest.approx(body["cost_usd"], abs=1e-6)
        assert spending["left_usd"] == pytest.approx(1.0 - body["cost_usd"], abs=1e-6)
        assert (spending["worst_case_usd"], spending["estimated_usd"]) == (0.0, 0.0)
        for text in (answer.text, health.text):
            assert KEY not in text

    def test_an_answer_without_token_counts_is_said_to_be_estimated_and_counted_apart(self, indexed, fake_embedder):
        with TestClient(create_app(services(fake_embedder, OPENAI, Model(None, None)))) as client:
            body = client.post("/api/ask", json={"repo": "shop", "question": "cancel order"}).json()
            spending = client.get("/api/health").json()["ai"]["spending"]
        assert body["tokens_are_estimated"] is True and body["cost_usd"] > 0
        assert spending["estimated_usd"] == pytest.approx(spending["recorded_usd"], abs=1e-6) and spending["estimated_usd"] > 0

    def test_a_local_answer_costs_nothing(self, indexed, fake_embedder):
        with TestClient(create_app(services(fake_embedder, LOCAL))) as client:
            body = client.post("/api/ask", json={"repo": "shop", "question": "cancel order"}).json()
        assert (body["cost_usd"], body["ai"]["spending"]) == (0.0, None)


@pytest.mark.db
class TestSpendingByKind:
    def test_each_kind_is_summed_apart_and_old_rows_count_as_reported(self, store):
        store.connection.execute(
            "INSERT INTO llm_usage (base_url, model, input_tokens, output_tokens, cost_usd) VALUES ('u', 'm', 1, 1, 0.5)"
        )
        store.record_llm_usage("u", "m", 1, 1, 0.25, "worst_case")
        store.record_llm_usage("u", "m", 1, 1, 0.125, "estimated")
        assert store.llm_spending() == {"legacy": 0.5, "worst_case": 0.25, "estimated": 0.125}
        assert store.total_llm_cost() == pytest.approx(0.875)

    def test_rows_from_before_the_column_become_legacy_and_new_rows_keep_their_kind(self, store):
        """As on a database first opened by this version: its rows cannot be told apart, and are not called reported."""
        store.connection.execute("ALTER TABLE llm_usage DROP COLUMN kind")
        store.connection.execute("INSERT INTO llm_usage (base_url, model, input_tokens, output_tokens, cost_usd) VALUES ('u', 'm', 1, 1, 0.5)")
        store.connection.execute(SCHEMA.read_text(encoding="utf-8"))
        store.record_llm_usage("u", "m", 1, 1, 0.25)
        store.record_llm_usage("u", "m", 1, 1, 0.125, "estimated")
        kinds = [row["kind"] for row in store.connection.execute("SELECT kind FROM llm_usage ORDER BY id").fetchall()]
        assert kinds == ["legacy", "reported", "estimated"]

    def test_applying_the_schema_again_changes_no_row(self, store):
        store.record_llm_usage("u", "m", 1, 1, 0.25, "worst_case")
        store.connection.execute(SCHEMA.read_text(encoding="utf-8"))
        assert store.llm_spending() == {"worst_case": 0.25}

    def test_legacy_spending_is_shown_apart(self, store, indexed, fake_embedder):
        store.connection.execute("INSERT INTO llm_usage (base_url, model, input_tokens, output_tokens, cost_usd) VALUES ('u', 'm', 1, 1, 0.01)")
        store.record_llm_usage("u", "m", 1, 1, 0.02, "reported")
        with TestClient(create_app(services(fake_embedder, OPENAI))) as client:
            spending = client.get("/api/health").json()["ai"]["spending"]
        assert (spending["recorded_usd"], spending["legacy_usd"], spending["estimated_usd"]) == (0.03, 0.01, 0.0)
