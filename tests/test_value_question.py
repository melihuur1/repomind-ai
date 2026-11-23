"""A question about a value, answered by a constant's declaration that the model chose on its own.

From the second real request on 2026-11-22: "Bir üye aynı anda en fazla kaç kitap ödünç alabilir?" The model
chose `export const MAX_ACTIVE_LOANS = 3;`, which is right. That line shares no word with the question
("maximum" is not `max`, "borrow" is not `loan`), so the evidence rule turned it down and the reader was told
"I do not know". The line that uses the constant, the limit check in `lendBook`, shares "book" and "member"
and passes the rule on its own, and it was offered too. No model is asked here.
"""

from __future__ import annotations

import json
import re

import pytest

from types import SimpleNamespace

from repomind.api.routes import _sources
from repomind.indexing import index_repository
from repomind.qa import engine as engine_module
from repomind.qa.completion import MAX_COMPLETIONS, Completion, uses_to_review
from repomind.qa.declarations import Declaration
from repomind.qa.engine import QAEngine
from repomind.qa.llm import LLMConfig, LLMResponse
from repomind.qa.passages import Passage
from repomind.search.engine import SearchEngine
from tests.conftest import commit_all, git, write

QUESTION = "Bir üye aynı anda en fazla kaç kitap ödünç alabilir?"
ENGLISH = "What is the maximum number of books a member can borrow at one time?"
DECLARATION = "export const MAX_ACTIVE_LOANS = 3;"
CHECK = "if (member.activeLoans.length >= MAX_ACTIVE_LOANS) throw new Error('loan limit reached');"
LOCAL = LLMConfig("http://127.0.0.1:1/v1", "stand-in")

FILES = {
    "src/loans.ts": (
        "import { lateFee } from './fees';\n"
        "import { isSuspended, type Member } from './members';\n"
        "\n"
        f"{DECLARATION}\n"
        "export const LOAN_DAYS = 14;\n"
        "\n"
        "export interface Book {\n"
        "  isbn: string;\n"
        "  title: string;\n"
        "  available: boolean;\n"
        "}\n"
        "\n"
        "/** Lends a book: the member must not be suspended or at the loan limit, and the book must be on the shelf. */\n"
        "export function lendBook(member: Member, book: Book): Date {\n"
        "  if (isSuspended(member)) throw new Error('member is suspended');\n"
        f"  {CHECK}\n"
        "  if (!book.available) throw new Error('book is not available');\n"
        "  book.available = false;\n"
        "  member.activeLoans.push(book.isbn);\n"
        "  const due = new Date();\n"
        "  due.setDate(due.getDate() + LOAN_DAYS);\n"
        "  return due;\n"
        "}\n"
        "\n"
        "/** Takes a book back, and adds a late fee when it comes back after its due date. */\n"
        "export function returnBook(member: Member, book: Book, daysLate: number): number {\n"
        "  member.activeLoans = member.activeLoans.filter((isbn) => isbn !== book.isbn);\n"
        "  book.available = true;\n"
        "  const fee = lateFee(daysLate);\n"
        "  member.unpaidFees += fee;\n"
        "  return fee;\n"
        "}\n"
    ),
    "src/members.ts": (
        "export interface Member {\n  id: string;\n  name: string;\n  unpaidFees: number;\n  activeLoans: string[];\n}\n\n"
        "export const SUSPENSION_LIMIT = 5;\n\n"
        "/** A member who owes more than the limit may not borrow until the fees are paid. */\n"
        "export function isSuspended(member: Member): boolean {\n  return member.unpaidFees > SUSPENSION_LIMIT;\n}\n"
    ),
    "src/fees.ts": (
        "export const DAILY_LATE_FEE = 0.5;\nexport const MAX_LATE_FEE = 10;\n\n"
        "export function lateFee(daysLate: number): number {\n"
        "  if (daysLate <= 0) return 0;\n  return Math.min(daysLate * DAILY_LATE_FEE, MAX_LATE_FEE);\n}\n"
    ),
}


class ChoosesTheDeclaration:
    """Translates as the real model did, and chooses only the passage that declares the constant."""

    def __init__(self, chooses=(DECLARATION,), avoids=(CHECK,)) -> None:
        self.offered: list[tuple[str, str]] = []
        self.chooses, self.avoids = chooses, avoids

    def complete(self, system, user, max_output_tokens):
        if "<passage" not in user:
            return LLMResponse(json.dumps({"english": ENGLISH}), 61, 25, "stop")
        blocks = re.findall(r'<passage-\w+ id="(P\d+)" path="[^"]+" lines="[^"]+"[^>]*>\n(.*?)\n</passage-\w+>', user, re.S)
        self.offered = blocks
        chosen = [pid for want in self.chooses for pid, text in blocks if want in text and not any(no in text for no in self.avoids if no != want)][:2]
        return LLMResponse(json.dumps({"chosen": chosen, "note": "MAX_ACTIVE_LOANS is 3."}), 1058, 43, "stop")


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    git(root, "init", "-b", "main")
    for path, content in FILES.items():
        write(root, path, content)
    commit_all(root, "a made-up lending library")
    return root


@pytest.fixture
def search(store, library, fake_embedder):
    index_repository(store, str(library), fake_embedder, name="library")
    return SearchEngine(store, fake_embedder)


def ask(store, search, config=LOCAL, model=None):
    model = model or ChoosesTheDeclaration()
    result = QAEngine(store, search, config, model).ask(QUESTION, ["library"])
    return result, model


def holds(passages, text: str) -> bool:
    return any(text in passage.text for passage in passages)


@pytest.mark.db
class TestAValueTheModelFoundOnItsOwn:
    def test_both_places_reach_the_model_and_it_chooses_the_declaration(self, store, search):
        result, model = ask(store, search)
        offered = [text for _, text in model.offered]
        assert any(DECLARATION in text for text in offered), "the declaration is offered"
        assert any(CHECK in text for text in offered), "the line that uses it is offered"
        assert result.translated_query == ENGLISH

    def test_the_value_is_shown_with_the_check_that_uses_it_and_is_still_not_an_answer(self, store, search):
        """Accepting a declaration through a line that uses it was measured on 342 questions: about two in
        three such acceptances were a wrong place or an unanswerable question. So the verdict stays, and the
        reader is shown both places to check."""
        result, _ = ask(store, search)
        assert result.status == "insufficient"
        assert not result.evidence and result.answer == ""
        assert [p.text for p in result.review_candidates] == [p.text for p in result.review_candidates if DECLARATION in p.text]
        assert len(result.review_candidates) == 1, "the model's own choice"
        assert len(result.review_uses) == 1 and holds(result.review_uses, CHECK), "the use, found from the declaration"
        assert result.review_uses[0] in result.sent, "it was offered, so it passed the credential and Git checks"

    def test_both_are_listed_apart_for_review_with_their_commit_and_none_as_an_answer(self, store, search):
        result, _ = ask(store, search)
        sources = _sources(result)
        roles = {source.role for source in sources}
        assert "answer" not in roles and "declaration" not in roles
        [chosen] = [s for s in sources if s.role == "review_candidate"]
        [added] = [s for s in sources if s.role == "review_use"]
        assert DECLARATION in chosen.text and CHECK in added.text
        assert chosen.exists_in_git and added.exists_in_git and chosen.commit == added.commit
        assert (chosen.answer_check, added.answer_check) == ("failed", "not_made"), "the model did not choose the use"
        keys = [(s.path, s.start_line, s.end_line) for s in sources]
        assert len(keys) == len(set(keys)), "nothing is shown twice"

    def test_linked_evidence_as_built_does_not_take_it_either(self, store, search):
        """It also asks the declaration to share a word with the question, and this one shares none."""
        result, _ = ask(store, search, LLMConfig(LOCAL.base_url, "stand-in", linked_evidence=True))
        assert result.status == "insufficient"
        assert holds(result.review_candidates, DECLARATION) and holds(result.review_uses, CHECK)


@pytest.mark.db
class TestWhenNoUseIsShown:
    def test_a_use_that_did_not_match_git_is_not_brought_back(self, store, search, monkeypatch):
        verified = QAEngine._verified_passages

        def drop_the_check(self, passages, result):
            return [p for p in verified(self, passages, result) if CHECK not in p.text]

        monkeypatch.setattr(QAEngine, "_verified_passages", drop_the_check)
        result, _ = ask(store, search)
        assert result.status == "insufficient"
        assert holds(result.review_candidates, DECLARATION)
        assert result.review_uses == []
        assert not any(CHECK in p.text for p in (*result.passages, *result.sent))

    def test_a_use_that_does_not_pass_the_evidence_rule_itself_is_not_shown(self, store, search, monkeypatch):
        rule = engine_module.is_evidence
        monkeypatch.setattr(engine_module, "is_evidence", lambda passage, wording: CHECK not in passage.text and rule(passage, wording))
        result, _ = ask(store, search)
        assert result.status == "insufficient"
        assert holds(result.review_candidates, DECLARATION)
        assert result.review_uses == []

    def test_when_the_model_chooses_the_use_the_value_is_shown_with_it_and_nothing_is_left_to_review(self, store, search):
        result, _ = ask(store, search, model=ChoosesTheDeclaration(chooses=(CHECK,), avoids=()))
        assert result.status == "answered"
        assert holds([e.passage for e in result.evidence], CHECK) and holds(result.declared_values, DECLARATION)
        assert result.review_candidates == [] and result.review_uses == []

    def test_when_the_model_chooses_both_each_is_shown_once(self, store, search):
        result, _ = ask(store, search, model=ChoosesTheDeclaration(chooses=(DECLARATION, CHECK), avoids=()))
        assert result.status == "answered"
        assert result.review_candidates == [] and result.review_uses == []
        keys = [(s.path, s.start_line, s.end_line) for s in _sources(result)]
        assert len(keys) == len(set(keys))


@pytest.mark.db
class TestALineThatMustStayHidden:
    def test_a_line_withheld_for_a_credential_does_not_come_back_with_the_use(self, store, tmp_path, fake_embedder):
        """Index version 3 withholds the statement that holds a credential, and keeps the rest of the code."""
        root = tmp_path / "library-with-a-secret"
        root.mkdir()
        git(root, "init", "-b", "main")
        files = dict(FILES)
        files["src/loans.ts"] = files["src/loans.ts"].replace(
            f"  {CHECK}\n", f"  {CHECK}\n  const API_TOKEN = \"fake-token-for-repomind-tests-only\";\n"
        )
        assert "fake-token-for-repomind-tests-only" in files["src/loans.ts"]
        for path, content in files.items():
            write(root, path, content)
        commit_all(root, "a made-up lending library, with a fake credential beside the check")
        index_repository(store, str(root), fake_embedder, name="library")
        result, _ = ask(store, SearchEngine(store, fake_embedder))
        assert holds(result.review_uses, CHECK), "the check is still there to review"
        shown = [*result.review_candidates, *result.review_uses, *result.passages, *result.sent]
        assert not any("API_TOKEN" in p.text for p in shown)
        assert not any("fake-token-for-repomind-tests-only" in p.text for p in shown)
        assert not any("fake-token-for-repomind-tests-only" in s.text for s in _sources(result))


def _passage(pid: str, path: str, start: int, end: int, commit: str = "c1") -> Passage:
    chunk = SimpleNamespace(path=path, repo="library", commit_sha=commit, language="typescript", symbol=None)
    return Passage(pid, chunk, start, end, f"lines {start}-{end}")


def _found(name: str, used_in: Passage, path: str, line: int, reason: str = "") -> Completion:
    return Completion(name, f"{used_in.path}:{used_in.start_line}-{used_in.end_line}", Declaration(name, path, line, line), reason)


class TestWhichUsesAreTaken:
    """uses_to_review on its own, with made-up passages."""

    def setup_method(self):
        self.declaration = _passage("P1", "src/loans.ts", 4, 4)
        self.use = _passage("P2", "src/loans.ts", 13, 23)
        self.record = [_found("MAX_ACTIVE_LOANS", self.use, "src/loans.ts", 4)]

    def taken(self, review=None, chosen=(), offered=None, record=None, passes=lambda p: True):
        return uses_to_review(review or [self.declaration], chosen, offered or [self.declaration, self.use], record or self.record, passes)

    def test_a_resolved_use_that_was_offered_is_taken(self):
        assert self.taken() == [self.use]

    def test_a_declaration_already_shown_in_a_short_passage_counts_too(self):
        record = [_found("MAX_ACTIVE_LOANS", self.use, "src/loans.ts", 4, reason="already shown")]
        window = _passage("P1", "src/loans.ts", 3, 5)
        assert uses_to_review([window], [], [window, self.use], record, lambda p: True) == [self.use]

    def test_a_passage_longer_than_a_declaration_is_not_taken_for_one(self):
        long = _passage("P1", "src/loans.ts", 1, 9)
        assert uses_to_review([long], [], [long, self.use], self.record, lambda p: True) == []

    def test_an_unresolved_constant_brings_nothing(self):
        record = [Completion("MAX_ACTIVE_LOANS", "src/loans.ts:13-23", None, "no declaration of MAX_ACTIVE_LOANS was found")]
        assert self.taken(record=record) == []
        record = [_found("MAX_ACTIVE_LOANS", self.use, "src/loans.ts", 4, reason="different declarations in scope")]
        assert self.taken(record=record) == []

    def test_a_declaration_of_another_constant_brings_nothing(self):
        record = [_found("LOAN_DAYS", self.use, "src/loans.ts", 5)]
        assert self.taken(record=record) == []

    def test_a_use_that_was_not_offered_is_not_brought_back(self):
        assert self.taken(offered=[self.declaration]) == []

    def test_a_use_from_another_commit_is_not_taken(self):
        other = _passage("P2", "src/loans.ts", 13, 23, commit="c2")
        assert self.taken(offered=[self.declaration, other]) == []

    def test_a_use_that_does_not_pass_the_rule_is_not_taken(self):
        assert self.taken(passes=lambda p: p is not self.use) == []

    def test_a_use_already_shown_is_not_shown_again(self):
        assert self.taken(chosen=[self.use]) == []
        assert self.taken(review=[self.declaration, self.use]) == []

    def test_at_most_a_few_are_added(self):
        uses = [_passage(f"P{n}", "src/a.ts", n * 10, n * 10 + 3) for n in range(2, 6)]
        record = [_found("LIMIT", use, "src/loans.ts", 4) for use in uses]
        assert len(uses_to_review([self.declaration], [], [self.declaration, *uses], record, lambda p: True)) == MAX_COMPLETIONS
