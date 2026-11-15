"""A larger model on another machine: what is asked of it, and what the reader is told.

No request leaves these tests. The server, its address and its key are made up.
"""

from __future__ import annotations

import io
import json

import pytest

from repomind.api import services as services_module
from repomind.api.services import Services, _probe_openai_compatible
from repomind.errors import RepoMindError
from repomind.qa.llm import LLMConfig, load_llm_config, validate_config

REMOTE = LLMConfig("https://models.example.org/v1", "qwen2.5-coder:14b", api_key="made-up-key", allow_remote=True, max_total_cost_usd=1.0)


class TestAModelOnAnotherMachine:
    @staticmethod
    def serving(monkeypatch, models, seen):
        def urlopen(request, timeout=None):
            seen.append({"url": request.full_url, "authorization": request.get_header("Authorization"), "timeout": timeout})
            return io.BytesIO(json.dumps({"data": [{"id": name} for name in models]}).encode("utf-8"))

        monkeypatch.setattr(services_module.urllib.request, "urlopen", urlopen)

    def test_it_is_asked_with_its_key_and_the_reader_is_told_where_code_goes(self, monkeypatch):
        seen: list[dict] = []
        self.serving(monkeypatch, ["qwen2.5-coder:14b"], seen)
        status = _probe_openai_compatible(REMOTE)
        assert (status.state, status.remote, status.endpoint) == ("ready", True, "models.example.org")
        assert "sent to models.example.org" in status.detail and "made-up-key" not in status.model_dump_json()
        assert seen == [{"url": "https://models.example.org/v1/models", "authorization": "Bearer made-up-key", "timeout": 6.0}]

    def test_a_local_model_is_asked_as_before_and_is_not_called_remote(self, monkeypatch):
        seen: list[dict] = []
        self.serving(monkeypatch, ["qwen2.5:7b"], seen)
        status = _probe_openai_compatible(LLMConfig("http://127.0.0.1:11535/v1", "qwen2.5:7b"))
        assert (status.state, status.remote, status.endpoint, status.detail) == ("ready", False, None, "The local model is available.")
        assert seen[0]["authorization"] is None and seen[0]["timeout"] == 1.5

    def test_a_missing_model_and_an_unreachable_server_are_said_plainly(self, monkeypatch):
        self.serving(monkeypatch, ["something-else"], [])
        assert _probe_openai_compatible(REMOTE).state == "model_missing"

        def refuse(request, timeout=None):
            raise OSError("no route")

        monkeypatch.setattr(services_module.urllib.request, "urlopen", refuse)
        status = _probe_openai_compatible(REMOTE)
        assert status.state == "offline" and status.remote and "docker compose" not in status.detail

    def test_this_machines_memory_only_matters_for_a_model_on_this_machine(self):
        ready = lambda: services_module.AiStatus(state="ready", model="m", detail="ready")  # noqa: E731
        remote = Services("postgresql://unused", None, None, REMOTE, object(), ai_probe=ready, free_memory=lambda: 1.0)
        local = Services("postgresql://unused", None, None, LLMConfig("http://127.0.0.1:1/v1", "m"), object(), ai_probe=ready, free_memory=lambda: 1.0)
        assert remote.ai_status().state == "ready"
        assert local.ai_status().state == "low_memory"

    def test_repository_code_is_not_sent_anywhere_unless_that_is_asked_for(self):
        with pytest.raises(RepoMindError, match="Refusing to send repository code"):
            validate_config(LLMConfig("https://models.example.org/v1", "m"))
        validate_config(LLMConfig("https://models.example.org/v1", "m", allow_remote=True))
        with pytest.raises(RepoMindError, match="no price is configured"):
            validate_config(LLMConfig("https://models.example.org/v1", "m", allow_remote=True, api_key="made-up-key"))

    def test_the_settings_for_it_are_read_from_the_environment(self, monkeypatch):
        for name, value in {
            "REPOMIND_LLM_BASE_URL": "https://models.example.org/v1/", "REPOMIND_LLM_MODEL": "qwen2.5-coder:14b",
            "REPOMIND_LLM_API_KEY": "made-up-key", "REPOMIND_LLM_ALLOW_REMOTE": "1", "REPOMIND_LLM_MAX_TOTAL_COST_USD": "1",
            "REPOMIND_LLM_TIMEOUT_SECONDS": "120", "REPOMIND_LLM_SELECTION": "", "REPOMIND_LLM_LINKED_EVIDENCE": "",
        }.items():
            monkeypatch.setenv(name, value)
        config = load_llm_config()
        assert (config.base_url, config.model, config.timeout_seconds, config.is_loopback) == ("https://models.example.org/v1", "qwen2.5-coder:14b", 120, False)
        assert (config.selection, config.linked_evidence) == ("choose", False), "choosing at once and no linked evidence, whatever the model"
        monkeypatch.setenv("REPOMIND_LLM_TIMEOUT_SECONDS", "0")
        with pytest.raises(RepoMindError, match="TIMEOUT"):
            load_llm_config()
