"""The changes that keep right lines from being lost before the model, or refused after it.

The repository here is a small library of books, made for these tests and used nowhere else.
Every model is a stand-in with fixed replies.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from repomind.indexing import index_repository
from repomind.qa import trace as stages
from repomind.qa.engine import AskResult, QAEngine
from repomind.qa.llm import LLMResponse
from repomind.qa.passages import Passage, is_evidence, passage_terms
from repomind.qa.prompt import TRANSLATION_PROMPT
from repomind.qa.sufficiency import check_sufficiency
from repomind.search.engine import SearchConfig, SearchEngine
from repomind.search.tokenizer import tokenize
from repomind.search.translation import plain_sentence
from tests.conftest import commit_all, git, write
from tests.test_qa import LOCAL

LOANS = """package lib;

public class LoanService {
    static final int MAX_LOANS = 5;

    /** Lends a book to a member. */
    public Loan lend(Member member, Book book) {
        if (member.openLoans() >= MAX_LOANS) {
            throw new LimitReached();
        }
        return ledger.open(member, book);
    }

    /** A member gives a book back. */
    public void giveBack(Loan loan) {
        ledger.close(loan);
    }
}
"""
FINES = """package lib;

public class FineCalculator {
    public int amount(int lateDays) {
        return lateDays * rate;
    }
}
"""


def chunk(path: str, text: str, symbol: str | None = None, start: int = 1):
    return SimpleNamespace(id=hash((path, start)), repo="lib", commit_sha="c1", path=path, language="java", symbol=symbol,
                           start_line=start, end_line=start + text.count("\n"), content=text)


def passage(path: str, text: str, symbol: str | None = None, start: int = 1) -> Passage:
    owner = chunk(path, text, symbol, start)
    return Passage("P1", owner, owner.start_line, owner.end_line, text)


class TestWhereLinesAreCountsAsWhatTheyAreAbout:
    def test_the_file_name_and_the_symbol_are_words_of_a_passage(self):
        lines = passage("src/main/java/lib/FineCalculator.java", "        return lateDays * rate;", "FineCalculator.amount")
        assert {"fine", "amount", "late"} <= passage_terms(lines)
        assert is_evidence(lines, "How is the fine amount worked out?"), "fine and amount are where the line is, not on it"

    def test_folders_are_not(self):
        lines = passage("src/main/fine/Other.java", "        return 1;", None)
        assert not {"main", "fine", "src"} & passage_terms(lines)
        assert not is_evidence(lines, "What is the main fine?")

    def test_one_shared_word_is_still_not_enough(self):
        lines = passage("src/main/java/lib/FineCalculator.java", "        return lateDays * rate;", "FineCalculator.amount")
        assert not is_evidence(lines, "How is a fine refunded to a credit card?")


VOCABULARY = set(tokenize("exposure show created commit health details endpoint index version percentile latency request book member"))


class TestWordsTheCodeWritesInAnotherForm:
    @pytest.mark.parametrize(
        ("question", "missing"),
        [
            ("Which endpoints are exposed, and are health details shown?", ()),
            ("Which index is used by creation?", ("used",)),
            ("How are comments stored?", ("comments", "stored")),
        ],
    )
    def test_a_word_of_the_same_family_is_known_and_a_word_that_only_starts_alike_is_not(self, question, missing):
        assert check_sufficiency(question, None, VOCABULARY).missing_words == missing

    def test_a_short_word_is_never_matched_by_its_start(self):
        assert check_sufficiency("How is tax shown?", None, VOCABULARY | {"taxonomi"}).missing_words == ("tax",)

    @pytest.mark.parametrize("word", ["95th", "1st", "22nd", "3rd", "100"])
    def test_a_number_is_neither_a_name_nor_a_missing_word(self, word):
        result = check_sufficiency(f"What is the {word} percentile of request latency?", None, VOCABULARY)
        assert result.missing_names == () and word not in result.missing_words and not result.insufficient

    @pytest.mark.parametrize("name", ["S3", "OAuth2", "k8s"])
    def test_a_name_with_a_digit_in_it_still_refuses(self, name):
        assert check_sufficiency(f"How does the index use {name}?", None, VOCABULARY).missing_names == (name,)


class Model:
    """Translates as told, and then chooses the passage whose text holds a marker, or nothing."""

    def __init__(self, english: str | None, choose: str | None = None) -> None:
        self.english, self.choose, self.choices = english, choose, 0

    def complete(self, system, user, max_output_tokens):
        import re

        if system == TRANSLATION_PROMPT:
            return LLMResponse(json.dumps({"english": self.english}), 10, 5)
        self.choices += 1
        found = re.search(rf'id="(P\d+)"[^>]*>[^<]*{re.escape(self.choose)}', user) if self.choose else None
        return LLMResponse(json.dumps({"chosen": [found.group(1)] if found else [], "note": ""}), 100, 10)


class Translator:
    def __init__(self, english: str) -> None:
        self.english = english

    def translate(self, text: str) -> str:
        return self.english


@pytest.fixture
def library(store, tmp_path, fake_embedder):
    root = tmp_path / "lib"
    root.mkdir()
    git(root, "init", "-b", "main")
    write(root, "src/main/java/lib/LoanService.java", LOANS)
    write(root, "src/main/java/lib/FineCalculator.java", FINES)
    commit_all(root, "library")
    index_repository(store, str(root), fake_embedder, name="lib")
    return store


TURKISH = "Üye kitabı geri verince ne oluyor?"
RIGHT = "What happens when a member gives a book back?"
WRONG = "What happens when a contributor forks the repository?"


@pytest.mark.db
class TestATranslationThatChangesTheSubject:
    def ask(self, store, fake_embedder, model, second):
        search = SearchEngine(store, fake_embedder, SearchConfig(), Translator(second))
        return QAEngine(store, search, LOCAL, model).ask(TURKISH, ["lib"])

    def test_the_second_wording_keeps_right_lines_as_evidence(self, library, fake_embedder):
        model = Model(WRONG, choose="ledger.close")
        result = self.ask(library, fake_embedder, model, RIGHT)
        assert model.choices == 1, f"the question has to reach the model: {result.stopped_by} {result.warnings}"
        assert result.status == "answered"
        assert [c.path.rsplit("/", 1)[-1] for c in result.citations] == ["LoanService.java"]
        assert any("fork" in warning for warning in result.warnings), "the words that are nowhere in the code are still reported"

    def test_two_wordings_that_are_both_wrong_are_still_refused_before_the_model(self, library, fake_embedder):
        model = Model(WRONG, choose="ledger.close")
        result = self.ask(library, fake_embedder, model, "What happens when a contributor forks it?")
        assert model.choices == 0
        assert result.status == "insufficient" and result.stopped_by == "no passage passes the evidence rule"

    def test_the_words_of_two_wordings_are_never_added_together(self, library, fake_embedder):
        # One word of the lines from each wording. Together they would be two, and neither wording says enough alone.
        model = Model("What does a contributor do with a book?", choose="ledger.close")
        result = self.ask(library, fake_embedder, model, "What does a contributor fork for a member?")
        closing = [p for p in result.passages if "ledger.close" in p.text]
        assert closing, "the lines are found either way"
        assert model.choices == 0 or not any("ledger.close" in item.passage.text for item in result.evidence)

    @pytest.mark.parametrize(
        ("written", "read"),
        [
            ("What-happens-to-the-version-records-when-a-note-is-deleted?", "What happens to the version records when a note is deleted?"),
            ("Which_of_the_stock_counters_change_when_a Reservation_is_Confirmed?", "Which of the stock counters change when a Reservation is Confirmed?"),
            ("Where is the token-ttl of STOCKGUARD_DB_HOST_PORT read?", "Where is the token-ttl of STOCKGUARD_DB_HOST_PORT read?"),
            ("How is a note deleted?", "How is a note deleted?"),
        ],
    )
    def test_a_sentence_written_with_joiners_is_read_as_a_sentence_and_an_identifier_in_one_is_kept(self, written, read):
        assert plain_sentence(written) == read

    def test_a_joined_translation_is_searched_as_words(self, library, fake_embedder):
        model = Model("What-happens-when-a-member-gives-a-book-back?", choose="ledger.close")
        result = self.ask(library, fake_embedder, model, WRONG)
        assert result.translated_query == "What happens when a member gives a book back?"
        assert result.status == "answered"



def answer(**fields) -> AskResult:
    return AskResult(**{"status": "insufficient", "question": "q", **fields})


class TestWhereTheLinesWereLost:
    WANTED = [stages.Lines("src/A.java", 10, 12)]
    RIGHT = passage("src/A.java", "a\nb\nc", start=10)
    OTHER = passage("src/B.java", "x\ny", start=1)

    @staticmethod
    def indexed(path):
        return [chunk("src/A.java", "a\nb\nc", start=10)] if path == "src/A.java" else []

    def stage(self, result, wanted=None, indexed=None) -> stages.PlaceTrace:
        return stages.trace(result, [wanted or self.WANTED], indexed or self.indexed)[0]

    def hit(self, rank=4):
        return [SimpleNamespace(rank=rank, chunk=self.RIGHT.chunk)]

    def test_each_stage_is_told_apart(self):
        missing = [stages.Lines("src/Gone.java", 1, 2)]
        assert self.stage(answer(), missing).stage == stages.NOT_INDEXED
        assert self.stage(answer(hits=[SimpleNamespace(rank=1, chunk=self.OTHER.chunk)])).stage == stages.NOT_RETRIEVED
        assert self.stage(answer(hits=self.hit(), passages=[self.OTHER], sent=[self.OTHER])).stage == stages.NOT_A_PASSAGE
        refused = self.stage(answer(hits=self.hit(), passages=[self.RIGHT], stopped_by="no passage passes the evidence rule"))
        assert (refused.stage, refused.refused_because) == (stages.NOT_ASKED, "no passage passes the evidence rule")
        assert self.stage(answer(hits=self.hit(), passages=[self.RIGHT, self.OTHER], sent=[self.OTHER])).stage == stages.NOT_SENT
        assert self.stage(answer(hits=self.hit(), passages=[self.RIGHT], sent=[self.RIGHT])).stage == stages.NOT_CHOSEN
        turned_down = answer(hits=self.hit(), passages=[self.RIGHT], sent=[self.RIGHT], review_candidates=[self.RIGHT])
        assert self.stage(turned_down).stage == stages.TURNED_DOWN
        shown = answer(status="answered", hits=self.hit(), passages=[self.RIGHT], sent=[self.RIGHT], evidence=[SimpleNamespace(passage=self.RIGHT)])
        assert self.stage(shown).stage == stages.ANSWERED and not self.stage(shown).lost
        assert self.stage(answer(status="no_llm", hits=self.hit(), passages=[self.RIGHT])).stage == stages.WAITING

    def test_the_rank_search_gave_the_chunk_is_kept_and_any_accepted_place_will_do(self):
        either = [stages.Lines("src/Nowhere.java", 1, 1), stages.Lines("src/A.java", 12, 30)]
        traced = self.stage(answer(hits=self.hit(7), passages=[self.RIGHT], sent=[self.RIGHT]), either)
        assert (traced.stage, traced.retrieval_rank) == (stages.NOT_CHOSEN, 7)

    def test_lines_are_read_from_text(self):
        assert stages.Lines.parse("src/A.java:10-12") == stages.Lines("src/A.java", 10, 12)
        assert stages.Lines.parse("C:/x/A.java:7") == stages.Lines("C:/x/A.java", 7, 7)
        with pytest.raises(ValueError):
            stages.Lines.parse("src/A.java")

    @pytest.mark.db
    def test_a_real_question_is_traced_from_what_its_answer_holds(self, library, fake_embedder):
        search = SearchEngine(library, fake_embedder)
        snapshot = library.active_snapshots(["lib"])[0]
        wanted = [[stages.Lines("src/main/java/lib/LoanService.java", 15, 17)]]
        indexed = lambda path: library.file_chunks(snapshot.id, path)  # noqa: E731
        chosen = QAEngine(library, search, LOCAL, Model(None, choose="ledger.close")).ask(RIGHT, ["lib"])
        assert [t.stage for t in stages.trace(chosen, wanted, indexed)] == [stages.ANSWERED]
        declined = QAEngine(library, search, LOCAL, Model(None)).ask(RIGHT, ["lib"])
        assert [t.stage for t in stages.trace(declined, wanted, indexed)] == [stages.NOT_CHOSEN]
        assert chosen.sent and declined.sent and declined.stopped_by is None
        refused = QAEngine(library, search, LOCAL, Model(None)).ask("How are barcodes printed on invoices?", ["lib"])
        assert refused.stopped_by == "words of the question are nowhere in the code" and not refused.sent
        assert "place 1: " + stages.ANSWERED in stages.describe(stages.trace(chosen, wanted, indexed))[0]
