"""The OpenAI provider in RepoMind's services: its status, its budget, and what a question does when it fails.

No request leaves this machine. OpenAI is stood in for by a fake opener, redirects are tested against
two HTTP servers on 127.0.0.1, and the key is made up.
"""

from __future__ import annotations

import email.message
import http.server
import io
import json
import threading
import urllib.error
from pathlib import Path

import pytest

from repomind.api import services as services_module
from repomind.api.services import START_THE_MODEL, Services, _GuardedClient
from repomind.config import Settings
from repomind.errors import RepoMindError
from repomind.indexing import index_repository
from repomind.qa import llm as llm_module
from repomind.qa.engine import AskResult, QAEngine
from repomind.qa.llm import OPENAI_BASE_URL, BudgetReached, LLMConfig, LLMRequestError, LLMResponse, OpenAICompatibleClient, cost_usd
from repomind.search.engine import SearchEngine

KEY = "made-up-key-for-tests-0001"
SERVER_TEXT = f"Incorrect API key provided: {KEY}. shard-7 detail"
OPENAI = LLMConfig(
    OPENAI_BASE_URL, "an-openai-model", api_key=KEY, allow_remote=True, input_usd_per_mtok=0.4,
    output_usd_per_mtok=1.6, max_total_cost_usd=1.0, provider="openai",
)
LOCAL = LLMConfig("http://127.0.0.1:11535/v1", "qwen2.5:7b")


def headers(**values: str) -> email.message.Message:
    message = email.message.Message()
    for name, value in values.items():
        message[name.replace("_", "-")] = value
    return message


class Answer:
    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode()
        self.headers = headers()

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def failure(status: int, error: dict | None = None, **head: str) -> urllib.error.HTTPError:
    body = json.dumps({"error": error}).encode() if error is not None else b""
    return urllib.error.HTTPError(OPENAI_BASE_URL, status, "error", headers(**head), io.BytesIO(body))


MODEL = Answer({"id": "an-openai-model", "object": "model", "owned_by": "openai"})
REPLY = Answer({"choices": [{"message": {"content": '{"chosen": []}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

# Each is made afresh for each use: an HTTP error's body can be read once.
FAILURES = [
    (lambda: failure(401, {"message": SERVER_TEXT, "code": "invalid_api_key"}, x_request_id="req_a1"), "auth_failed"),
    (lambda: failure(403, {"message": SERVER_TEXT}), "auth_failed"),
    (lambda: failure(404, {"message": SERVER_TEXT, "code": "model_not_found"}), "model_missing"),
    (lambda: failure(429, {"message": SERVER_TEXT, "type": "insufficient_quota", "code": "insufficient_quota"}), "limited"),
    (lambda: failure(429, {"message": SERVER_TEXT, "type": "rate_limit_error"}), "limited"),
    (lambda: failure(500, {"message": SERVER_TEXT}), "offline"),
    (lambda: failure(503, {"message": SERVER_TEXT, "code": "server_is_overloaded"}), "offline"),
    (lambda: failure(302, None, location="https://elsewhere.example.org/v1/models"), "rejected"),
    (lambda: failure(400, {"message": SERVER_TEXT, "param": "temperature"}), "rejected"),
    (lambda: TimeoutError("timed out"), "offline"),
    (lambda: urllib.error.URLError(ConnectionRefusedError("refused")), "offline"),
    (lambda: ConnectionResetError("reset"), "offline"),
]


class FakeOpenAI:
    def __init__(self, answers) -> None:
        self.answers = list(answers)
        self.sent: list[dict] = []

    def open(self, request, timeout=None):
        self.sent.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "data": request.data,
                "redirected_headers": dict(request.headers),
                "unredirected_headers": dict(request.unredirected_hdrs),
                "timeout": timeout,
            }
        )
        if not self.answers:
            raise AssertionError("an unexpected request was sent")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeStore:
    def __init__(self, spent: float = 0.0) -> None:
        self.spent = spent
        self.opened = 0

    def __enter__(self):
        self.opened += 1
        return self

    def __exit__(self, *exc_info):
        return False

    def total_llm_cost(self) -> float:
        return self.spent

    def llm_spending(self) -> dict[str, float]:
        return {"reported": self.spent}


class FakeEngine:
    """Stands in for the QA engine: asks the model once when it has one, and records which way it went."""

    runs: list[str] = []
    raise_budget = False

    def __init__(self, store, search, config, client) -> None:
        self.config, self.client = config, client

    def ask(self, question, repos):
        if self.client is None:
            FakeEngine.runs.append("without the model")
            return AskResult(status="no_llm", question=question)
        FakeEngine.runs.append("with the model")
        if FakeEngine.raise_budget:
            raise BudgetReached("This request could cost more than is left of the budget. Nothing was sent.")
        self.client.complete("system", "user", 10)
        return AskResult(status="answered", question=question)


@pytest.fixture
def openai(monkeypatch):
    def install(*answers) -> FakeOpenAI:
        fake = FakeOpenAI(answers)
        monkeypatch.setattr(llm_module, "_NO_REDIRECTS", fake)
        return fake

    monkeypatch.setattr(llm_module, "_sleep", lambda seconds: None)
    monkeypatch.setattr(llm_module.urllib.request, "urlopen", lambda *a, **k: pytest.fail("OpenAI must not use the plain urlopen"))
    monkeypatch.setattr(services_module.urllib.request, "urlopen", lambda *a, **k: pytest.fail("OpenAI must not use the plain urlopen"))
    return install


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(services_module.time, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def engine(monkeypatch):
    FakeEngine.runs, FakeEngine.raise_budget = [], False
    monkeypatch.setattr(services_module, "QAEngine", FakeEngine)
    return FakeEngine


def services(config: LLMConfig = OPENAI, spent: float = 0.0, **more) -> Services:
    built = Services("postgresql://unused", None, None, config, OpenAICompatibleClient(config), free_memory=lambda: 0.1, **more)
    built.fake_store = FakeStore(spent)
    built.store = lambda: built.fake_store
    built.search_engine = lambda store, include_tests=None: None
    return built


def never_shows_secrets(text: str) -> None:
    assert KEY not in text and "shard-7" not in text and "Incorrect API key" not in text


class TestTheModelCheck:
    def test_it_asks_about_the_model_with_a_get_that_sends_no_question_or_code(self, openai):
        fake = openai(MODEL)
        status = services().ai_status()
        [sent] = fake.sent
        assert (sent["method"], sent["url"], sent["data"]) == ("GET", "https://api.openai.com/v1/models/an-openai-model", None)
        assert sent["unredirected_headers"] == {"Authorization": f"Bearer {KEY}"}
        assert "Authorization" not in sent["redirected_headers"]
        assert sent["timeout"] == 6.0
        assert status.state == "ready" and status.remote and status.endpoint == "api.openai.com"
        assert "lines of code chosen for it, are sent to api.openai.com" in status.detail

    def test_a_model_name_cannot_change_the_address(self, openai):
        fake = openai(MODEL)
        services(LLMConfig(**{**OPENAI.__dict__, "model": "ft:gpt-x:org::abc/../../files?x=1"})).ai_status()
        assert fake.sent[0]["url"] == "https://api.openai.com/v1/models/ft:gpt-x:org::abc%2F..%2F..%2Ffiles%3Fx%3D1"

    def test_it_is_asked_once_a_minute(self, openai, clock):
        fake = openai(MODEL, MODEL)
        checked = services()
        for _ in range(5):
            assert checked.ai_status().state == "ready"
        assert len(fake.sent) == 1
        clock[0] += 59
        checked.ai_status()
        assert len(fake.sent) == 1
        clock[0] += 2
        checked.ai_status()
        assert len(fake.sent) == 2

    @pytest.mark.parametrize(("answer", "state"), FAILURES)
    def test_each_failure_has_its_own_state_and_says_nothing_secret(self, openai, answer, state):
        openai(answer())
        status = services().ai_status()
        assert status.state == state and status.remote and status.endpoint == "api.openai.com"
        never_shows_secrets(status.detail + repr(status))

    def test_the_key_problem_is_named(self, openai):
        openai(FAILURES[0][0]())
        status = services().ai_status()
        assert "did not accept the API key (HTTP 401)" in status.detail and "Request ID: req_a1." in status.detail
        assert "REPOMIND_LLM_API_KEY" in status.detail

    def test_the_memory_of_this_machine_does_not_stop_openai(self, openai):
        openai(MODEL)
        assert services().ai_status().state == "ready"

    def test_the_local_model_still_waits_for_memory(self, openai):
        local = Services("postgresql://unused", None, None, LOCAL, object(), free_memory=lambda: 0.1)
        assert local.ai_status().state == "low_memory"


class TestSettingsThatAreNotValid:
    @pytest.fixture
    def production(self, monkeypatch):
        monkeypatch.setattr(services_module, "FastEmbedProvider", lambda *args: None)
        monkeypatch.setattr(services_module, "LocalTurkishTranslator", lambda *args: None)

        def build(**values: str) -> Services:
            for name in ("PROVIDER", "BASE_URL", "MODEL", "API_KEY", "ALLOW_REMOTE", "INPUT_USD_PER_MTOK", "OUTPUT_USD_PER_MTOK",
                         "MAX_TOTAL_COST_USD", "SELECTION", "LINKED_EVIDENCE", "TIMEOUT_SECONDS", "REASONING_EFFORT",
                         "MAX_CONTEXT_CHUNKS", "MAX_INPUT_TOKENS", "MAX_OUTPUT_TOKENS"):
                monkeypatch.setenv(f"REPOMIND_LLM_{name}", values.get(name, ""))
            return Services.production(Settings("postgresql://unused", "fake-embedder", Path(".")))

        return build

    def test_a_wrong_setting_is_named_and_not_shown_as_not_configured(self, production):
        status = production(PROVIDER="openai", MODEL="m", ALLOW_REMOTE="1").ai_status()
        assert status.state == "misconfigured"
        assert "REPOMIND_LLM_API_KEY" in status.detail and "No model is configured" not in status.detail

    def test_a_key_in_the_wrong_setting_is_not_repeated(self, production):
        status = production(PROVIDER=KEY, MODEL="m").ai_status()
        assert status.state == "misconfigured" and KEY not in status.detail

    def test_nothing_set_is_still_not_configured(self, production):
        assert production().ai_status().state == "not_configured"

    def test_search_still_answers(self, production, engine):
        built = production(PROVIDER="openai", MODEL="m")
        built.search_engine = lambda store, include_tests=None: None
        result, status, used = built.ask(FakeStore(), "shop", "cancel order")
        assert (result.status, status.state, used) == ("no_llm", "misconfigured", False)
        assert engine.runs == ["without the model"]


class TestTheBudget:
    def test_spent_means_no_request_at_all(self, openai):
        fake = openai()
        status = services(spent=1.0).ai_status()
        assert status.state == "budget_reached" and fake.sent == []
        assert "$1.0000 of the $1.0000 budget is counted as spent" in status.detail and "Search still works" in status.detail

    def test_too_little_left_for_the_largest_request_is_spent(self, openai):
        fake = openai()
        largest = OPENAI.worst_case_cost(OPENAI.max_input_tokens)
        assert services(spent=1.0 - largest / 2).ai_status().state == "budget_reached"
        assert fake.sent == []

    def test_enough_left_and_the_model_is_checked(self, openai):
        openai(MODEL)
        assert services(spent=0.5).ai_status().state == "ready"

    def test_the_spending_is_read_each_time_and_not_cached(self, openai, clock):
        openai(MODEL)
        checked = services(spent=0.0)
        assert checked.ai_status().state == "ready"
        checked.fake_store.spent = 1.0
        assert checked.ai_status().state == "budget_reached"

    def test_spending_that_cannot_be_read_sends_nothing(self, openai):
        fake = openai()
        checked = services()

        def broken():
            raise RepoMindError("database down")

        checked.store = broken
        status = checked.ai_status()
        assert status.state == "offline" and "cannot be read" in status.detail and fake.sent == []

    def test_a_free_local_model_never_reads_the_spending(self):
        local = Services("postgresql://unused", None, None, LOCAL, object(), ai_probe=lambda: services_module.AiStatus(state="ready", detail="ok"),
                         free_memory=lambda: 64.0)
        local.store = lambda: pytest.fail("a free model has no budget to read")
        assert local.ai_status().state == "ready"

    def test_a_budget_reached_during_a_question_still_gives_search_results(self, openai, engine):
        openai(MODEL)
        engine.raise_budget = True
        result, status, used = services().ask(FakeStore(), "shop", "cancel order")
        assert (result.status, status.state, used) == ("no_llm", "budget_reached", False)
        assert engine.runs == ["with the model", "without the model"]


class TestAQuestionWhenOpenAIFails:
    @pytest.mark.parametrize(("answer", "state"), FAILURES)
    def test_search_still_answers_and_the_reason_is_given(self, openai, engine, answer, state):
        openai(MODEL, answer())
        result, status, used = services().ask(FakeStore(), "shop", "cancel order")
        assert (result.status, used) == ("no_llm", False)
        assert engine.runs == ["with the model", "without the model"]
        assert status.state == state
        assert status.detail.startswith("This question was answered without the model. ")
        never_shows_secrets(status.detail)

    def test_a_refused_key_is_not_tried_again_for_a_minute(self, openai, engine, clock):
        fake = openai(MODEL, FAILURES[0][0](), MODEL)
        checked = services()
        checked.ask(FakeStore(), "shop", "cancel order")
        clock[0] += 30
        later = checked.ai_status()
        assert later.state == "auth_failed" and not later.detail.startswith("This question")
        result, status, used = checked.ask(FakeStore(), "shop", "again")
        assert (status.state, used) == ("auth_failed", False)
        assert [sent["method"] for sent in fake.sent] == ["GET", "POST"], "no second question was sent with a refused key"
        clock[0] += 31
        assert checked.ai_status().state == "ready"

    def test_after_a_rate_limit_it_is_tried_again_sooner(self, openai, engine, clock):
        fake = openai(MODEL, failure(429, {"type": "rate_limit_error"}), MODEL)
        checked = services()
        checked.ask(FakeStore(), "shop", "cancel order")
        assert checked.ai_status().state == "limited"
        clock[0] += 16
        assert checked.ai_status().state == "ready"
        assert len(fake.sent) == 3

    def test_the_local_model_fails_as_before(self, engine):
        class Stopped:
            def complete(self, *args):
                raise RepoMindError("Cannot reach the LLM endpoint")

        probes = []
        local = Services("postgresql://unused", None, None, LOCAL, Stopped(),
                         ai_probe=lambda: probes.append(1) or services_module.AiStatus(state="ready", detail="ok"), free_memory=lambda: 64.0)
        local.search_engine = lambda store, include_tests=None: None
        result, status, used = local.ask(FakeStore(), "shop", "cancel order")
        assert (result.status, status.state, used) == ("no_llm", "offline", False)
        assert status.detail == f"The model stopped responding during this question. {START_THE_MODEL}"
        local.ai_status()
        assert len(probes) == 2, "the status is asked for again, as before"


class _Recorder(http.server.BaseHTTPRequestHandler):
    def _handle(self) -> None:
        self.server.seen.append({"method": self.command, "headers": dict(self.headers)})
        if self.server.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"data": [{"id": "m"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _handle

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def servers():
    started = []
    for _ in range(2):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        server.seen, server.redirect_to = [], None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
    yield started
    for server in started:
        server.shutdown()
        server.server_close()


def url_of(server, path: str = "/v1") -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


class TestTheModelCheckOverRealHTTP:
    def test_openai_does_not_follow_a_redirect(self, monkeypatch, servers):
        first, elsewhere = servers
        first.redirect_to = url_of(elsewhere, "/v1/models/m")
        monkeypatch.setattr(llm_module, "OPENAI_BASE_URL", url_of(first))
        config = LLMConfig(url_of(first), "m", api_key=KEY, allow_remote=True, input_usd_per_mtok=0.4,
                           output_usd_per_mtok=1.6, max_total_cost_usd=1.0, provider="openai")
        # The address is on this machine here, so the memory rule would apply. It does not for OpenAI's real address.
        checked = Services("postgresql://unused", None, None, config, OpenAICompatibleClient(config), free_memory=lambda: 64.0)
        checked.store = lambda: FakeStore()
        status = checked.ai_status()
        assert status.state == "rejected" and "redirect" in status.detail
        assert [seen["method"] for seen in first.seen] == ["GET"] and first.seen[0]["headers"]["Authorization"] == f"Bearer {KEY}"
        assert elsewhere.seen == []

    def test_a_server_of_ones_own_may_redirect_its_model_list_but_not_the_key(self, servers):
        first, elsewhere = servers
        first.redirect_to = url_of(elsewhere, "/v1/models")
        config = LLMConfig(url_of(first), "m", api_key=KEY)
        status = Services("postgresql://unused", None, None, config, object(), free_memory=lambda: 64.0).ai_status()
        assert status.state == "ready"
        [arrived] = elsewhere.seen
        assert "Authorization" not in arrived["headers"] and KEY not in json.dumps(arrived["headers"])


@pytest.mark.db
class TestWhatAFailedRequestMayHaveCost:
    @pytest.fixture
    def search(self, store, sample_repo, fake_embedder):
        index_repository(store, str(sample_repo), fake_embedder, name="shop")
        return SearchEngine(store, fake_embedder)

    class Failing:
        def __init__(self, error: RepoMindError) -> None:
            self.error = error
            self.calls = 0

        def complete(self, system, user, max_output_tokens):
            self.calls += 1
            raise self.error

    @staticmethod
    def rows(store):
        return store.connection.execute("SELECT input_tokens, output_tokens, cost_usd, kind FROM llm_usage").fetchall()

    @pytest.mark.parametrize("wrapped", [False, True])
    def test_a_request_that_may_have_reached_the_model_is_counted_at_its_most(self, store, search, wrapped):
        error = LLMRequestError("timeout", "OpenAI did not answer within 300 seconds.", may_have_been_charged=True)
        client = self.Failing(error)
        with pytest.raises(RepoMindError):
            QAEngine(store, search, OPENAI, _GuardedClient(client) if wrapped else client).ask("cancel order")
        [row] = self.rows(store)
        assert row["output_tokens"] == OPENAI.max_output_tokens and row["input_tokens"] > 0
        assert row["kind"] == "worst_case", "kept apart, so that it is never shown as what was charged"
        assert float(row["cost_usd"]) == pytest.approx(cost_usd(OPENAI, row["input_tokens"], OPENAI.max_output_tokens), abs=1e-6)
        assert store.total_llm_cost() > 0

    @pytest.mark.parametrize("kind", ["auth", "quota", "rate_limit", "bad_request", "redirect"])
    def test_a_request_turned_away_is_not_counted(self, store, search, kind):
        with pytest.raises(RepoMindError):
            QAEngine(store, search, OPENAI, self.Failing(LLMRequestError(kind, "refused"))).ask("cancel order")
        assert self.rows(store) == []

    def test_a_free_model_counts_nothing(self, store, search):
        error = LLMRequestError("timeout", "no answer", may_have_been_charged=True)
        with pytest.raises(RepoMindError):
            QAEngine(store, search, LOCAL, self.Failing(error)).ask("cancel order")
        assert self.rows(store) == []

    def test_a_spent_budget_sends_nothing_and_says_so_in_its_own_error(self, store, search):
        tiny = LLMConfig(**{**OPENAI.__dict__, "max_total_cost_usd": 0.000001})
        client = self.Failing(RepoMindError("must not be called"))
        with pytest.raises(BudgetReached, match="budget"):
            QAEngine(store, search, tiny, client).ask("cancel order")
        assert client.calls == 0
        assert issubclass(BudgetReached, RepoMindError)

    def test_a_reply_that_came_back_is_counted_as_reported(self, store, search):
        class Answers:
            def complete(self, system, user, max_output_tokens):
                return LLMResponse('{"chosen": []}', 1000, 100, "stop")

        result = QAEngine(store, search, OPENAI, Answers()).ask("cancel order")
        assert result.cost_usd == pytest.approx(cost_usd(OPENAI, 1000, 100))
        assert store.total_llm_cost() == pytest.approx(cost_usd(OPENAI, 1000, 100), abs=1e-6)
