"""Tokenizer, BM25 and RRF, without a database."""

from __future__ import annotations

import pytest

from repomind.search.engine import asks_about_tests
from repomind.search.hybrid import reciprocal_rank_fusion
from repomind.search.keyword import BM25Config, KeywordIndex
from repomind.search.tokenizer import fold, tokenize
from repomind.search.translation import looks_turkish
from repomind.store.database import ChunkRow


def chunk(identifier: int, content: str, path: str = "src/a.ts", symbol: str | None = None, symbols=()) -> ChunkRow:
    return ChunkRow(
        id=identifier,
        repo="r",
        commit_sha="0" * 40,
        path=path,
        language="typescript",
        kind="function",
        symbol=symbol,
        parent=None,
        signature=None,
        symbols=list(symbols),
        start_line=1,
        end_line=1,
        part_index=1,
        part_total=1,
        is_test=False,
        content=content,
        content_hash="h",
        embed_text_hash="e",
    )


class TestTokenizer:
    def test_splits_camel_snake_and_acronyms(self):
        assert "reservationservic" not in tokenize("ReservationService")
        assert set(tokenize("cancelReservation")) >= {"cancel", "reserv", "cancelreservation"}
        assert set(tokenize("max_retry_count")) >= {"max", "retri", "count"}
        assert set(tokenize("HTTPServerError")) >= {"http", "server", "error"}

    def test_query_and_identifier_meet(self):
        assert set(tokenize("cancel reservations")) <= set(tokenize("cancelReservation"))

    def test_exact_identifier_is_a_token(self):
        assert "cancelreservation" in tokenize("cancelReservation")

    def test_removes_english_and_turkish_stopwords(self):
        assert tokenize("how is the cancel") == ["cancel"]
        assert tokenize("Rezervasyon nasıl ve nerede") == ["rezervasyon"]

    @pytest.mark.parametrize(
        ("word", "expected"),
        [("İPTAL", "iptal"), ("ışık", "isik"), ("Şifre", "sifre"), ("bağlantı", "baglanti"), ("ÖZEL", "ozel")],
    )
    def test_turkish_folding(self, word, expected):
        assert fold(word) == expected

    def test_empty_and_symbols_only(self):
        assert tokenize("") == []
        assert tokenize("!!! ??? {}") == []


class TestBM25:
    def test_ranks_matching_document_first(self):
        index = KeywordIndex([chunk(1, "stock level update"), chunk(2, "user password hash"), chunk(3, "render button")])
        assert [m.chunk.id for m in index.search("password", 5)] == [2]

    def test_no_match_returns_nothing(self):
        assert KeywordIndex([chunk(1, "alpha beta")]).search("gamma", 5) == []

    def test_rare_term_outweighs_common_term(self):
        docs = [chunk(i, "common filler text") for i in range(1, 11)]
        docs.append(chunk(11, "rarity"))
        docs.append(chunk(12, "common"))
        results = KeywordIndex(docs).search("rarity common", 5)
        assert results[0].chunk.id == 11

    def test_score_matches_hand_computed_bm25(self):
        import math

        config = BM25Config(k1=1.2, b=0.75)
        index = KeywordIndex([chunk(1, "alpha beta gamma delta", path="x.ts"), chunk(2, "epsilon zeta", path="y.ts")], config)
        idf = math.log(1 + (2 - 1 + 0.5) / (1 + 0.5))
        lengths = [4 + 1 * config.path_weight, 2 + 1 * config.path_weight]
        average = sum(lengths) / 2
        expected = idf * (1 * 2.2) / (1 + 1.2 * (1 - 0.75 + 0.75 * lengths[0] / average))
        assert index.search("alpha", 1)[0].score == pytest.approx(expected)

    def test_symbol_name_outweighs_body_mention(self):
        index = KeywordIndex(
            [
                chunk(1, "this code calls cancelOrder once among many other unrelated words here today"),
                chunk(2, "this code does many other unrelated things with several words here today", symbol="Orders.cancelOrder"),
            ]
        )
        assert index.search("cancelOrder", 2)[0].chunk.id == 2

    def test_path_contributes(self):
        index = KeywordIndex([chunk(1, "x = 1", path="src/billing.ts"), chunk(2, "y = 2", path="src/other.ts")])
        assert index.search("billing", 2)[0].chunk.id == 1

    def test_ties_break_deterministically(self):
        index = KeywordIndex([chunk(2, "same words"), chunk(1, "same words")])
        assert [m.chunk.id for m in index.search("same", 2)] == [1, 2]

    def test_matched_terms_explain_the_score(self):
        match = KeywordIndex([chunk(1, "cancel order")]).search("cancel order", 1)[0]
        assert match.score == pytest.approx(sum(match.matched_terms.values()))


class TestTurkishDetection:
    @pytest.mark.parametrize(
        "query",
        [
            "Rezervasyon iptali nerede yönetiliyor?",
            "CSRF koruması nasıl uygulanıyor?",
            "StockGuard nerede",
            "Şifre",
            "JWT neden kullanilir",
        ],
    )
    def test_turkish_queries_are_detected(self, query):
        assert looks_turkish(query)

    @pytest.mark.parametrize(
        "query",
        [
            "How is overselling prevented?",
            "Where are user permissions checked?",
            "cancelReservation",
            "ReservationService.cancel",
            "Idempotency-Key",
        ],
    )
    def test_english_queries_and_identifiers_are_left_alone(self, query):
        assert not looks_turkish(query)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Where are the integration tests?", True),
        ("testler nerede", True),
        ("concurrency test", True),
        ("e2e helpers", True),
        ("How does the latest contest work?", False),
        ("Where is overselling prevented?", False),
        ("Rezervasyon iptali nerede?", False),
    ],
)
def test_asks_about_tests(query, expected):
    assert asks_about_tests(query) is expected


class TestRRF:
    def test_hand_computed_scores(self):
        fused = reciprocal_rank_fusion({"a": ["x", "y"], "b": ["y", "z"]}, k=60)
        scores = {item.key: item.score for item in fused}
        assert scores["y"] == pytest.approx(1 / 62 + 1 / 61)
        assert scores["x"] == pytest.approx(1 / 61)
        assert scores["z"] == pytest.approx(1 / 62)
        assert [item.key for item in fused] == ["y", "x", "z"]

    def test_records_ranks_for_explanation(self):
        fused = reciprocal_rank_fusion({"keyword": ["x", "y"], "semantic": ["y"]})
        by_key = {item.key: item.ranks for item in fused}
        assert by_key["y"] == {"keyword": 2, "semantic": 1}
        assert by_key["x"] == {"keyword": 1}

    def test_uses_ranks_not_scores(self):
        assert [i.key for i in reciprocal_rank_fusion({"a": ["p", "q"]})] == ["p", "q"]

    def test_document_in_both_lists_beats_top_of_one(self):
        fused = reciprocal_rank_fusion({"a": ["only-a", "both"], "b": ["only-b", "both"]})
        assert fused[0].key == "both"

    def test_weight_zero_removes_a_list(self):
        fused = reciprocal_rank_fusion({"a": ["x"], "b": ["y"]}, weights={"a": 0.0})
        assert fused[0].key == "y"
        assert fused[0].score > fused[1].score

    def test_empty_input(self):
        assert reciprocal_rank_fusion({"a": [], "b": []}) == []

    def test_tie_is_deterministic(self):
        first = reciprocal_rank_fusion({"a": ["x"], "b": ["y"]})
        second = reciprocal_rank_fusion({"b": ["y"], "a": ["x"]})
        assert [i.key for i in first] == [i.key for i in second] == ["x", "y"]
