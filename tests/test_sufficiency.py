"""Refusing questions about things the code never mentions."""

from __future__ import annotations

import pytest

from repomind.indexing import index_repository
from repomind.qa.engine import QAEngine
from repomind.qa.sufficiency import check_sufficiency
from repomind.search.engine import SearchEngine
from repomind.search.tokenizer import tokenize
from tests.test_qa import GOOD_REPLY, LOCAL, FakeClient, only_about_order_service

CODE = """
class Reservation { void cancel() { stockLedger.release(); log("connection lost"); } }
// StockGuard handles JWT tokens, PostgreSQL and WebSocket sessions, and rejects stale versions.
"""
VOCABULARY = set(tokenize(CODE))


@pytest.mark.parametrize(
    "question",
    [
        "How does the service use Kafka?",
        "Where is the GraphQL schema?",
        "How are files uploaded to S3?",
        "How does StockGuard call Stripe?",
    ],
)
def test_an_unknown_name_refuses(question):
    result = check_sufficiency(question, None, VOCABULARY)
    assert result.insufficient
    assert result.missing_names


def test_two_unknown_ordinary_words_refuse():
    result = check_sufficiency("How are reservation emails delivered?", None, VOCABULARY)
    assert result.insufficient
    assert set(result.missing_words) == {"emails", "delivered"}


def test_a_single_unknown_ordinary_word_only_warns():
    result = check_sufficiency("How is cancel prevented?", None, VOCABULARY)
    assert not result.insufficient
    assert result.missing_words == ("prevented",)
    assert "prevented" in result.message


def test_known_question_passes_without_a_message():
    result = check_sufficiency("How does StockGuard cancel a reservation?", None, VOCABULARY)
    assert not result.insufficient
    assert result.message is None


def test_known_names_pass():
    assert not check_sufficiency("How does PostgreSQL handle JWT tokens in WebSocket sessions?", None, VOCABULARY).insufficient


def test_capital_at_sentence_start_is_not_a_name():
    assert check_sufficiency("Frontend cancel", None, VOCABULARY).missing_names == ()


def test_compound_is_only_known_as_a_whole():
    vocabulary = VOCABULARY | {"graph"}
    assert check_sufficiency("Where is the GraphQL schema?", None, vocabulary).missing_names == ("GraphQL",)


def test_turkish_question_is_compared_through_its_translation():
    result = check_sufficiency("Rezervasyon iptali nerede?", "Where is reservation cancel?", VOCABULARY)
    assert not result.insufficient


def test_words_seen_only_in_a_translation_warn_but_never_refuse():
    """Regression: translation noise such as "retrialed" refused answerable questions."""
    result = check_sufficiency("Kilit zaman aşımında işlem nasıl tekrar ediliyor?", "How is the process retrialed?", VOCABULARY)
    assert not result.insufficient
    assert {"process", "retrialed"} <= set(result.missing_words)
    assert "retrialed" in result.message


def test_names_in_a_turkish_question_are_still_checked():
    result = check_sufficiency("StockGuard Kafka mesajlarını nasıl tüketiyor?", "How does StockGuard run out?", VOCABULARY)
    assert result.missing_names == ("Kafka",)
    assert result.insufficient


def test_turkish_words_do_not_refuse_when_translation_is_unavailable():
    result = check_sufficiency("Rezervasyon iptali nerede yönetiliyor?", None, VOCABULARY)
    assert not result.insufficient
    assert result.missing_words == ()


def test_contractions_do_not_count():
    result = check_sufficiency("Why doesn't cancel work?", None, VOCABULARY | {"work"})
    assert result.missing_words == ()


@pytest.mark.db
class TestInEngine:
    @pytest.fixture
    def search(self, store, sample_repo, fake_embedder):
        index_repository(store, str(sample_repo), fake_embedder, name="shop")
        return SearchEngine(store, fake_embedder)

    def test_refusal_never_calls_the_model(self, store, search):
        client = FakeClient(GOOD_REPLY)
        result = QAEngine(store, search, LOCAL, client).ask("How does cancelOrder publish to Kafka?", ["shop"])
        assert result.status == "insufficient"
        assert client.calls == []
        assert result.answer == ""
        assert "Kafka" in result.warnings[0]
        assert result.hits, "the closest code is still shown"

    def test_refusal_applies_without_a_model_too(self, store, search):
        result = QAEngine(store, search, None, None).ask("How does cancelOrder publish to Kafka?", ["shop"])
        assert result.status == "insufficient"

    def test_answerable_question_reaches_the_model(self, store, search):
        client = FakeClient(only_about_order_service)
        result = QAEngine(store, search, LOCAL, client).ask("How does cancelOrder release stock?", ["shop"])
        assert result.status == "answered"
        assert client.calls
