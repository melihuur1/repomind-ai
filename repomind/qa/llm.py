"""A minimal client for any OpenAI-compatible chat endpoint, with hard limits.

Sending source code to a third party and spending money are both opt-in: a non-loopback
endpoint needs an explicit setting, and paid usage needs a price and a budget.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from repomind.config import get_setting
from repomind.errors import RepoMindError

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str | None = None
    allow_remote: bool = False
    # Passage selection narrows these to a few lines, so more of them cost no model time.
    max_context_chunks: int = 12
    max_input_tokens: int = 4000
    # Per request. The reply is a short list of passage ids and at most one sentence.
    max_output_tokens: int = 250
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0
    max_total_cost_usd: float = 0.0
    timeout_seconds: int = 300
    # "judge": the model judges each passage on its own. "choose": it picks among all of them at once.
    selection: str = "judge"

    @property
    def is_loopback(self) -> bool:
        return (urlparse(self.base_url).hostname or "") in _LOOPBACK_HOSTS

    @property
    def is_free(self) -> bool:
        return self.input_usd_per_mtok == 0 and self.output_usd_per_mtok == 0

    def worst_case_cost(self, input_tokens: int) -> float:
        return cost_usd(self, input_tokens, self.max_output_tokens)


def cost_usd(config: LLMConfig, input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * config.input_usd_per_mtok + output_tokens * config.output_usd_per_mtok) / 1_000_000


def load_llm_config() -> LLMConfig | None:
    """None means no LLM is configured, which is a normal state and not an error."""
    base_url = get_setting("REPOMIND_LLM_BASE_URL")
    model = get_setting("REPOMIND_LLM_MODEL")
    if not base_url or not model:
        return None

    def number(name: str, default: float) -> float:
        raw = get_setting(name)
        try:
            return float(raw) if raw is not None else default
        except ValueError as exc:
            raise RepoMindError(f"{name} must be a number, got '{raw}'.") from exc

    config = LLMConfig(
        base_url=base_url.rstrip("/"),
        model=model,
        api_key=get_setting("REPOMIND_LLM_API_KEY"),
        allow_remote=get_setting("REPOMIND_LLM_ALLOW_REMOTE", "0") == "1",
        max_context_chunks=int(number("REPOMIND_LLM_MAX_CONTEXT_CHUNKS", 12)),
        max_input_tokens=int(number("REPOMIND_LLM_MAX_INPUT_TOKENS", 4000)),
        max_output_tokens=int(number("REPOMIND_LLM_MAX_OUTPUT_TOKENS", 250)),
        input_usd_per_mtok=number("REPOMIND_LLM_INPUT_USD_PER_MTOK", 0.0),
        output_usd_per_mtok=number("REPOMIND_LLM_OUTPUT_USD_PER_MTOK", 0.0),
        max_total_cost_usd=number("REPOMIND_LLM_MAX_TOTAL_COST_USD", 0.0),
        selection=(get_setting("REPOMIND_LLM_SELECTION", "judge") or "judge").strip().lower(),
    )
    validate_config(config)
    return config


def validate_config(config: LLMConfig) -> None:
    if config.selection not in ("judge", "choose"):
        raise RepoMindError(f"REPOMIND_LLM_SELECTION must be judge or choose, got '{config.selection}'.")
    if not config.is_loopback and not config.allow_remote:
        raise RepoMindError(
            f"Refusing to send repository code to the remote endpoint {config.base_url}.",
            hint="Set REPOMIND_LLM_ALLOW_REMOTE=1 if that is intended, and set its prices and a budget.",
        )
    if not config.is_loopback and config.is_free and config.max_total_cost_usd == 0 and config.api_key:
        raise RepoMindError(
            "A remote endpoint with an API key may charge for usage, but no price is configured.",
            hint=(
                "Set REPOMIND_LLM_INPUT_USD_PER_MTOK, REPOMIND_LLM_OUTPUT_USD_PER_MTOK and "
                "REPOMIND_LLM_MAX_TOTAL_COST_USD, so that cost limits can be enforced."
            ),
        )


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int | None
    output_tokens: int | None


class LLMClient(Protocol):
    def complete(self, system: str, user: str, max_output_tokens: int) -> LLMResponse: ...


class OpenAICompatibleClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def complete(self, system: str, user: str, max_output_tokens: int) -> LLMResponse:
        body = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_output_tokens,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        request = urllib.request.Request(
            f"{self.config.base_url}/chat/completions", json.dumps(body).encode(), headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise RepoMindError(
                f"The LLM endpoint answered with HTTP {exc.code}: {detail}",
                hint=f"Check that model '{self.config.model}' is available at {self.config.base_url}.",
            ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise RepoMindError(
                f"Cannot reach the LLM endpoint at {self.config.base_url}: {exc}",
                hint="For the local model: docker compose --profile llm up -d",
            ) from exc
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RepoMindError("The LLM endpoint returned an unexpected response shape.") from exc
        usage = payload.get("usage") or {}
        return LLMResponse(text or "", usage.get("prompt_tokens"), usage.get("completion_tokens"))
