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

from repomind.indexing import index_repository
from repomind.qa.engine import QAEngine
from repomind.qa.llm import LLMConfig, LLMResponse
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

    def __init__(self) -> None:
        self.offered: list[tuple[str, str]] = []

    def complete(self, system, user, max_output_tokens):
        if "<passage" not in user:
            return LLMResponse(json.dumps({"english": ENGLISH}), 61, 25, "stop")
        blocks = re.findall(r'<passage-\w+ id="(P\d+)" path="[^"]+" lines="[^"]+"[^>]*>\n(.*?)\n</passage-\w+>', user, re.S)
        self.offered = blocks
        chosen = [pid for pid, text in blocks if DECLARATION in text and CHECK not in text][:1]
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


def ask(store, search, config=LOCAL):
    model = ChoosesTheDeclaration()
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

    def test_today_it_is_only_a_source_to_review(self, store, search):
        result, _ = ask(store, search)
        assert result.status == "insufficient"
        assert holds(result.review_candidates, DECLARATION)
        assert not result.evidence

    def test_linked_evidence_as_built_does_not_take_it_either(self, store, search):
        """It also asks the declaration to share a word with the question, and this one shares none."""
        result, _ = ask(store, search, LLMConfig(LOCAL.base_url, "stand-in", linked_evidence=True))
        assert result.status == "insufficient"
        assert holds(result.review_candidates, DECLARATION)

    @pytest.mark.xfail(strict=True, reason="Proposed, and not built: the value shown with the check that uses it, for review.")
    def test_proposed_the_value_is_shown_with_the_check_that_uses_it_and_is_still_not_called_an_answer(self, store, search):
        """Accepting a declaration through a line that uses it was measured on 342 questions: about two in
        three such acceptances were a wrong place or an unanswerable question. So the verdict stays, and the
        reader is shown both places to check."""
        result, _ = ask(store, search)
        assert result.status == "insufficient"
        assert not result.evidence
        assert holds(result.review_candidates, DECLARATION) and holds(result.review_candidates, CHECK)
