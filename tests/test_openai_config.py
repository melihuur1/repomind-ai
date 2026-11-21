"""Choosing the OpenAI API, and keeping its key and the code it is sent safe.

Only the configuration is tested here. No request is made, and the key is made up.
"""

from __future__ import annotations

import math

import pytest

from repomind.errors import RepoMindError
from repomind.qa.llm import OPENAI_BASE_URL, LLMConfig, load_llm_config, validate_config

# Made up, and shaped so that it is not mistaken for a real key of any service.
KEY = "made-up-key-for-tests-0001"

SETTINGS = (
    "PROVIDER", "BASE_URL", "MODEL", "API_KEY", "ALLOW_REMOTE", "MAX_CONTEXT_CHUNKS", "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_TOKENS", "INPUT_USD_PER_MTOK", "OUTPUT_USD_PER_MTOK", "MAX_TOTAL_COST_USD", "SELECTION",
    "LINKED_EVIDENCE", "TIMEOUT_SECONDS", "REASONING_EFFORT",
)

OPENAI = {
    "PROVIDER": "openai", "MODEL": "an-openai-model", "API_KEY": KEY, "ALLOW_REMOTE": "1",
    "INPUT_USD_PER_MTOK": "0.4", "OUTPUT_USD_PER_MTOK": "1.6", "MAX_TOTAL_COST_USD": "1",
}


@pytest.fixture
def settings(monkeypatch):
    """Sets these settings and blanks every other one, so that nothing comes from a real .env."""

    def apply(**values: str) -> None:
        for name in SETTINGS:
            monkeypatch.setenv(f"REPOMIND_LLM_{name}", values.get(name, ""))

    return apply


def refused(settings, **values: str) -> RepoMindError:
    settings(**values)
    with pytest.raises(RepoMindError) as caught:
        load_llm_config()
    return caught.value


def said(error: RepoMindError) -> str:
    return f"{error} {error.hint or ''}"


class TestOllamaStaysAsItWas:
    def test_without_a_provider_the_local_model_is_used_as_before(self, settings):
        settings(BASE_URL="http://127.0.0.1:11535/v1/", MODEL="qwen2.5:7b")
        config = load_llm_config()
        assert config.provider == "ollama"
        assert config.base_url == "http://127.0.0.1:11535/v1"
        assert config.api_key is None and config.reasoning_effort is None
        assert config.is_free and config.max_total_cost_usd == 0

    def test_nothing_set_is_still_no_model_and_not_an_error(self, settings):
        settings()
        assert load_llm_config() is None
        settings(PROVIDER="ollama")
        assert load_llm_config() is None

    def test_a_server_of_ones_own_with_a_key_and_no_price_still_works_with_a_budget(self, settings):
        settings(
            BASE_URL="https://models.example.org/v1", MODEL="qwen2.5-coder:14b", API_KEY=KEY,
            ALLOW_REMOTE="1", MAX_TOTAL_COST_USD="1",
        )
        config = load_llm_config()
        assert config.provider == "ollama" and config.api_key == KEY

    def test_a_configuration_made_in_code_keeps_its_old_defaults(self):
        config = LLMConfig("http://127.0.0.1:11535/v1", "qwen2.5:7b")
        assert config.provider == "ollama" and config.reasoning_effort is None
        validate_config(config)

    def test_the_openai_address_is_not_reached_by_the_old_way_around_its_rules(self, settings):
        error = refused(
            settings, BASE_URL="https://api.openai.com/v1", MODEL="m", API_KEY=KEY, ALLOW_REMOTE="1",
            MAX_TOTAL_COST_USD="1",
        )
        assert "REPOMIND_LLM_PROVIDER=openai" in str(error)


class TestTheProviderSetting:
    def test_case_and_spaces_do_not_matter(self, settings):
        settings(**{**OPENAI, "PROVIDER": "  OpenAI "})
        assert load_llm_config().provider == "openai"

    def test_an_unknown_provider_is_named_with_the_ones_that_exist(self, settings):
        error = refused(settings, PROVIDER="opneai", MODEL="m")
        assert "must be ollama or openai" in str(error) and "'opneai'" in str(error)

    def test_an_unknown_provider_is_refused_even_with_nothing_else_set(self, settings):
        refused(settings, PROVIDER="anthropic")

    def test_a_key_pasted_into_the_provider_is_not_repeated(self, settings):
        error = refused(settings, PROVIDER=KEY, MODEL="m")
        assert KEY not in said(error) and "not shown" in str(error)

    def test_a_provider_made_in_code_is_checked_too(self):
        with pytest.raises(RepoMindError, match="must be ollama or openai"):
            validate_config(LLMConfig("http://127.0.0.1:11535/v1", "m", provider="other"))


class TestOpenAI:
    def test_a_complete_setup_uses_the_official_address(self, settings):
        settings(**OPENAI)
        config = load_llm_config()
        assert config.provider == "openai"
        assert config.base_url == OPENAI_BASE_URL == "https://api.openai.com/v1"
        assert config.api_key == KEY and config.allow_remote and not config.is_loopback
        assert (config.input_usd_per_mtok, config.output_usd_per_mtok, config.max_total_cost_usd) == (0.4, 1.6, 1.0)

    def test_the_official_address_may_also_be_written_out(self, settings):
        settings(**OPENAI, BASE_URL="https://api.openai.com/v1/")
        assert load_llm_config().base_url == OPENAI_BASE_URL

    @pytest.mark.parametrize(
        "address",
        [
            "http://api.openai.com/v1",
            "https://api.openai.com.example.org/v1",
            "https://models.example.org/v1",
            "https://api.openai.com:8443/v1",
            "https://api.openai.com/v2",
            "https://api.openai.com/v1?next=https://models.example.org",
            "http://127.0.0.1:11535/v1",
            "https://someone:password-in-url@api.openai.com/v1",
        ],
    )
    def test_no_other_address_gets_the_key(self, settings, address):
        error = refused(settings, **OPENAI, BASE_URL=address)
        assert OPENAI_BASE_URL in str(error)
        # The address set is not repeated: it could hold a secret, or point at a server that should not get one.
        assert address not in said(error) and "password-in-url" not in said(error)

    def test_a_configuration_made_in_code_cannot_point_the_key_elsewhere(self):
        with pytest.raises(RepoMindError, match="must be https://api.openai.com/v1"):
            LLMConfig("https://models.example.org/v1", "m", api_key=KEY, provider="openai")

    def test_a_model_is_required(self, settings):
        error = refused(settings, **{**OPENAI, "MODEL": ""})
        assert "REPOMIND_LLM_MODEL" in str(error)

    def test_a_key_is_required(self, settings):
        error = refused(settings, **{**OPENAI, "API_KEY": ""})
        assert "REPOMIND_LLM_API_KEY" in str(error) and ".env" in said(error)

    def test_a_key_of_spaces_counts_as_none(self, settings):
        error = refused(settings, **{**OPENAI, "API_KEY": "   "})
        assert "needs REPOMIND_LLM_API_KEY" in str(error)

    def test_consent_to_send_code_is_required(self, settings):
        error = refused(settings, **{**OPENAI, "ALLOW_REMOTE": "0"})
        assert "lines of code" in str(error) and "REPOMIND_LLM_ALLOW_REMOTE=1" in said(error)

    @pytest.mark.parametrize("price", ["INPUT_USD_PER_MTOK", "OUTPUT_USD_PER_MTOK"])
    @pytest.mark.parametrize("value", ["", "0"])
    def test_both_prices_are_required(self, settings, price, value):
        error = refused(settings, **{**OPENAI, price: value})
        assert "prices" in str(error) and "OpenAI's current price list" in said(error)

    @pytest.mark.parametrize("value", ["", "0"])
    def test_a_budget_is_required(self, settings, value):
        error = refused(settings, **{**OPENAI, "MAX_TOTAL_COST_USD": value})
        assert "budget" in str(error) and "REPOMIND_LLM_MAX_TOTAL_COST_USD above 0" in said(error)


class TestNumbersThatCannotBeRight:
    @pytest.mark.parametrize("name", ["INPUT_USD_PER_MTOK", "OUTPUT_USD_PER_MTOK", "MAX_TOTAL_COST_USD"])
    @pytest.mark.parametrize("value", ["-1", "-0.01", "nan", "inf"])
    def test_a_negative_or_endless_price_or_budget_is_refused_for_either_provider(self, settings, name, value):
        for base in ({"BASE_URL": "http://127.0.0.1:11535/v1", "MODEL": "m"}, OPENAI):
            error = refused(settings, **{**base, name: value})
            assert f"REPOMIND_LLM_{name} must be a number of 0 or more" in str(error)

    def test_a_word_where_a_price_belongs_is_named(self, settings):
        error = refused(settings, **{**OPENAI, "INPUT_USD_PER_MTOK": "cheap"})
        assert "must be a number, got 'cheap'" in str(error)

    def test_a_key_pasted_into_a_price_is_not_repeated(self, settings):
        error = refused(settings, **{**OPENAI, "INPUT_USD_PER_MTOK": KEY})
        assert KEY not in said(error) and "not shown" in str(error)

    def test_a_price_set_in_code_is_checked_too(self):
        with pytest.raises(RepoMindError, match="0 or more"):
            validate_config(LLMConfig("http://127.0.0.1:11535/v1", "m", max_total_cost_usd=-1.0))
        with pytest.raises(RepoMindError, match="0 or more"):
            validate_config(LLMConfig("http://127.0.0.1:11535/v1", "m", input_usd_per_mtok=math.nan))


class TestTheKeyIsNeverShown:
    def test_not_in_the_configurations_repr_or_str(self, settings):
        settings(**OPENAI)
        config = load_llm_config()
        assert KEY not in repr(config) and KEY not in str(config)
        assert "api_key" not in repr(config)
        assert KEY not in repr(LLMConfig("https://models.example.org/v1", "m", api_key=KEY))

    @pytest.mark.parametrize(
        "change",
        [
            {"ALLOW_REMOTE": "0"},
            {"INPUT_USD_PER_MTOK": "0"},
            {"MAX_TOTAL_COST_USD": "-1"},
            {"BASE_URL": "https://models.example.org/v1"},
            {"REASONING_EFFORT": "turbo"},
            {"SELECTION": "both"},
            {"TIMEOUT_SECONDS": "0"},
            {"MODEL": ""},
        ],
    )
    def test_not_in_any_error_about_the_rest_of_the_setup(self, settings, change):
        error = refused(settings, **{**OPENAI, **change})
        assert KEY not in said(error)
        assert KEY not in repr(error)

    def test_not_in_the_error_for_an_ollama_setup_either(self, settings):
        error = refused(settings, BASE_URL="https://models.example.org/v1", MODEL="m", API_KEY=KEY)
        assert "Refusing to send repository code" in str(error) and KEY not in said(error)

    @pytest.mark.parametrize("key", ["made-up key", "made-up-key\nX-Other: 1", "made-up-kéy"])
    def test_a_key_that_cannot_go_in_a_header_is_refused_without_being_repeated(self, settings, key):
        error = refused(settings, **{**OPENAI, "API_KEY": key})
        assert "cannot be sent in a request header" in str(error)
        assert key not in said(error) and "made-up" not in said(error)

    def test_spaces_around_a_key_are_dropped(self, settings):
        settings(**{**OPENAI, "API_KEY": f"  {KEY}  "})
        assert load_llm_config().api_key == KEY

    def test_an_address_holding_a_password_is_refused_without_being_repeated(self, settings):
        error = refused(settings, BASE_URL="https://someone:password-in-url@models.example.org/v1", MODEL="m", ALLOW_REMOTE="1")
        assert "user name or a password" in str(error)
        assert "password-in-url" not in said(error)


class TestReasoningEffort:
    def test_unset_it_is_left_to_the_model(self, settings):
        settings(**OPENAI)
        assert load_llm_config().reasoning_effort is None

    @pytest.mark.parametrize("value", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
    def test_each_value_the_api_names_is_kept(self, settings, value):
        settings(**OPENAI, REASONING_EFFORT=value)
        assert load_llm_config().reasoning_effort == value

    def test_case_and_spaces_do_not_matter(self, settings):
        settings(**OPENAI, REASONING_EFFORT=" Low ")
        assert load_llm_config().reasoning_effort == "low"

    def test_an_unknown_value_is_named_with_the_ones_that_exist(self, settings):
        error = refused(settings, **OPENAI, REASONING_EFFORT="turbo")
        assert "must be one of none, minimal, low, medium, high, xhigh, max" in str(error) and "'turbo'" in str(error)
        assert "depends on the model" in said(error)

    def test_it_is_not_taken_for_the_local_model(self, settings):
        error = refused(settings, BASE_URL="http://127.0.0.1:11535/v1", MODEL="qwen2.5:7b", REASONING_EFFORT="low")
        assert "only with REPOMIND_LLM_PROVIDER=openai" in str(error)
