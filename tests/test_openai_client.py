"""What is sent to the OpenAI API, how its answers and errors are read, and where the key may go.

No request leaves this machine. The OpenAI API is stood in for by a fake opener, and redirects are
tested against two small HTTP servers on 127.0.0.1. The key is made up.
"""

from __future__ import annotations

import dataclasses
import email.message
import http.server
import io
import json
import threading
import urllib.error

import pytest

from repomind.api.services import LLMUnavailable, _GuardedClient
from repomind.errors import RepoMindError
from repomind.qa import llm as llm_module
from repomind.qa.llm import OPENAI_BASE_URL, LLMConfig, LLMRequestError, LLMResponse, OpenAICompatibleClient, validate_config
from repomind.qa.verification import verify_selection

KEY = "made-up-key-for-tests-0001"
OPENAI = LLMConfig(
    OPENAI_BASE_URL, "an-openai-model", api_key=KEY, allow_remote=True, input_usd_per_mtok=0.4,
    output_usd_per_mtok=1.6, max_total_cost_usd=1.0, timeout_seconds=45, provider="openai",
)
REASONING = dataclasses.replace(OPENAI, reasoning_effort="low")
# What a server might say in an error. None of it may reach the reader.
SERVER_TEXT = f"Incorrect API key provided: {KEY}. Internal detail: shard-7 stack trace."


def headers(**values: str) -> email.message.Message:
    message = email.message.Message()
    for name, value in values.items():
        message[name.replace("_", "-")] = value
    return message


class Answer:
    def __init__(self, payload: object, **head: str) -> None:
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.headers = headers(**head)

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def completion(text: str | None = '{"chosen": ["P1"], "note": "n"}', finish: str = "stop", usage=(1200, 40), **message):
    reply = {"choices": [{"message": {"role": "assistant", "content": text, **message}, "finish_reason": finish}]}
    if usage is not None:
        reply["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}
    return Answer(reply, x_request_id="req_ok123")


def failure(status: int, error: dict | bytes | None = None, **head: str) -> urllib.error.HTTPError:
    body = error if isinstance(error, bytes) else json.dumps({"error": error}).encode() if error is not None else b""
    return urllib.error.HTTPError(f"{OPENAI_BASE_URL}/chat/completions", status, "error", headers(**head), io.BytesIO(body))


class FakeOpenAI:
    """Answers from a list, in order, and keeps every request it was given."""

    def __init__(self, answers) -> None:
        self.answers = list(answers)
        self.sent: list[dict] = []

    def open(self, request, timeout=None):
        self.sent.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "body": json.loads(request.data),
                "redirected_headers": dict(request.headers),
                "unredirected_headers": dict(request.unredirected_hdrs),
                "timeout": timeout,
            }
        )
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def openai(monkeypatch):
    """Installs a fake OpenAI API. Waits are recorded and not slept, and the plain urlopen may not be used."""
    waits: list[float] = []

    def install(*answers) -> FakeOpenAI:
        fake = FakeOpenAI(answers)
        monkeypatch.setattr(llm_module, "_NO_REDIRECTS", fake)
        fake.waits = waits
        return fake

    def no_plain_urlopen(*args, **kwargs):
        raise AssertionError("the OpenAI provider must not use urllib's plain urlopen, which follows redirects")

    monkeypatch.setattr(llm_module, "_sleep", waits.append)
    monkeypatch.setattr(llm_module.urllib.request, "urlopen", no_plain_urlopen)
    return install


def ask(config: LLMConfig = OPENAI, max_output_tokens: int = 250) -> LLMResponse:
    return OpenAICompatibleClient(config).complete("Reply with one JSON object.", "the question and passages", max_output_tokens)


def refusal(config: LLMConfig = OPENAI) -> LLMRequestError:
    with pytest.raises(LLMRequestError) as caught:
        ask(config)
    return caught.value


def everything_said(error: RepoMindError) -> str:
    return f"{error} {error.hint or ''} {error!r}"


class TestTheRequest:
    def test_it_goes_to_the_official_chat_completions_address_with_the_key_in_its_header(self, openai):
        fake = openai(completion())
        ask()
        [sent] = fake.sent
        assert sent["url"] == "https://api.openai.com/v1/chat/completions" and sent["method"] == "POST"
        assert sent["unredirected_headers"] == {"Authorization": f"Bearer {KEY}"}
        assert "Authorization" not in sent["redirected_headers"], "the key must not be carried over to a redirect"
        assert sent["redirected_headers"] == {"Content-type": "application/json"}
        assert sent["timeout"] == 45

    def test_the_body_uses_the_current_parameters_and_nothing_a_model_may_refuse(self, openai):
        fake = openai(completion())
        ask(max_output_tokens=77)
        body = fake.sent[0]["body"]
        assert set(body) == {"model", "messages", "max_completion_tokens", "response_format", "store", "temperature"}
        assert body["model"] == "an-openai-model"
        assert body["max_completion_tokens"] == 77 and "max_tokens" not in body
        assert body["response_format"] == {"type": "json_object"}
        assert body["store"] is False
        assert body["temperature"] == 0
        assert body["messages"] == [
            {"role": "system", "content": "Reply with one JSON object."},
            {"role": "user", "content": "the question and passages"},
        ]

    def test_a_reasoning_model_gets_its_effort_and_no_temperature(self, openai):
        fake = openai(completion())
        ask(REASONING)
        body = fake.sent[0]["body"]
        assert body["reasoning_effort"] == "low"
        assert "temperature" not in body and "max_tokens" not in body
        assert body["max_completion_tokens"] == 250 and body["store"] is False

    def test_without_an_effort_none_is_sent(self, openai):
        fake = openai(completion())
        ask()
        assert "reasoning_effort" not in fake.sent[0]["body"]


class TestTheReply:
    def test_a_reply_keeps_its_text_tokens_and_finish_reason(self, openai):
        openai(completion())
        assert ask() == LLMResponse('{"chosen": ["P1"], "note": "n"}', 1200, 40, "stop")

    def test_no_content_after_a_refusal_is_an_empty_reply_and_its_text_is_not_kept(self, openai):
        openai(completion(None, refusal="I cannot help with that."))
        response = ask()
        assert response.text == "" and response.finish_reason == "stop"
        assert verify_selection(response.text, [], "q", response.finish_reason).problem == "empty"

    def test_json_cut_off_at_the_limit_is_read_as_cut_off(self, openai):
        openai(completion('{"chosen": ["P1"', finish="length", usage=(1200, 250)))
        response = ask()
        assert (response.text, response.output_tokens, response.finish_reason) == ('{"chosen": ["P1"', 250, "length")
        assert verify_selection(response.text, [], "q", response.finish_reason).problem == "cut_off"

    def test_a_reasoning_model_that_spent_the_limit_thinking_gives_an_empty_reply(self, openai):
        openai(completion("", finish="length", usage=(1200, 250)))
        response = ask(REASONING)
        assert (response.text, response.finish_reason) == ("", "length")
        # Never read as "nothing chosen". It is labelled empty, though the limit is what ended it.
        selection = verify_selection(response.text, [], "q", response.finish_reason)
        assert (selection.outcome, selection.problem) == ("invalid", "empty")

    def test_a_filtered_reply_is_passed_on_as_it_came(self, openai):
        openai(completion("", finish="content_filter"))
        assert ask().finish_reason == "content_filter"

    def test_missing_usage_is_missing_and_not_zero(self, openai):
        openai(completion(usage=None))
        response = ask()
        assert (response.input_tokens, response.output_tokens) == (None, None)

    @pytest.mark.parametrize("body", [b"<html>gateway</html>", b"", json.dumps({"object": "list"}).encode(), json.dumps({"choices": []}).encode()])
    def test_something_other_than_a_completion_is_a_bad_response(self, openai, body):
        openai(Answer(body, x_request_id="req_bad1"))
        error = refusal()
        assert error.kind == "bad_response" and error.request_id == "req_bad1"
        assert "gateway" not in str(error)


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "error", "kind", "words"),
        [
            (401, {"message": SERVER_TEXT, "type": "invalid_request_error", "code": "invalid_api_key"}, "auth", "did not accept the API key"),
            (403, {"message": SERVER_TEXT, "type": "invalid_request_error", "code": "unsupported_country_region_territory"}, "forbidden", "refused the request"),
            (404, {"message": SERVER_TEXT, "type": "invalid_request_error", "code": "model_not_found"}, "not_found", "no such model"),
            (400, {"message": SERVER_TEXT, "type": "invalid_request_error", "param": "temperature", "code": "unsupported_value"}, "bad_request", "did not accept the request"),
            (422, None, "bad_request", "did not accept the request"),
            (500, {"message": SERVER_TEXT, "type": "server_error"}, "server", "server error"),
            (502, b"<html>Bad gateway</html>", "server", "server error"),
            (503, {"message": SERVER_TEXT, "type": "service_unavailable_error", "code": "server_is_overloaded"}, "server", "server error"),
            (504, None, "server", "server error"),
        ],
    )
    def test_each_status_is_named_without_the_servers_text(self, openai, status, error, kind, words):
        fake = openai(failure(status, error, x_request_id="req_abc123"))
        caught = refusal()
        assert caught.kind == kind and caught.status == status and words in str(caught)
        assert f"HTTP {status}" in str(caught) and "Request ID: req_abc123." in str(caught)
        said = everything_said(caught)
        assert KEY not in said and "shard-7" not in said and "Bad gateway" not in said and "Incorrect API key" not in said
        assert len(fake.sent) == 1 and fake.waits == []

    def test_a_refused_parameter_is_named_with_the_way_out(self, openai):
        openai(failure(400, {"message": SERVER_TEXT, "type": "invalid_request_error", "param": "temperature", "code": "unsupported_value"}))
        error = refusal()
        assert "It names the parameter 'temperature'." in str(error) and "Code: unsupported_value." in str(error)
        assert "REPOMIND_LLM_REASONING_EFFORT" in error.hint

    def test_an_identifier_that_is_not_plain_is_not_repeated(self, openai):
        openai(failure(400, {"message": "x", "param": f"<script>{KEY}", "code": "a b c"}, x_request_id="req id with spaces"))
        error = refusal()
        assert "parameter" not in str(error) and "Code:" not in str(error) and "Request ID" not in str(error)
        assert KEY not in everything_said(error)

    @pytest.mark.parametrize(
        "error",
        [
            {"message": SERVER_TEXT, "type": "insufficient_quota", "code": "insufficient_quota"},
            {"message": SERVER_TEXT, "type": "insufficient_quota", "code": "credit_balance_exhausted"},
            {"message": SERVER_TEXT, "code": "organization_spend_limit_exceeded"},
            {"message": SERVER_TEXT, "code": "project_spend_limit_exceeded"},
            {"message": SERVER_TEXT, "code": "organization_usage_limit_exceeded"},
            {"message": SERVER_TEXT, "type": "insufficient_quota"},
        ],
    )
    def test_a_used_up_quota_is_told_apart_from_a_rate_limit_and_never_tried_again(self, openai, error):
        fake = openai(failure(429, error, retry_after="1"), completion())
        caught = refusal()
        assert caught.kind == "quota" and "Waiting does not help" in caught.hint
        assert len(fake.sent) == 1 and fake.waits == [], "a quota problem must not cost a second request"
        assert KEY not in everything_said(caught)

    @pytest.mark.parametrize("error", [{"message": SERVER_TEXT, "type": "rate_limit_error", "code": "slow_down"}, {"message": SERVER_TEXT}, None])
    def test_a_rate_limit_without_a_wait_is_not_tried_again(self, openai, error):
        fake = openai(failure(429, error), completion())
        caught = refusal()
        assert caught.kind == "rate_limit" and caught.retry_after is None
        assert len(fake.sent) == 1 and fake.waits == []

    @pytest.mark.parametrize(("exc", "kind"), [(TimeoutError("timed out"), "timeout"), (urllib.error.URLError(TimeoutError("timed out")), "timeout"),
                                                (urllib.error.URLError(ConnectionRefusedError("refused")), "connection"),
                                                (ConnectionResetError("reset"), "connection")])
    def test_no_answer_is_named_and_not_sent_again(self, openai, exc, kind):
        fake = openai(exc, completion())
        caught = refusal()
        assert caught.kind == kind and len(fake.sent) == 1 and fake.waits == []
        if kind == "timeout":
            assert "45 seconds" in str(caught) and "charged" in caught.hint

    def test_every_error_is_a_repomind_error_so_search_without_a_model_still_answers(self, openai):
        openai(failure(401, {"message": SERVER_TEXT}))
        with pytest.raises(LLMUnavailable):
            _GuardedClient(OpenAICompatibleClient(OPENAI)).complete("s", "u", 10)


class TestTryingAgain:
    @pytest.mark.parametrize(("status", "error"), [(429, {"type": "rate_limit_error", "code": "slow_down"}), (429, None),
                                                   (503, {"type": "service_unavailable_error", "code": "server_is_overloaded"})])
    def test_once_after_the_wait_the_server_asked_for(self, openai, status, error):
        fake = openai(failure(status, error, retry_after="2"), completion(usage=(900, 30)))
        response = ask()
        assert fake.waits == [2.0] and len(fake.sent) == 2
        assert fake.sent[0]["body"] == fake.sent[1]["body"]
        # Only the answer that came back is counted.
        assert (response.input_tokens, response.output_tokens) == (900, 30)

    def test_never_more_than_twice(self, openai):
        fake = openai(failure(429, None, retry_after="1"), failure(429, None, retry_after="1"), completion())
        caught = refusal()
        assert caught.kind == "rate_limit" and len(fake.sent) == 2 and fake.waits == [1.0]

    @pytest.mark.parametrize("wait", ["60", "10.5", "-1", "Wed, 21 Oct 2026 07:28:00 GMT", "soon", "nan"])
    def test_not_after_a_long_or_unclear_wait(self, openai, wait):
        fake = openai(failure(429, None, retry_after=wait), completion())
        caught = refusal()
        assert caught.retry_after is None and len(fake.sent) == 1 and fake.waits == []

    def test_a_long_wait_is_said(self, openai):
        openai(failure(429, None, retry_after="60"))
        assert "It asks to wait 60 seconds." in str(refusal())

    @pytest.mark.parametrize("status", [500, 502, 504])
    def test_not_after_an_error_that_may_have_been_charged(self, openai, status):
        fake = openai(failure(status, None, retry_after="1"), completion())
        caught = refusal()
        assert caught.kind == "server" and len(fake.sent) == 1 and fake.waits == []

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_not_after_a_mistake_in_the_request(self, openai, status):
        fake = openai(failure(status, None, retry_after="1"), completion())
        refusal()
        assert len(fake.sent) == 1 and fake.waits == []


class _Recorder(http.server.BaseHTTPRequestHandler):
    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.server.seen.append(
            {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": self.rfile.read(length) if length else b""}
        )
        if self.server.redirect_to:
            self.send_response(self.server.redirect_code)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"error": {"message": "not here"}}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_HEAD = _handle

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def servers():
    """Two HTTP servers on 127.0.0.1: the first redirects to the second, which records what reaches it."""
    started = []

    def start():
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        server.seen, server.redirect_to, server.redirect_code = [], None, 302
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server

    first, second = start(), start()
    yield first, second
    for server in started:
        server.shutdown()
        server.server_close()


def url_of(server, path: str = "/v1") -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


class TestRedirectsOverRealHTTP:
    @pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
    def test_openai_never_follows_one(self, monkeypatch, servers, code):
        first, elsewhere = servers
        first.redirect_to, first.redirect_code = url_of(elsewhere, "/v1/chat/completions"), code
        # The address is fixed for this provider. Only here is it pointed at the first server.
        monkeypatch.setattr(llm_module, "OPENAI_BASE_URL", url_of(first))
        monkeypatch.setattr(llm_module, "_sleep", lambda seconds: pytest.fail("no wait expected"))
        config = LLMConfig(url_of(first), "an-openai-model", api_key=KEY, allow_remote=True, input_usd_per_mtok=0.4,
                           output_usd_per_mtok=1.6, max_total_cost_usd=1.0, provider="openai")
        with pytest.raises(LLMRequestError) as caught:
            ask(config)
        assert caught.value.kind == "redirect" and f"HTTP {code}" in str(caught.value)
        assert [seen["method"] for seen in first.seen] == ["POST"]
        assert first.seen[0]["headers"]["Authorization"] == f"Bearer {KEY}"
        assert elsewhere.seen == [], "nothing, and so neither the key nor the code, reached the other address"

    @pytest.mark.parametrize("code", [301, 302, 303])
    def test_a_server_of_ones_own_may_redirect_but_the_key_and_the_code_stay_behind(self, servers, code):
        first, elsewhere = servers
        first.redirect_to, first.redirect_code = url_of(elsewhere, "/v1/chat/completions"), code
        config = LLMConfig(url_of(first), "m", api_key=KEY)
        with pytest.raises(RepoMindError, match="HTTP 404"):
            ask(config)
        assert first.seen[0]["headers"]["Authorization"] == f"Bearer {KEY}"
        [arrived] = elsewhere.seen
        assert arrived["method"] == "GET" and arrived["body"] == b""
        assert "Authorization" not in arrived["headers"]
        assert KEY not in json.dumps(arrived["headers"])

    @pytest.mark.parametrize("code", [307, 308])
    def test_a_redirect_that_would_resend_the_body_is_refused_for_a_server_of_ones_own_too(self, servers, code):
        first, elsewhere = servers
        first.redirect_to, first.redirect_code = url_of(elsewhere, "/v1/chat/completions"), code
        with pytest.raises(RepoMindError, match=f"HTTP {code}"):
            ask(LLMConfig(url_of(first), "m", api_key=KEY))
        assert elsewhere.seen == []


class TestOllamaAsBefore:
    @pytest.fixture
    def sent(self, monkeypatch):
        seen: list = []

        def urlopen(request, timeout=None):
            seen.append((request, timeout))
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"chosen": []}'}, "finish_reason": "stop"}],
                                          "usage": {"prompt_tokens": 10, "completion_tokens": 3}}).encode())

        monkeypatch.setattr(llm_module.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(llm_module, "_NO_REDIRECTS", None)
        return seen

    def test_the_local_model_gets_the_same_request_as_before(self, sent):
        local = LLMConfig("http://127.0.0.1:11535/v1", "qwen2.5:7b")
        validate_config(local)
        response = ask(local, max_output_tokens=120)
        [(request, timeout)] = sent
        assert request.full_url == "http://127.0.0.1:11535/v1/chat/completions" and timeout == 300
        body = json.loads(request.data)
        assert body == {
            "model": "qwen2.5:7b",
            "messages": [{"role": "system", "content": "Reply with one JSON object."}, {"role": "user", "content": "the question and passages"}],
            "max_tokens": 120,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        assert request.get_header("Authorization") is None
        assert response == LLMResponse('{"chosen": []}', 10, 3, "stop")

    def test_a_server_of_ones_own_gets_its_key_in_a_header_that_a_redirect_does_not_carry(self, sent):
        ask(LLMConfig("https://models.example.org/v1", "qwen2.5-coder:14b", api_key=KEY))
        [(request, _)] = sent
        assert request.unredirected_hdrs == {"Authorization": f"Bearer {KEY}"}
        assert "Authorization" not in request.headers

    def test_its_error_messages_are_as_before_but_never_hold_the_key(self, monkeypatch):
        def urlopen(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 500, "error", headers(), io.BytesIO(f"bad key {KEY}".encode()))

        monkeypatch.setattr(llm_module.urllib.request, "urlopen", urlopen)
        with pytest.raises(RepoMindError) as caught:
            ask(LLMConfig("https://models.example.org/v1", "m", api_key=KEY))
        assert str(caught.value) == "The LLM endpoint answered with HTTP 500: bad key <key>"
        with pytest.raises(RepoMindError, match="answered with HTTP 500: bad key"):
            ask(LLMConfig("http://127.0.0.1:11535/v1", "m"))

    def test_a_reply_that_is_not_json_is_an_error_and_no_longer_a_crash(self, monkeypatch):
        monkeypatch.setattr(llm_module.urllib.request, "urlopen", lambda request, timeout=None: io.BytesIO(b"<html>"))
        with pytest.raises(RepoMindError, match="unexpected response shape"):
            ask(LLMConfig("http://127.0.0.1:11535/v1", "m"))
