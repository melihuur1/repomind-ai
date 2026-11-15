"""A reply from the model that cannot be used: empty, cut off, or not what was asked for.

Such a reply is neither an answer nor a refusal by the model. It gives no source, it keeps a
status of its own, and what was wrong with it stays on record. Every model here is a stand-in
that returns fixed text, so nothing is random and no model is loaded.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from repomind.api.app import create_app
from repomind.evaluation.ask_runner import _row
from repomind.indexing import index_repository
from repomind.qa import llm as llm_module
from repomind.qa.engine import QAEngine
from repomind.qa.llm import LLMConfig, LLMResponse, OpenAICompatibleClient
from repomind.qa.verification import verify_selection
from repomind.search.engine import SearchEngine
from tests.test_api import _services
from tests.test_qa import LOCAL, NONE

QUESTION = "cancel order payment"


class Scripted:
    """Returns one fixed reply, with the token counts and the finish reason an endpoint might give."""

    def __init__(self, text, input_tokens=100, output_tokens=20, finish_reason=None):
        self.reply = LLMResponse(text, input_tokens, output_tokens, finish_reason)
        self.calls = 0

    def complete(self, system, user, max_output_tokens):
        self.calls += 1
        return self.reply


class TestWhatAReplyIsTakenFor:
    @pytest.mark.parametrize("text", ["", " ", "\n\n\n", "\t \n"])
    def test_an_empty_reply_is_invalid_and_never_read_as_nothing_chosen(self, text):
        selection = verify_selection(text, [], "q")
        assert (selection.outcome, selection.problem) == ("invalid", "empty")
        assert selection.chosen == ()

    def test_only_an_empty_list_says_that_nothing_was_chosen(self):
        assert verify_selection('{"chosen": []}', [], "q").outcome == "none"
        assert verify_selection('{"chosen": []}', [], "q").problem is None

    @pytest.mark.parametrize(
        ("text", "finish_reason", "problem"),
        [
            ('{"chosen": ["P1"], "note": "It rel', "length", "cut_off"),
            ('{"chosen": ["P1"], "note": "It rel', "stop", "not_json"),
            ("I think it is the first one.", None, "not_json"),
            ('{"answer": "P1"}', None, "no_choice_list"),
            ('{"chosen": [1, 2]}', None, "no_choice_list"),
            ('{"chosen": ["P99"]}', None, "not_offered"),
        ],
    )
    def test_each_kind_of_unusable_reply_has_its_own_name(self, text, finish_reason, problem):
        selection = verify_selection(text, [], "q", finish_reason)
        assert (selection.outcome, selection.problem) == ("invalid", problem)

    def test_a_whole_reply_is_still_used_when_the_endpoint_says_it_ran_into_the_limit(self):
        assert verify_selection('{"chosen": []}', [], "q", "length").outcome == "none"


@pytest.mark.db
class TestAnInvalidReplyInAnAnswer:
    @pytest.fixture
    def search(self, store, sample_repo, fake_embedder):
        index_repository(store, str(sample_repo), fake_embedder, name="shop")
        return SearchEngine(store, fake_embedder)

    @pytest.mark.parametrize(
        ("client", "problem", "said"),
        [
            # What the model server sent back for one question of the sixth set: no text, and 0 for both counts.
            (Scripted("", 0, 0), "empty", "The model returned an empty reply."),
            (Scripted("   \n\n", 100, 30), "empty", "The model returned an empty reply."),
            (Scripted('{"chosen": ["P1"], "no', 100, 250, "length"), "cut_off", "The model's reply was cut off at its output limit."),
            (Scripted("not json", 100, 20), "not_json", "The model did not return the required JSON."),
            (Scripted('{"chosen": ["P99"]}', 100, 20), "not_offered", "The model did not return the required JSON."),
        ],
    )
    def test_it_gives_no_source_and_keeps_its_own_status_and_reason(self, store, search, client, problem, said):
        result = QAEngine(store, search, LOCAL, client).ask(QUESTION, ["shop"])
        assert client.calls == 1, "it is not asked again"
        assert result.status == "invalid_response"
        assert result.reply_problem == problem
        assert result.invalid_reply == client.reply.text
        assert said in result.warnings
        assert result.answer == "" and result.evidence == [] and result.citations == [] and result.review_candidates == []
        assert result.passages, "the closest lines are still there to read"
        assert {source.answer_check.value for source in result.sources()} == {"not_made"}, "none of them is an answer or a wrong one"

    def test_it_is_told_apart_from_a_refusal_by_the_model(self, store, search):
        refused = QAEngine(store, search, LOCAL, Scripted(NONE)).ask(QUESTION, ["shop"])
        empty = QAEngine(store, search, LOCAL, Scripted("", 0, 0)).ask(QUESTION, ["shop"])
        assert (refused.status, refused.reply_problem, refused.invalid_reply) == ("insufficient", None, None)
        assert (empty.status, empty.reply_problem) == ("invalid_response", "empty")

    def test_no_token_count_is_not_taken_for_a_truncated_prompt_or_for_nothing_spent(self, store, search):
        priced = LLMConfig(LOCAL.base_url, "m", input_usd_per_mtok=2.0, output_usd_per_mtok=10.0, max_total_cost_usd=1.0)
        result = QAEngine(store, search, priced, Scripted("", 0, 0)).ask(QUESTION, ["shop"])
        assert not any("truncated" in warning for warning in result.warnings), "0 tokens for a prompt is a missing count"
        assert result.tokens_are_estimated
        assert result.input_tokens > 0
        assert result.cost_usd > 0 and store.total_llm_cost() == pytest.approx(result.cost_usd), "the request is still accounted for"

    def test_a_small_count_is_still_reported_as_a_truncated_prompt(self, store, search):
        result = QAEngine(store, search, LOCAL, Scripted(NONE, 5, 3)).ask(QUESTION, ["shop"])
        assert sum("truncated" in warning for warning in result.warnings) == 1

    def test_an_evaluation_row_keeps_what_was_wrong_and_the_reply(self, store, search):
        result = QAEngine(store, search, LOCAL, Scripted('{"chosen": ["P1"], "no', 100, 250, "length")).ask(QUESTION, ["shop"])
        row = _row("q", "shop", "en", result, 1.0)
        assert (row["status"], row["reply_problem"], row["invalid_reply"]) == ("invalid_response", "cut_off", '{"chosen": ["P1"], "no')
        assert row["citations"] == [] and row["review_candidates"] == []
        assert not any(passage["chosen"] for passage in row["passages"])

    def test_the_api_says_invalid_and_why_and_shows_no_answer(self, indexed_shop, fake_embedder):
        with TestClient(create_app(_services(fake_embedder, Scripted("", 0, 0)))) as client:
            empty = client.post("/api/ask", json={"repo": "shop", "question": QUESTION}).json()
        assert (empty["verdict"], empty["reply_problem"]) == ("invalid_response", "empty")
        assert empty["sources"] and {source["role"] for source in empty["sources"]} == {"closest"}
        assert empty["model_reading"] is None
        assert "The model returned an empty reply." in empty["warnings"]
        assert "the model returned an empty reply" in empty["discarded"]
        with TestClient(create_app(_services(fake_embedder, Scripted(NONE)))) as client:
            refused = client.post("/api/ask", json={"repo": "shop", "question": QUESTION}).json()
        assert (refused["verdict"], refused["reply_problem"]) == ("not_enough_evidence", None)

    @pytest.fixture
    def indexed_shop(self, store, sample_repo, fake_embedder):
        index_repository(store, str(sample_repo), fake_embedder, name="shop")


class TestWhatTheClientPassesOn:
    """The reply of an OpenAI-compatible endpoint, as the client reads it. No request leaves the test."""

    @staticmethod
    def reading(monkeypatch, payload: dict) -> LLMResponse:
        monkeypatch.setattr(llm_module.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode("utf-8")))
        return OpenAICompatibleClient(LLMConfig("http://127.0.0.1:1/v1", "m")).complete("system", "user", 50)

    def test_an_empty_reply_with_no_counts_is_passed_on_as_it_is(self, monkeypatch):
        payload = {"choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
        assert self.reading(monkeypatch, payload) == LLMResponse("", 0, 0, "stop")

    def test_missing_text_is_an_empty_reply_and_not_an_error(self, monkeypatch):
        reply = self.reading(monkeypatch, {"choices": [{"message": {"role": "assistant", "content": None}, "finish_reason": "length"}]})
        assert reply == LLMResponse("", None, None, "length")

    def test_the_reason_it_stopped_is_kept_and_anything_else_there_is_ignored(self, monkeypatch):
        usage = {"prompt_tokens": 12, "completion_tokens": 3}
        whole = self.reading(monkeypatch, {"choices": [{"message": {"content": '{"chosen": []}'}, "finish_reason": "stop"}], "usage": usage})
        assert whole == LLMResponse('{"chosen": []}', 12, 3, "stop")
        odd = self.reading(monkeypatch, {"choices": [{"message": {"content": "x"}, "finish_reason": {"not": "text"}}], "usage": usage})
        assert odd.finish_reason is None
        silent = self.reading(monkeypatch, {"choices": [{"message": {"content": "x"}}]})
        assert silent == LLMResponse("x", None, None, None)
