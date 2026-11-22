"""What a reply cost when the provider caches input, and how the budget allows for it.

OpenAI bills input at one of three rates: uncached, read from its cache, or written to it. A cache write
is billed instead of the uncached rate, not on top of it. No request leaves this machine.
"""

from __future__ import annotations

import dataclasses
import email.message
import json

import pytest

from repomind.api.services import Services
from repomind.indexing import index_repository
from repomind.qa import llm as llm_module
from repomind.qa.engine import QAEngine
from repomind.qa.llm import OPENAI_BASE_URL, BudgetReached, LLMConfig, LLMResponse, OpenAICompatibleClient, usage_cost
from repomind.qa.prompt import _MESSAGE_OVERHEAD_TOKENS, estimate_tokens
from repomind.search.engine import SearchEngine

# The prices of gpt-6-luna on OpenAI's pricing page, read on 2026-11-22: input, cached input, the 30-minute
# cache write that Chat Completions uses, and output, in US dollars per million tokens. Test data only:
# nothing in RepoMind holds a model's prices.
LUNA = LLMConfig(
    OPENAI_BASE_URL, "gpt-6-luna", api_key="made-up-key-for-tests-0001", allow_remote=True, input_usd_per_mtok=0.10,
    output_usd_per_mtok=0.50, max_total_cost_usd=0.05, provider="openai", reasoning_effort="none",
    cached_input_usd_per_mtok=0.01, cache_write_usd_per_mtok=0.125,
)
# The usage of the one real request on 2026-11-22, as OpenAI returned it.
REAL_USAGE = {
    "prompt_tokens": 1050,
    "completion_tokens": 45,
    "total_tokens": 1095,
    "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 1047, "audio_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 0, "audio_tokens": 0, "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0},
}


class TestWhatAReplyCost:
    def test_the_real_request_costs_what_its_three_parts_cost(self):
        cost, cautious = usage_cost(LUNA, 1050, 45, cached_tokens=0, cache_write_tokens=1047)
        # 3 uncached at 0.10, 1,047 written at 0.125, 45 out at 0.50, per million.
        assert cost == pytest.approx(0.000153675, abs=1e-12)
        assert cautious is False

    def test_a_cache_write_replaces_the_input_price_and_is_not_added_to_it(self):
        written, _ = usage_cost(LUNA, 2000, 0, cached_tokens=0, cache_write_tokens=2000)
        assert written == pytest.approx(2000 * 0.125 / 1_000_000)
        assert written < usage_cost(LUNA, 2000, 0, 0, 0)[0] + 2000 * 0.125 / 1_000_000

    def test_reads_from_the_cache_cost_the_cached_price(self):
        cost, cautious = usage_cost(LUNA, 2000, 10, cached_tokens=1500, cache_write_tokens=0)
        assert cost == pytest.approx((500 * 0.10 + 1500 * 0.01 + 10 * 0.50) / 1_000_000)
        assert cautious is False

    def test_without_the_split_the_input_counts_at_the_highest_price_and_is_cautious(self):
        cost, cautious = usage_cost(LUNA, 1050, 45)
        assert cost == pytest.approx((1050 * 0.125 + 45 * 0.50) / 1_000_000)
        assert cautious is True

    def test_a_missing_write_count_alone_is_cautious_too(self):
        cost, cautious = usage_cost(LUNA, 1050, 45, cached_tokens=0, cache_write_tokens=None)
        assert cost == pytest.approx((1050 * 0.125 + 45 * 0.50) / 1_000_000) and cautious

    def test_parts_that_add_up_to_more_than_the_whole_count_at_the_highest_price(self):
        cost, cautious = usage_cost(LUNA, 100, 0, cached_tokens=80, cache_write_tokens=80)
        assert cost == pytest.approx(100 * 0.125 / 1_000_000) and cautious

    def test_a_server_without_cache_prices_is_counted_as_before(self):
        plain = LLMConfig("https://models.example.org/v1", "m", input_usd_per_mtok=2.0, output_usd_per_mtok=10.0, max_total_cost_usd=1.0)
        assert usage_cost(plain, 1000, 100) == (pytest.approx(0.003), False)
        assert usage_cost(plain, 1000, 100, cached_tokens=400, cache_write_tokens=300) == (pytest.approx(0.003), False)
        assert plain.worst_case_cost(1000) == pytest.approx((1000 * 2.0 + 250 * 10.0) / 1_000_000)

    def test_the_local_model_costs_nothing(self):
        local = LLMConfig("http://127.0.0.1:11535/v1", "qwen2.5:7b")
        assert local.is_free and usage_cost(local, 1500, 40) == (0.0, False) and local.worst_case_cost(4000) == 0.0

    def test_a_cache_price_alone_makes_a_model_paid(self):
        assert not dataclasses.replace(LLMConfig("http://127.0.0.1:1/v1", "m"), cache_write_usd_per_mtok=0.1).is_free


class TestTheBudgetAllowsForCacheWrites:
    def test_the_most_a_request_can_cost_uses_the_highest_input_price(self):
        assert LUNA.highest_input_rate == 0.125
        assert LUNA.worst_case_cost(4000) == pytest.approx((4000 * 0.125 + 250 * 0.50) / 1_000_000)
        assert LUNA.worst_case_cost(4000, 80) == pytest.approx((4000 * 0.125 + 80 * 0.50) / 1_000_000)

    def test_the_status_says_budget_reached_when_only_the_write_price_does_not_fit(self):
        at_input_price = (4000 * 0.10 + 250 * 0.50) / 1_000_000
        at_write_price = LUNA.worst_case_cost(4000)
        spent = LUNA.max_total_cost_usd - (at_input_price + at_write_price) / 2

        class Spent:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def llm_spending(self):
                return {"reported": spent}

        services = Services("postgresql://unused", None, None, LUNA, object(), ai_probe=lambda: pytest.fail("nothing may be asked"))
        services.store = Spent
        status = services.ai_status()
        assert status.state == "budget_reached"
        assert (status.spending.cached_input_usd_per_mtok, status.spending.cache_write_usd_per_mtok) == (0.01, 0.125)


class TestTheReplyIsReadWithItsCacheCounts:
    @pytest.fixture
    def openai(self, monkeypatch):
        def answer(usage):
            class Body:
                headers = email.message.Message()

                def read(self):
                    return json.dumps({"choices": [{"message": {"content": '{"chosen": []}'}, "finish_reason": "stop"}], "usage": usage}).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    return False

            class Opener:
                def open(self, request, timeout=None):
                    return Body()

            monkeypatch.setattr(llm_module, "_NO_REDIRECTS", Opener())
            return OpenAICompatibleClient(LUNA).complete("system", "user", 250)

        return answer

    def test_the_real_usage_gives_its_cache_counts(self, openai):
        response = openai(REAL_USAGE)
        assert (response.input_tokens, response.output_tokens, response.cached_tokens, response.cache_write_tokens) == (1050, 45, 0, 1047)

    @pytest.mark.parametrize(
        ("usage", "expected"),
        [
            ({"prompt_tokens": 10, "completion_tokens": 2}, (None, None)),
            ({"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": None}, (None, None)),
            ({"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 4}}, (4, None)),
            ({"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": "4", "cache_write_tokens": -1}}, (None, None)),
            ({"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": True, "cache_write_tokens": 3}}, (None, 3)),
        ],
    )
    def test_counts_that_are_missing_or_not_counts_are_unknown_and_not_zero(self, openai, usage, expected):
        response = openai(usage)
        assert (response.cached_tokens, response.cache_write_tokens) == expected


@pytest.mark.db
class TestWhatTheEngineRecords:
    @pytest.fixture
    def search(self, store, sample_repo, fake_embedder):
        index_repository(store, str(sample_repo), fake_embedder, name="shop")
        return SearchEngine(store, fake_embedder)

    class Replying:
        def __init__(self, response: LLMResponse) -> None:
            self.response = response
            self.calls = 0

        def complete(self, system, user, max_output_tokens):
            self.calls += 1
            return self.response

    def rows(self, store):
        return store.connection.execute("SELECT input_tokens, output_tokens, cost_usd, kind FROM llm_usage").fetchall()

    def test_the_real_usage_is_recorded_at_its_cost(self, store, search):
        result = QAEngine(store, search, LUNA, self.Replying(LLMResponse('{"chosen": []}', 1050, 45, "stop", 0, 1047))).ask("cancel order")
        assert result.cost_usd == pytest.approx(0.000153675, abs=1e-12)
        assert result.cost_is_cautious is False
        [row] = self.rows(store)
        assert (row["input_tokens"], row["output_tokens"], row["kind"]) == (1050, 45, "reported")
        assert float(row["cost_usd"]) == pytest.approx(0.000154, abs=1e-9), "the table keeps six decimals"

    def test_a_reply_without_the_split_is_recorded_at_most_and_said_to_be(self, store, search):
        result = QAEngine(store, search, LUNA, self.Replying(LLMResponse('{"chosen": []}', 1050, 45, "stop"))).ask("cancel order")
        assert result.cost_usd == pytest.approx((1050 * 0.125 + 45 * 0.50) / 1_000_000)
        assert result.cost_is_cautious is True
        [row] = self.rows(store)
        assert row["kind"] == "estimated"

    def test_a_reply_without_token_counts_ignores_any_cache_counts_and_is_cautious(self, store, search):
        result = QAEngine(store, search, LUNA, self.Replying(LLMResponse('{"chosen": []}', None, None, "stop", 0, 5))).ask("cancel order")
        assert result.tokens_are_estimated and result.cost_is_cautious
        [row] = self.rows(store)
        assert row["kind"] == "estimated"
        assert float(row["cost_usd"]) == pytest.approx(row["input_tokens"] * 0.125 / 1e6 + row["output_tokens"] * 0.5 / 1e6, abs=1e-6)

    def test_the_budget_check_allows_for_the_write_price(self, store, search):
        # A write price far above the input price, so that the gap between the two cannot be crossed by a
        # token's difference in the estimate or by the table's rounding.
        pricey = dataclasses.replace(LUNA, cache_write_usd_per_mtok=1.0)

        class Stop(Exception):
            pass

        class Measuring:
            tokens = 0

            def complete(self, system, user, max_output_tokens):
                Measuring.tokens = estimate_tokens(system) + estimate_tokens(user) + _MESSAGE_OVERHEAD_TOKENS
                raise Stop

        with pytest.raises(Stop):
            QAEngine(store, search, pricey, Measuring()).ask("cancel order")
        at_input_price = (Measuring.tokens * 0.10 + 250 * 0.50) / 1_000_000
        at_write_price = pricey.worst_case_cost(Measuring.tokens)
        # Spent so that the request would fit at the input price, and not at the write price.
        store.record_llm_usage(OPENAI_BASE_URL, "gpt-6-luna", 1, 1, pricey.max_total_cost_usd - (at_input_price + at_write_price) / 2, "reported")
        client = self.Replying(LLMResponse('{"chosen": []}', 1050, 45, "stop", 0, 1047))
        with pytest.raises(BudgetReached):
            QAEngine(store, search, pricey, client).ask("cancel order")
        assert client.calls == 0

    def test_a_failed_request_is_counted_at_the_highest_price(self, store, search):
        from repomind.qa.llm import LLMRequestError

        class Failing:
            def complete(self, system, user, max_output_tokens):
                raise LLMRequestError("timeout", "no answer", may_have_been_charged=True)

        with pytest.raises(LLMRequestError):
            QAEngine(store, search, LUNA, Failing()).ask("cancel order")
        [row] = self.rows(store)
        assert row["kind"] == "worst_case"
        assert float(row["cost_usd"]) == pytest.approx(LUNA.worst_case_cost(row["input_tokens"], row["output_tokens"]), abs=1e-6)
