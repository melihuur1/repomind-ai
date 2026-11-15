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
from repomind.qa.llm import LLMClient, LLMConfig, LLMResponse, OpenAICompatibleClient, load_llm_config
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


def public_message(text: str) -> str:
    """Errors from lower layers can name local folders, which a web response has no reason to show."""
    return _LOCAL_PATH.sub("<local path>", text)


def data_directory() -> Path:
    """Where clones of repositories added by URL live. Nothing outside it is ever written or removed."""
    return Path(get_setting("REPOMIND_DATA_DIR", str(PROJECT_ROOT / ".data")))


class LLMUnavailable(RepoMindError):
    """The model could not be reached. It is not a problem with the question."""


class _GuardedClient:
    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner

    def complete(self, system: str, user: str, max_output_tokens: int) -> LLMResponse:
        try:
            return self.inner.complete(system, user, max_output_tokens)
        except RepoMindError as exc:
            raise LLMUnavailable(str(exc)) from exc


def _host(config: LLMConfig) -> str:
    return urlparse(config.base_url).hostname or "another machine"


def _probe_openai_compatible(config: LLMConfig) -> AiStatus:
    """Asks the endpoint which models it has. The same request works for Ollama and for any OpenAI-compatible server."""
    remote = not config.is_loopback
    where = {"remote": remote, "endpoint": _host(config) if remote else None}
    request = urllib.request.Request(
        f"{config.base_url}/models", headers={"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    )
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
    ) -> None:
        self.database_url = database_url
        self.embedder = embedder
        self.translator = translator
        self.llm_config = llm_config
        self.llm_client = llm_client
        self.free_memory = free_memory
        self.limits = limits
        self.web_root = web_root
        self._ai_probe = ai_probe
        self._ai_status: tuple[float, AiStatus] | None = None
        self._ai_status_lock = threading.Lock()
        self.ask_lock = threading.Lock()
        self.keyword_indexes: dict[tuple, KeywordIndex] = {}

    @classmethod
    def production(cls, settings: Settings) -> "Services":
        try:
            config = load_llm_config()
        except RepoMindError:
            # A wrong LLM setting must not take search down. The status line reports it as not configured.
            config = None
        web_root = Path(get_setting("REPOMIND_WEB_ROOT", str(PROJECT_ROOT / "web" / "dist")))
        return cls(
            settings.database_url,
            FastEmbedProvider(settings.embedding_model, settings.models_dir),
            LocalTurkishTranslator(settings.models_dir),
            config,
            OpenAICompatibleClient(config) if config else None,
            web_root=web_root,
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

    def ai_status(self) -> AiStatus:
        """The model's state, asked for at most every few seconds, because the page polls it."""
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
        with self._ai_status_lock:
            now = time.monotonic()
            if self._ai_status and now - self._ai_status[0] < _AI_STATUS_SECONDS:
                return self._ai_status[1]
            status = self._ai_probe() if self._ai_probe else _probe_openai_compatible(self.llm_config)
            self._ai_status = (now, status)
            return status

    def forget_ai_status(self) -> None:
        with self._ai_status_lock:
            self._ai_status = None

    def ask(self, store: Store, repo: str, question: str) -> tuple[AskResult, AiStatus, bool]:
        """The result, the model's state, and whether the model took part. A model that is off is not an error."""
        status = self.ai_status()
        search = self.search_engine(store)
        if status.state == "ready":
            try:
                return QAEngine(store, search, self.llm_config, _GuardedClient(self.llm_client)).ask(question, [repo]), status, True
            except LLMUnavailable:
                self.forget_ai_status()
                status = AiStatus(
                    state="offline",
                    model=self.llm_config.model,
                    detail="The model stopped responding during this question."
                    + (f" {START_THE_MODEL}" if self.llm_config.is_loopback else ""),
                    remote=not self.llm_config.is_loopback,
                    endpoint=None if self.llm_config.is_loopback else _host(self.llm_config),
                )
        return QAEngine(store, search, None, None).ask(question, [repo]), status, False
