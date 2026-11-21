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
import time
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
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
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


class LLMRequestError(RepoMindError):
    """A request to the OpenAI API that got no usable answer.

    ``kind`` says why: auth, forbidden, quota, rate_limit, bad_request, not_found, server, timeout,
    connection, redirect or bad_response. The message never holds the server's own text or the key.
    It is a RepoMindError, so everything that already falls back to search without a model still does.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        hint: str | None = None,
        *,
        status: int | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{message} Request ID: {request_id}." if request_id else message, hint)
        self.kind = kind
        self.status = status
        self.request_id = request_id
        # Set only when the server asked to wait and a second try is allowed.
        self.retry_after = retry_after


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is not followed: it would take the key, and the code in the request, to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None  # urllib then raises the redirect as an HTTPError


_NO_REDIRECTS = urllib.request.build_opener(_RefuseRedirects)
_sleep = time.sleep

# A second try is made only when the server asked to wait, for a temporary rate limit or an overload,
# and not for longer than this. Those requests were turned away, so the one request the budget allowed
# for is still the only one that can cost anything.
_MAX_RETRY_WAIT_SECONDS = 10.0
# Words in a 429's type or code that mean the account needs its owner, and waiting will not help.
_QUOTA_WORDS = ("quota", "credit", "billing", "spend_limit", "usage_limit")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _plain(value: object, pattern: re.Pattern[str] = _IDENTIFIER) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _retry_after(headers: object) -> float | None:
    """The wait the server asked for, in seconds. A date or anything else unclear means no second try."""
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _openai_error(exc: urllib.error.HTTPError) -> LLMRequestError:
    """What an error answer means, from its status and the API's type and code. Its text is not repeated."""
    status = exc.code
    request_id = _plain(exc.headers.get("x-request-id") if exc.headers is not None else None, _REQUEST_ID)
    try:
        error = json.loads(exc.read(8192)).get("error") or {}
    except (ValueError, AttributeError, OSError):
        error = {}
    if not isinstance(error, dict):
        error = {}
    kind_of, code, param = _plain(error.get("type")), _plain(error.get("code")), _plain(error.get("param"))
    found = dict(status=status, request_id=request_id)

    if 300 <= status < 400:
        return LLMRequestError(
            "redirect",
            f"OpenAI answered with a redirect (HTTP {status}). It is not followed, so that the key and the code are not sent elsewhere.",
            **found,
        )
    if status == 401:
        return LLMRequestError(
            "auth",
            "OpenAI did not accept the API key (HTTP 401).",
            hint="Check REPOMIND_LLM_API_KEY in .env, and that the key is active and allowed to use the API.",
            **found,
        )
    if status == 403:
        return LLMRequestError(
            "forbidden",
            "OpenAI refused the request (HTTP 403).",
            hint="The key's project or organisation, or the country or region the request comes from, may not have access.",
            **found,
        )
    if status == 404:
        return LLMRequestError(
            "not_found", "OpenAI has no such model for this key (HTTP 404).", hint="Check REPOMIND_LLM_MODEL.", **found
        )
    if status == 429:
        said = f"{kind_of or ''} {code or ''}".lower()
        if any(word in said for word in _QUOTA_WORDS):
            return LLMRequestError(
                "quota",
                f"OpenAI reports that the account's quota, credit or spending limit is used up (HTTP 429{f', {code}' if code else ''}).",
                hint="Waiting does not help. Check billing and limits in the OpenAI account.",
                **found,
            )
        wait = _retry_after(exc.headers)
        asked = f" It asks to wait {wait:g} seconds." if wait is not None else ""
        return LLMRequestError(
            "rate_limit",
            f"OpenAI's rate limit was reached (HTTP 429).{asked}",
            hint="Ask again in a moment.",
            retry_after=wait if wait is not None and wait <= _MAX_RETRY_WAIT_SECONDS else None,
            **found,
        )
    if 400 <= status < 500:
        named = "".join(
            part for part in (f" It names the parameter '{param}'." if param else "", f" Code: {code}." if code else "")
        )
        return LLMRequestError(
            "bad_request",
            f"OpenAI did not accept the request (HTTP {status}).{named}",
            hint=(
                "The model may not take a parameter RepoMind sends. For a reasoning model, set "
                "REPOMIND_LLM_REASONING_EFFORT, which leaves the temperature out. Otherwise check REPOMIND_LLM_MODEL."
            ),
            **found,
        )
    if 500 <= status < 600:
        wait = _retry_after(exc.headers) if status == 503 else None
        return LLMRequestError(
            "server",
            f"OpenAI had a server error (HTTP {status}).",
            hint="Ask again in a moment. A failed request is not sent again on its own, except after an overload with a wait.",
            retry_after=wait if wait is not None and wait <= _MAX_RETRY_WAIT_SECONDS else None,
            **found,
        )
    return LLMRequestError("bad_response", f"OpenAI answered with HTTP {status}.", **found)


def _reply(payload: object) -> LLMResponse:
    """The reply from a chat completion. None for the text, as after a refusal, is an empty reply."""
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


class OpenAICompatibleClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def complete(self, system: str, user: str, max_output_tokens: int) -> LLMResponse:
        if self.config.provider == "openai":
            return self._complete_with_openai(system, user, max_output_tokens)
        body = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_output_tokens,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{self.config.base_url}/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST"
        )
        if self.config.api_key:
            # Not carried over if the server redirects the request somewhere else.
            request.add_unredirected_header("Authorization", f"Bearer {self.config.api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            if self.config.api_key:
                detail = detail.replace(self.config.api_key, "<key>")
            raise RepoMindError(
                f"The LLM endpoint answered with HTTP {exc.code}: {detail}",
                hint=f"Check that model '{self.config.model}' is available at {self.config.base_url}.",
            ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise RepoMindError(
                f"Cannot reach the LLM endpoint at {self.config.base_url}: {exc}",
                hint="For the local model: docker compose --profile llm up -d",
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # A reply that is not JSON used to end the question with a crash.
            raise RepoMindError("The LLM endpoint returned an unexpected response shape.") from exc
        return _reply(payload)

    def _complete_with_openai(self, system: str, user: str, max_output_tokens: int) -> LLMResponse:
        body: dict[str, object] = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # Counts reasoning tokens as well as the reply, so it caps what one request can cost.
            "max_completion_tokens": max_output_tokens,
            "response_format": {"type": "json_object"},
            # OpenAI keeps nothing of this request for its distillation or evaluation products.
            "store": False,
        }
        if self.config.reasoning_effort is None:
            body["temperature"] = 0
        else:
            # Set for a reasoning model, and the temperature left out, since such a model may refuse one.
            body["reasoning_effort"] = self.config.reasoning_effort
        data = json.dumps(body).encode()
        url = f"{self.config.base_url}/chat/completions"

        for attempt in (1, 2):
            request = urllib.request.Request(url, data, {"Content-Type": "application/json"}, method="POST")
            request.add_unredirected_header("Authorization", f"Bearer {self.config.api_key}")
            try:
                with _NO_REDIRECTS.open(request, timeout=self.config.timeout_seconds) as response:
                    raw = response.read()
                    request_id = _plain(response.headers.get("x-request-id"), _REQUEST_ID)
                break
            except urllib.error.HTTPError as exc:
                error = _openai_error(exc)
                if attempt == 1 and error.retry_after is not None:
                    _sleep(error.retry_after)
                    continue
                raise error from exc
            except (TimeoutError, socket.timeout) as exc:
                raise self._timed_out() from exc
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    raise self._timed_out() from exc
                raise self._unreachable() from exc
            except OSError as exc:
                raise self._unreachable() from exc
        try:
            return _reply(json.loads(raw))
        except (ValueError, RepoMindError) as exc:
            raise LLMRequestError(
                "bad_response", "OpenAI returned something other than a chat completion.", request_id=request_id
            ) from exc

    def _timed_out(self) -> LLMRequestError:
        return LLMRequestError(
            "timeout",
            f"OpenAI did not answer within {self.config.timeout_seconds} seconds.",
            hint="It is not sent again, since it may have been processed and charged. REPOMIND_LLM_TIMEOUT_SECONDS sets the wait.",
        )

    @staticmethod
    def _unreachable() -> LLMRequestError:
        return LLMRequestError(
            "connection",
            "The OpenAI API cannot be reached.",
            hint="Check the internet connection, and any proxy or firewall in the way.",
        )
