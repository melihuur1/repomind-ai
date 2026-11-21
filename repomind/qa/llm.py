"""A minimal client for any OpenAI-compatible chat endpoint, with hard limits.

Sending source code to a third party and spending money are both opt-in: a non-loopback
endpoint needs an explicit setting, and paid usage needs a price and a budget.

Two providers share the client. "ollama", the default, is what RepoMind always did: Ollama, or any
OpenAI-compatible server at REPOMIND_LLM_BASE_URL. "openai" is the OpenAI API: its address is fixed,
and it needs a key, consent to send code, the model's prices and a budget.
"""

from __future__ import annotations

import json
import math
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

from repomind.config import get_setting
from repomind.errors import RepoMindError

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

PROVIDERS = ("ollama", "openai")
OPENAI_BASE_URL = "https://api.openai.com/v1"
_OPENAI_HOST = "api.openai.com"
# The values the OpenAI API names for reasoning effort. Which of them a model accepts depends on the
# model, so nothing here assumes that a model takes one.
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
# A key is sent in a request header: printable ASCII and no spaces.
_KEY_CHARACTERS = re.compile(r"[!-~]+")
# A setting's value is repeated in an error only when it is short and plain, so that a key pasted
# into the wrong setting is never printed.
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9_.+-]{1,20}")


def _shown(value: str) -> str:
    return f"'{value}'" if _PLAIN_VALUE.fullmatch(value) else "a value that is not shown"


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    # Never in repr, and so never in a log line or an error that prints the configuration.
    api_key: str | None = field(default=None, repr=False)
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
    # "choose": the model picks among all passages at once, the measured way and the default.
    # "judge": it judges each passage on its own. Experimental, and not shown to be better.
    selection: str = "choose"
    # Experimental, and off: a constant's declaration the model chose counts as evidence beside the line
    # that uses it. The link shows that one line refers to the other, not that either answers the question.
    linked_evidence: bool = False
    provider: str = "ollama"
    # For reasoning models of the OpenAI API, and only with that provider. None leaves it to the model.
    # It is checked here and not yet sent: the request is the next step.
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        # The key goes wherever base_url points, so an OpenAI configuration cannot point anywhere else,
        # however it was made.
        if self.provider == "openai" and self.base_url != OPENAI_BASE_URL:
            raise RepoMindError(
                f"With REPOMIND_LLM_PROVIDER=openai, REPOMIND_LLM_BASE_URL must be {OPENAI_BASE_URL} or left unset. "
                "Any other address is refused, so that the key cannot be sent to another server.",
            )

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
    provider = (get_setting("REPOMIND_LLM_PROVIDER", "ollama") or "ollama").strip().lower()
    if provider not in PROVIDERS:
        raise RepoMindError(f"REPOMIND_LLM_PROVIDER must be ollama or openai, got {_shown(provider)}.")
    base_url = get_setting("REPOMIND_LLM_BASE_URL")
    model = get_setting("REPOMIND_LLM_MODEL")
    if provider == "openai":
        # Choosing OpenAI is a decision, so a missing model is a mistake and not "no model".
        if not model:
            raise RepoMindError("REPOMIND_LLM_PROVIDER=openai needs REPOMIND_LLM_MODEL, the name of the OpenAI model to use.")
        base_url = base_url or OPENAI_BASE_URL
    elif not base_url or not model:
        return None

    def number(name: str, default: float) -> float:
        raw = get_setting(name)
        try:
            return float(raw) if raw is not None else default
        except ValueError as exc:
            raise RepoMindError(f"{name} must be a number, got {_shown(raw.strip())}.") from exc

    effort = (get_setting("REPOMIND_LLM_REASONING_EFFORT") or "").strip().lower()
    config = LLMConfig(
        base_url=base_url.rstrip("/"),
        model=model,
        api_key=(get_setting("REPOMIND_LLM_API_KEY") or "").strip() or None,
        allow_remote=get_setting("REPOMIND_LLM_ALLOW_REMOTE", "0") == "1",
        max_context_chunks=int(number("REPOMIND_LLM_MAX_CONTEXT_CHUNKS", 12)),
        max_input_tokens=int(number("REPOMIND_LLM_MAX_INPUT_TOKENS", 4000)),
        max_output_tokens=int(number("REPOMIND_LLM_MAX_OUTPUT_TOKENS", 250)),
        input_usd_per_mtok=number("REPOMIND_LLM_INPUT_USD_PER_MTOK", 0.0),
        output_usd_per_mtok=number("REPOMIND_LLM_OUTPUT_USD_PER_MTOK", 0.0),
        max_total_cost_usd=number("REPOMIND_LLM_MAX_TOTAL_COST_USD", 0.0),
        selection=(get_setting("REPOMIND_LLM_SELECTION", "choose") or "choose").strip().lower(),
        linked_evidence=get_setting("REPOMIND_LLM_LINKED_EVIDENCE", "0") == "1",
        # A larger model on a slower machine can take a minute for one answer.
        timeout_seconds=int(number("REPOMIND_LLM_TIMEOUT_SECONDS", 300)),
        provider=provider,
        reasoning_effort=effort or None,
    )
    validate_config(config)
    return config


def validate_config(config: LLMConfig) -> None:
    """Raises on the first problem. No message holds the key, and none repeats a value that could be one."""
    if config.provider not in PROVIDERS:
        raise RepoMindError(f"REPOMIND_LLM_PROVIDER must be ollama or openai, got {_shown(config.provider)}.")
    if config.timeout_seconds <= 0:
        raise RepoMindError(f"REPOMIND_LLM_TIMEOUT_SECONDS must be more than 0, got {config.timeout_seconds}.")
    if config.selection not in ("judge", "choose"):
        raise RepoMindError(f"REPOMIND_LLM_SELECTION must be judge or choose, got '{config.selection}'.")
    address = urlparse(config.base_url)
    if address.username is not None or address.password is not None:
        # Errors below repeat the address, and this one would carry a secret.
        raise RepoMindError(
            "REPOMIND_LLM_BASE_URL must not hold a user name or a password.",
            hint="Put a key in REPOMIND_LLM_API_KEY instead.",
        )
    if config.api_key is not None and not _KEY_CHARACTERS.fullmatch(config.api_key):
        raise RepoMindError("REPOMIND_LLM_API_KEY holds spaces or characters that cannot be sent in a request header.")
    for name, value in (
        ("REPOMIND_LLM_INPUT_USD_PER_MTOK", config.input_usd_per_mtok),
        ("REPOMIND_LLM_OUTPUT_USD_PER_MTOK", config.output_usd_per_mtok),
        ("REPOMIND_LLM_MAX_TOTAL_COST_USD", config.max_total_cost_usd),
    ):
        if not math.isfinite(value) or value < 0:
            raise RepoMindError(f"{name} must be a number of 0 or more, got {value}.")
    if config.reasoning_effort is not None:
        if config.provider != "openai":
            raise RepoMindError("REPOMIND_LLM_REASONING_EFFORT is used only with REPOMIND_LLM_PROVIDER=openai.")
        if config.reasoning_effort not in REASONING_EFFORTS:
            raise RepoMindError(
                f"REPOMIND_LLM_REASONING_EFFORT must be one of {', '.join(REASONING_EFFORTS)}, "
                f"got {_shown(config.reasoning_effort)}.",
                hint="Which of them a model accepts depends on the model. Leave it unset for a model that does not reason.",
            )
    if config.provider == "openai":
        _validate_openai(config)
    elif (address.hostname or "") == _OPENAI_HOST:
        raise RepoMindError(
            "The OpenAI API is set up with REPOMIND_LLM_PROVIDER=openai.",
            hint="That provider requires the model's prices and a budget, so that spending is counted and limited.",
        )
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


def _validate_openai(config: LLMConfig) -> None:
    if not config.api_key:
        raise RepoMindError(
            "REPOMIND_LLM_PROVIDER=openai needs REPOMIND_LLM_API_KEY.",
            hint="Set it in .env, which Git ignores. It is read from nowhere else.",
        )
    if not config.allow_remote:
        raise RepoMindError(
            "REPOMIND_LLM_PROVIDER=openai sends each question, and the lines of code chosen for it, to OpenAI.",
            hint="Set REPOMIND_LLM_ALLOW_REMOTE=1 to confirm that this is intended.",
        )
    if config.input_usd_per_mtok <= 0 or config.output_usd_per_mtok <= 0:
        raise RepoMindError(
            "REPOMIND_LLM_PROVIDER=openai needs the model's prices, so that spending can be counted.",
            hint=(
                "Set REPOMIND_LLM_INPUT_USD_PER_MTOK and REPOMIND_LLM_OUTPUT_USD_PER_MTOK above 0, in US dollars "
                "per million tokens, from OpenAI's current price list for the model."
            ),
        )
    if config.max_total_cost_usd <= 0:
        raise RepoMindError(
            "REPOMIND_LLM_PROVIDER=openai needs a budget.",
            hint="Set REPOMIND_LLM_MAX_TOTAL_COST_USD above 0. A request that could go over it is not sent.",
        )


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    # Why the endpoint stopped writing: "stop", "length" at the output limit, or None when it did not say.
    finish_reason: str | None = None


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
            choice = payload["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RepoMindError("The LLM endpoint returned an unexpected response shape.") from exc
        usage = payload.get("usage") or {}
        finish = choice.get("finish_reason")
        # An empty reply is passed on as one, with whatever the endpoint said about it. The engine decides what it means.
        return LLMResponse(
            text if isinstance(text, str) else "",
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            finish if isinstance(finish, str) else None,
        )
