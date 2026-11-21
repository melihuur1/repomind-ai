"""What the API shares between requests: models, the LLM, and the limits on using them."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from repomind.api.schemas import AiStatus
from repomind.config import PROJECT_ROOT, Settings, get_setting
from repomind.embeddings.provider import Embedder, FastEmbedProvider
from repomind.errors import RepoMindError
from repomind.evaluation.memory import available_memory_gb
from repomind.qa.engine import AskResult, QAEngine
from repomind.qa.llm import (
    BudgetReached,
    LLMClient,
    LLMConfig,
    LLMRequestError,
    LLMResponse,
    OpenAICompatibleClient,
    load_llm_config,
)
from repomind.search.engine import SearchConfig, SearchEngine
from repomind.search.keyword import KeywordIndex
from repomind.search.translation import LocalTurkishTranslator, QueryTranslator
from repomind.store.database import Store

# Generating an answer takes 7B about 10 GB of commit space while it loads, so it does not start below this.
MIN_FREE_MEMORY_GB = 4.0
_AI_STATUS_SECONDS = 5.0
_LOCAL_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/(?:home|Users|tmp|var|root|opt|mnt|srv)/)[^\s'\"<>|]*")
START_THE_MODEL = "Start it with: docker compose --profile llm up -d ollama"
# A server on another machine, perhaps behind a tunnel, gets longer to say which models it has.
_REMOTE_PROBE_SECONDS = 6.0
# OpenAI's model check is free, and still a request over the internet each time the page asks.
_OPENAI_STATUS_SECONDS = 60.0
# After trouble that passes by itself, OpenAI is tried again sooner.
_PASSING_TROUBLE_SECONDS = 15.0
_PASSING_TROUBLE = {"rate_limit", "server", "timeout", "connection"}
_STATE_FOR_KIND = {
    "auth": "auth_failed",
    "forbidden": "auth_failed",
    "quota": "limited",
    "rate_limit": "limited",
    "not_found": "model_missing",
    "server": "offline",
    "timeout": "offline",
    "connection": "offline",
    "bad_request": "rejected",
    "redirect": "rejected",
    "bad_response": "rejected",
}


def public_message(text: str) -> str:
    """Errors from lower layers can name local folders, which a web response has no reason to show."""
    return _LOCAL_PATH.sub("<local path>", text)


def data_directory() -> Path:
    """Where clones of repositories added by URL live. Nothing outside it is ever written or removed."""
    return Path(get_setting("REPOMIND_DATA_DIR", str(PROJECT_ROOT / ".data")))


class LLMUnavailable(RepoMindError):
    """The model could not be used for this question. It is not a problem with the question.

    It keeps what the client said about the failure: its kind, for the OpenAI API, and whether the
    request may have been charged.
    """

    def __init__(self, cause: RepoMindError) -> None:
        super().__init__(str(cause), cause.hint)
        self.kind: str | None = getattr(cause, "kind", None)
        self.may_have_been_charged: bool = getattr(cause, "may_have_been_charged", False)


class _GuardedClient:
    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner

    def complete(self, system: str, user: str, max_output_tokens: int) -> LLMResponse:
        try:
            return self.inner.complete(system, user, max_output_tokens)
        except RepoMindError as exc:
            raise LLMUnavailable(exc) from exc


def _host(config: LLMConfig) -> str:
    return urlparse(config.base_url).hostname or "another machine"


def _said(error: RepoMindError) -> str:
    """An error and its hint, for a reader. Errors from the client never hold the key or a server's text."""
    return public_message(" ".join(part for part in (str(error), error.hint) if part))


def _openai_status(config: LLMConfig, error: RepoMindError, during_question: bool = False) -> AiStatus:
    state = _STATE_FOR_KIND.get(getattr(error, "kind", None) or "", "offline")
    first = "This question was answered without the model. " if during_question else ""
    return AiStatus(state=state, model=config.model, detail=first + _said(error), remote=True, endpoint=_host(config))


def _probe_openai(config: LLMConfig) -> AiStatus:
    """Asks OpenAI whether the key can use the model. It sends no question and no code, and costs nothing."""
    try:
        OpenAICompatibleClient(config).check_model()
    except LLMRequestError as error:
        return _openai_status(config, error)
    return AiStatus(
        state="ready",
        model=config.model,
        detail=f"The OpenAI API is in use. Each question, and the lines of code chosen for it, are sent to {_host(config)}.",
        remote=True,
        endpoint=_host(config),
    )


def _probe_openai_compatible(config: LLMConfig) -> AiStatus:
    """Asks the endpoint which models it has. The same request works for Ollama and for any OpenAI-compatible server."""
    if config.provider == "openai":
        return _probe_openai(config)
    remote = not config.is_loopback
    where = {"remote": remote, "endpoint": _host(config) if remote else None}
    request = urllib.request.Request(f"{config.base_url}/models")
    if config.api_key:
        # Not carried over if the server redirects the request somewhere else.
        request.add_unredirected_header("Authorization", f"Bearer {config.api_key}")
    try:
        with urllib.request.urlopen(request, timeout=_REMOTE_PROBE_SECONDS if remote else 1.5) as response:
            models = {entry.get("id") for entry in json.load(response).get("data", [])}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        detail = (
            f"The remote model server at {_host(config)} cannot be reached. Check REPOMIND_LLM_BASE_URL, its key, and that the server is up."
            if remote
            else f"The local model server is not running. {START_THE_MODEL}"
        )
        return AiStatus(state="offline", model=config.model, detail=detail, **where)
    if config.model not in models:
        return AiStatus(
            state="model_missing",
            model=config.model,
            detail=f"The model server is running, but {config.model} is not installed on it.",
            **where,
        )
    detail = (
        f"A remote model is in use. Each question, and the lines of code chosen for it, are sent to {_host(config)}."
        if remote
        else "The local model is available."
    )
    return AiStatus(state="ready", model=config.model, detail=detail, **where)


@dataclass
class Limits:
    min_free_memory_gb: float = MIN_FREE_MEMORY_GB


class Services:
    def __init__(
        self,
        database_url: str,
        embedder: Embedder | None,
        translator: QueryTranslator | None,
        llm_config: LLMConfig | None,
        llm_client: LLMClient | None,
        ai_probe: Callable[[], AiStatus] | None = None,
        free_memory: Callable[[], float | None] = available_memory_gb,
        limits: Limits = Limits(),
        web_root: Path | None = None,
        llm_problem: str | None = None,
    ) -> None:
        self.database_url = database_url
        # What is wrong with the model settings, when they are set and not valid.
        self.llm_problem = llm_problem
        self.embedder = embedder
        self.translator = translator
        self.llm_config = llm_config
        self.llm_client = llm_client
        self.free_memory = free_memory
        self.limits = limits
        self.web_root = web_root
        self._ai_probe = ai_probe
        # When the status was found, the status, and for how many seconds it holds.
        self._ai_status: tuple[float, AiStatus, float] | None = None
        self._ai_status_lock = threading.Lock()
        self.ask_lock = threading.Lock()
        self.keyword_indexes: dict[tuple, KeywordIndex] = {}

    @classmethod
    def production(cls, settings: Settings) -> "Services":
        problem = None
        try:
            config = load_llm_config()
        except RepoMindError as exc:
            # A wrong LLM setting must not take search down. The status says what is wrong, and search works without a model.
            config, problem = None, _said(exc)
        web_root = Path(get_setting("REPOMIND_WEB_ROOT", str(PROJECT_ROOT / "web" / "dist")))
        return cls(
            settings.database_url,
            FastEmbedProvider(settings.embedding_model, settings.models_dir),
            LocalTurkishTranslator(settings.models_dir),
            config,
            OpenAICompatibleClient(config) if config else None,
            web_root=web_root,
            llm_problem=problem,
        )

    def prepare_database(self) -> None:
        with Store(self.database_url):
            pass

    def store(self) -> Store:
        return Store(self.database_url, apply_schema=False)

    def search_engine(self, store: Store, include_tests: bool | None = None) -> SearchEngine:
        return SearchEngine(
            store,
            self.embedder,
            SearchConfig(include_tests=include_tests),
            self.translator,
            keyword_indexes=self.keyword_indexes,
        )

    def ai_status(self, store: Store | None = None) -> AiStatus:
        """The model's state. The server is asked at most every few seconds, and OpenAI once a minute, as the page polls."""
        if self.llm_problem:
            return AiStatus(state="misconfigured", detail=f"The model settings are not valid, so no model is used. {self.llm_problem}")
        if self.llm_config is None or self.llm_client is None:
            return AiStatus(
                state="not_configured",
                detail="No model is configured. Set REPOMIND_LLM_BASE_URL and REPOMIND_LLM_MODEL in .env.",
            )
        # Only a model on this machine needs this machine's memory.
        free = self.free_memory() if self.llm_config.is_loopback else None
        if free is not None and free < self.limits.min_free_memory_gb:
            return AiStatus(
                state="low_memory",
                model=self.llm_config.model,
                detail=f"Only {free:.1f} GB of memory is free, so the model is not being started.",
            )
        # Before any request, and never cached: what was spent changes with every answer.
        over_budget = self._budget_status(store)
        if over_budget is not None:
            return over_budget
        with self._ai_status_lock:
            now = time.monotonic()
            if self._ai_status and now - self._ai_status[0] < self._ai_status[2]:
                return self._ai_status[1]
            status = self._ai_probe() if self._ai_probe else _probe_openai_compatible(self.llm_config)
            holds = _OPENAI_STATUS_SECONDS if self.llm_config.provider == "openai" else _AI_STATUS_SECONDS
            self._ai_status = (now, status, holds)
            return status

    def _budget_status(self, store: Store | None) -> AiStatus | None:
        """budget_reached when the largest request a question can make no longer fits in what is left."""
        config = self.llm_config
        if config is None or config.is_free:
            return None
        where = {"model": config.model, "remote": not config.is_loopback, "endpoint": None if config.is_loopback else _host(config)}
        try:
            if store is None:
                with self.store() as opened:
                    spent = opened.total_llm_cost()
            else:
                spent = store.total_llm_cost()
        except Exception:  # noqa: BLE001 - whatever stops the reading, nothing is sent without it
            return AiStatus(state="offline", detail="What has been spent on the model cannot be read, so nothing is sent to it.", **where)
        largest = config.worst_case_cost(config.max_input_tokens)
        if spent + largest <= config.max_total_cost_usd:
            return None
        return AiStatus(
            state="budget_reached",
            detail=(
                f"${spent:.4f} of the ${config.max_total_cost_usd:.4f} budget is spent, and the next request could cost up to "
                f"${largest:.4f}, so nothing more is sent to the model. Search still works. "
                "Raise REPOMIND_LLM_MAX_TOTAL_COST_USD if more spending is intended."
            ),
            **where,
        )

    def forget_ai_status(self) -> None:
        with self._ai_status_lock:
            self._ai_status = None

    def _remember(self, status: AiStatus, holds: float) -> None:
        with self._ai_status_lock:
            self._ai_status = (time.monotonic(), status, holds)

    def _status_after(self, failure: LLMUnavailable) -> AiStatus:
        """What the reader is told after the model failed during a question, and what the status says from then on."""
        config = self.llm_config
        if config.provider != "openai":
            self.forget_ai_status()
            return AiStatus(
                state="offline",
                model=config.model,
                detail="The model stopped responding during this question." + (f" {START_THE_MODEL}" if config.is_loopback else ""),
                remote=not config.is_loopback,
                endpoint=None if config.is_loopback else _host(config),
            )
        # A refused key or a used-up quota is not asked about again for a minute. A rate limit, sooner.
        self._remember(_openai_status(config, failure), _PASSING_TROUBLE_SECONDS if failure.kind in _PASSING_TROUBLE else _OPENAI_STATUS_SECONDS)
        return _openai_status(config, failure, during_question=True)

    def ask(self, store: Store, repo: str, question: str) -> tuple[AskResult, AiStatus, bool]:
        """The result, the model's state, and whether the model took part. A model that is off is not an error.

        Whatever stops the model, the question is still answered with the closest lines search finds.
        """
        status = self.ai_status(store)
        search = self.search_engine(store)
        if status.state == "ready":
            try:
                return QAEngine(store, search, self.llm_config, _GuardedClient(self.llm_client)).ask(question, [repo]), status, True
            except BudgetReached as exc:
                status = self._budget_status(store) or AiStatus(
                    state="budget_reached", model=self.llm_config.model, detail=_said(exc),
                    remote=not self.llm_config.is_loopback, endpoint=None if self.llm_config.is_loopback else _host(self.llm_config),
                )
            except LLMUnavailable as failure:
                status = self._status_after(failure)
        return QAEngine(store, search, None, None).ask(question, [repo]), status, False
