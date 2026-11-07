"""A question naming its own repository: what leaves the search, what stays, and what must not change."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from repomind.indexing import index_repository
from repomind.qa.engine import QAEngine
from repomind.qa.llm import LLMConfig, LLMResponse
from repomind.qa.passages import MAX_PASSAGES, Passage, is_evidence
from repomind.qa.project_name import (
    Wording,
    counts_with_name,
    names_the_project,
    stands_alone,
    with_name_seat,
    without_project_name,
)
from repomind.qa.prompt import TRANSLATION_PROMPT
from repomind.search.engine import SearchEngine
from tests.conftest import commit_all, git, write
from tests.test_qa import chunk

# The rule is about which lines are offered, so these tests use the plainer way of choosing among them.
LOCAL = LLMConfig(base_url="http://127.0.0.1:1/v1", model="fake", selection="choose")
LEFT_OUT = "name was left out"
NOTHING = '{"chosen": [], "note": ""}'


class TestWhatLeavesTheSearch:
    @pytest.mark.parametrize(
        ("question", "left"),
        [
            ("How does ShopGuard prevent negative stock?", "How does prevent negative stock?"),
            ("shopguard stok miktarı nasıl korunuyor?", "stok miktarı nasıl korunuyor?"),
            ("ShopGuard'ın stok miktarı nasıl korunuyor?", "stok miktarı nasıl korunuyor?"),
            ("How is stock kept consistent in ShopGuard?", "How is stock kept consistent in ?"),
            ("Is ShopGuard's stock kept consistent?", "Is stock kept consistent?"),
        ],
    )
    def test_a_name_standing_alone_leaves(self, question, left):
        assert without_project_name("shopguard", Wording(question)).question == left

    @pytest.mark.parametrize(
        "question",
        [
            "What does the ShopGuardProperties class hold?",
            "Where are the shopguard.security settings?",
            "Where is SHOPGUARD_JWT_SECRET used?",
            "What is in the com.shopguard package?",
            "How is the shopguard-db container started?",
            "What is in the src/shopguard/config folder?",
            "Where is the `shopguard` database user defined?",
            'Where is the "shopguard" database user defined?',
            "Where is the “shopguard” database user defined?",
            "What does shopguard:security hold?",
        ],
    )
    def test_a_name_inside_an_identifier_key_path_or_quotes_stays(self, question):
        assert without_project_name("shopguard", Wording(question)) is None

    @pytest.mark.parametrize("question", ["What is ShopGuard?", "ShopGuard nedir?", "ShopGuard?", "Explain ShopGuard"])
    def test_with_too_little_left_the_name_is_the_topic(self, question):
        assert without_project_name("shopguard", Wording(question)) is None

    def test_named_in_two_forms_the_name_stays(self):
        assert without_project_name("shopguard", Wording("How does ShopGuard use ShopGuardProperties at startup?")) is None

    def test_another_project_is_not_this_one(self):
        assert without_project_name("shopguard", Wording("How does SyncNotes store notes on disk?")) is None

    def test_nothing_is_specific_to_one_repository(self):
        assert without_project_name("expressjs/cors", Wording("How does cors handle preflight requests?")).question == "How does handle preflight requests?"
        assert without_project_name("expressjs/cors", Wording("What does expressjs/cors export by default?")) is None

    def test_a_name_with_special_characters_is_only_text(self):
        assert without_project_name("a+b", Wording("How does a+b store user notes?")).question == "How does store user notes?"
        assert without_project_name("a+b", Wording("How does aab store user notes?")) is None

    def test_every_wording_loses_it_and_the_question_alone_decides(self):
        searched = without_project_name("shopguard", Wording("ShopGuard stok nasıl korunuyor?", "How does ShopGuard protect stock?", "ShopGuard protects stock"))
        assert searched == Wording("stok nasıl korunuyor?", "How does protect stock?", "protects stock")
        # A translator breaking a protected name apart cannot take it out of the search.
        assert without_project_name("shopguard", Wording("ShopGuardProperties ne tutuyor?", "What does the ShopGuard class hold?")) is None

    def test_a_translation_left_with_too_little_keeps_the_name(self):
        assert without_project_name("shopguard", Wording("ShopGuard stok koruması nedir?", "What is ShopGuard?")) is None

    def test_no_translation_stays_none(self):
        assert without_project_name("shopguard", Wording("How does ShopGuard protect stock?")).translated is None

    def test_stands_alone_says_the_same(self):
        assert stands_alone("shopguard", "How does ShopGuard protect stock?")
        assert not stands_alone("shopguard", "What does ShopGuardProperties hold?")


class TestLinesThatNameTheProject:
    @pytest.mark.parametrize(
        "text",
        ["name: shopguard", "public record ShopGuardProperties(", "SHOPGUARD_DB_URL: x", '@ConfigurationProperties("shopguard")', "shopguard_test"],
    )
    def test_these_name_it(self, text):
        assert names_the_project("shopguard", text)

    @pytest.mark.parametrize("text", ["import com.shopguard.common.Page;", "package com.shopguard;", "src/shopguard/a.ts", "${DB:-shopguard}", "public record Other("])
    def test_these_do_not(self, text):
        assert not names_the_project("shopguard", text)


def passage(text: str, identifier: int = 1, number: str = "P1", start: int = 1) -> Passage:
    return Passage(number, chunk(content=text, start=start, end=start + text.count("\n"), identifier=identifier), start, start + text.count("\n"), text)


class TestEvidenceIsNotWeakened:
    def test_the_name_counts_once_and_still_needs_another_word(self):
        wording = "What is the compose project name?"
        asked = "What is the compose project name of ShopGuard?"
        assert counts_with_name("shopguard", passage("name: shopguard"), wording, asked)

    def test_the_name_alone_is_not_enough(self):
        assert not counts_with_name("shopguard", passage("record ShopGuardProperties(String id)"), "How is stock protected?", "How is ShopGuard stock protected?")

    def test_another_word_without_the_name_is_not_enough(self):
        assert not counts_with_name("shopguard", passage("long stockTotal = 1;"), "How is stock protected?", "How is ShopGuard stock protected?")

    def test_a_package_line_does_not_count_as_naming_it(self):
        assert not counts_with_name("shopguard", passage("import com.shopguard.stock.Level;"), "How is stock protected?", "How is ShopGuard stock protected?")

    @pytest.mark.parametrize(
        ("text", "wording", "asked"),
        [
            # Typed in lower case, the question holds no "shop" or "guard" for ShopGuardProperties to share.
            ("record ShopGuardProperties(String stock)", "How is stock protected?", "How is shopguard stock protected?"),
            ("name: shopguard", "How is stock protected?", "How is shopguard stock protected?"),
            ("shopguard: stock", "How is stock protected?", "How is ShopGuard stock protected?"),
            ("long stockTotal = 1;", "How is stock protected?", "How is ShopGuard stock protected?"),
        ],
    )
    def test_it_never_accepts_what_the_rule_turns_down_for_the_question_as_asked(self, text, wording, asked):
        lines = passage(text)
        if counts_with_name("shopguard", lines, wording, asked):
            assert is_evidence(lines, asked)
        if is_evidence(lines, wording):
            assert is_evidence(lines, asked)


class TestTheSeat:
    def _five(self) -> list[Passage]:
        return [passage(f"long stock{i} = {i};", identifier=i, number=f"P{i}") for i in range(1, MAX_PASSAGES + 1)]

    def test_it_goes_last_and_the_order_of_the_others_stays(self):
        five = self._five()
        seat = passage("name: shopguard stock", identifier=9, number="P1")
        result = with_name_seat("shopguard", [seat], five, "stock name", "stock name of ShopGuard")
        assert [p.text for p in result] == [*(p.text for p in five[:4]), seat.text]

    def test_ids_follow_the_position_and_stay_distinct(self):
        seat = passage("name: shopguard stock", identifier=9, number="P1")
        result = with_name_seat("shopguard", [seat], self._five(), "stock name", "stock name of ShopGuard")
        assert [p.id for p in result] == ["P1", "P2", "P3", "P4", "P5"]

    def test_fewer_passages_are_all_kept(self):
        two = self._five()[:2]
        seat = passage("name: shopguard stock", identifier=9, number="P1")
        assert len(with_name_seat("shopguard", [seat], two, "stock name", "stock name of ShopGuard")) == 3

    def test_lines_already_offered_do_not_take_a_seat_and_the_next_one_can(self):
        offered = passage("name: shopguard stock", identifier=9, number="P1")
        other = passage("ShopGuardProperties stock name", identifier=8, number="P2")
        result = with_name_seat("shopguard", [offered, other], [offered], "stock name", "stock name of ShopGuard")
        assert [p.text for p in result] == [offered.text, other.text]

    def test_nothing_naming_the_project_changes_nothing(self):
        five = self._five()
        assert with_name_seat("shopguard", [passage("long stock = 1;", identifier=9)], five, "stock name", "stock name of ShopGuard") == five


@dataclass
class Model:
    english: str | None = None
    reply: object = NOTHING
    calls: list[dict] = field(default_factory=list)

    def complete(self, system, user, max_output_tokens):
        self.calls.append({"system": system, "user": user})
        if system == TRANSLATION_PROMPT:
            return LLMResponse(json.dumps({"english": self.english}), 10, 5)
        return LLMResponse(self.reply(user) if callable(self.reply) else self.reply, 10, 5)


def choosing(path_end: str):
    def reply(user: str) -> str:
        ids = re.findall(r'id="(P\d+)" path="([^"]+)"', user)
        return json.dumps({"chosen": [next(i for i, path in ids if path.endswith(path_end))], "note": ""})

    return reply


@pytest.fixture
def indexed(store, tmp_path: Path, fake_embedder):
    root = tmp_path / "shopguard"
    root.mkdir()
    git(root, "init", "-b", "main")
    write(root, "compose.yaml", "name: shopguard\nservices:\n  db:\n    image: postgres\n")
    write(root, "src/main/java/shopguard/config/ShopGuardProperties.java", '@ConfigurationProperties("shopguard")\npublic record ShopGuardProperties(String instanceId) {\n}\n')
    write(root, "src/main/java/shopguard/ShopGuardApplication.java", "public class ShopGuardApplication {\n  public static void main(String[] args) {\n    SpringApplication.run(ShopGuardApplication.class, args);\n  }\n}\n")
    write(root, "db/V1__stock.sql", "CREATE TABLE stock_levels (\n  available_quantity INTEGER NOT NULL,\n  -- The database refuses negative stock.\n  CONSTRAINT ck_stock_non_negative CHECK (available_quantity >= 0)\n);\n")
    write(root, "src/main/java/shopguard/report/Report.java", "public class Report {\n  long negativeCounters(long stock) {\n    return stock < 0 ? 1 : 0;\n  }\n}\n")
    commit_all(root, "first")
    index_repository(store, str(root), fake_embedder, name="shopguard")
    return store


@pytest.fixture
def search(indexed, fake_embedder):
    return SearchEngine(indexed, fake_embedder)


def spots(result) -> list[tuple[str, int, int]]:
    return [(p.path, p.start_line, p.end_line) for p in result.passages]


class TestInAsk:
    QUESTION = "How does ShopGuard prevent negative stock quantities?"

    def test_a_file_is_not_offered_for_being_named_after_the_project(self, indexed, search):
        result = QAEngine(indexed, search, LOCAL, Model()).ask(self.QUESTION, ["shopguard"])
        assert any(LEFT_OUT in warning for warning in result.warnings)
        assert not [p for p in result.passages if "ShopGuardProperties" in p.path]
        assert any(p.path.endswith("V1__stock.sql") for p in result.passages)

    def test_the_model_still_reads_the_whole_question(self, indexed, search):
        model = Model()
        QAEngine(indexed, search, LOCAL, model).ask(self.QUESTION, ["shopguard"])
        assert f"Question: {self.QUESTION}" in model.calls[-1]["user"]

    def test_a_turkish_question_keeps_its_name_for_the_model_and_the_page(self, indexed, search):
        question = "ShopGuard stok miktarının negatife düşmesini nasıl engelliyor?"
        english = "How does ShopGuard prevent stock quantities from going negative?"
        model = Model(english=english)
        result = QAEngine(indexed, search, LOCAL, model).ask(question, ["shopguard"])
        assert question in model.calls[-1]["user"]
        assert f"(In English: {english})" in model.calls[-1]["user"]
        assert result.translated_query == english

    def test_a_question_really_about_the_name_keeps_its_line(self, indexed, search):
        model = Model(reply=choosing("compose.yaml"))
        result = QAEngine(indexed, search, LOCAL, model).ask("What is the compose project name of ShopGuard?", ["shopguard"])
        assert any(LEFT_OUT in warning for warning in result.warnings)
        assert result.status == "answered"
        assert result.citations[0].path == "compose.yaml"
        assert result.citations[0].start_line == 1

    def test_lines_found_only_by_the_name_cannot_answer(self, indexed, search):
        def picks_the_first(user):
            return json.dumps({"chosen": [re.findall(r'id="(P\d+)"', user)[0]], "note": ""})

        result = QAEngine(indexed, search, LOCAL, Model(reply=picks_the_first)).ask("How does ShopGuard reject negative stock?", ["shopguard"])
        for item in result.evidence:
            assert "ShopGuardProperties" not in item.passage.path

    def test_refusal_reads_the_question_as_asked(self, indexed, search):
        # Everything but Kafka is in the code. Without the name in front, Kafka is the first word,
        # no longer looks like a proper noun, and the question would go on to the model.
        model = Model(english="How does ShopGuard protect negative stock quantities with Kafka?")
        result = QAEngine(indexed, search, LOCAL, model).ask("ShopGuard Kafka ile negatif stok miktarını nasıl koruyor?", ["shopguard"])
        assert result.status == "insufficient"
        assert any("Kafka" in warning for warning in result.warnings)
        assert [call["system"] for call in model.calls] == [TRANSLATION_PROMPT], "only the translation, and no choice"

    def test_it_also_works_with_no_model(self, indexed, search):
        result = QAEngine(indexed, search, None, None).ask(self.QUESTION, ["shopguard"])
        assert result.status == "no_llm"
        assert any(LEFT_OUT in warning for warning in result.warnings)
        assert not [p for p in result.passages if "ShopGuardProperties" in p.path]

    @pytest.mark.parametrize(
        "question",
        [
            "What does the ShopGuardProperties class hold?",
            "Where is the `shopguard` project name?",
            "What is ShopGuard?",
            "How is negative stock refused?",
        ],
    )
    def test_any_other_question_is_searched_exactly_as_before(self, indexed, search, question):
        kept = QAEngine(indexed, search, LOCAL, Model()).ask(question, ["shopguard"])
        # With no single repository given, the rule cannot apply, which shows what the old path gives.
        before = QAEngine(indexed, search, LOCAL, Model()).ask(question, None)
        assert not any(LEFT_OUT in warning for warning in kept.warnings)
        assert spots(kept) == spots(before)
        assert kept.translated_query == before.translated_query

    def test_with_no_single_repository_the_name_stays(self, indexed, search):
        result = QAEngine(indexed, search, LOCAL, Model()).ask(self.QUESTION, None)
        assert not any(LEFT_OUT in warning for warning in result.warnings)

    def test_passage_ids_are_distinct_for_the_model(self, indexed, search):
        model = Model()
        result = QAEngine(indexed, search, LOCAL, model).ask("Where does the ShopGuard application start?", ["shopguard"])
        ids = [p.id for p in result.passages]
        assert ids == [f"P{number}" for number in range(1, len(ids) + 1)]
        assert re.findall(r'id="(P\d+)"', model.calls[-1]["user"]) == ids
