"""Retrieve, narrow down to a few real lines, and let the model choose among them or decline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import Enum

from repomind.errors import RepoMindError
from repomind.evaluation.citations import CitationVerifier
from repomind.ingest.filters import find_credential
from repomind.ingest.git_source import GitRepository
from repomind.qa.completion import Completion, SnapshotSources, complete, declared_values, linked_evidence, unshown_values
from repomind.qa.judge import Judgement, build_judge_prompt, parse_judgement
from repomind.qa.llm import BudgetReached, LLMClient, LLMConfig, LLMResponse, usage_cost
from repomind.qa.passages import Passage, is_evidence, select_passages
from repomind.qa.project_name import Wording, counts_with_name, stands_alone, with_name_seat, without_project_name
from repomind.qa.prompt import (
    PreparedPrompt,
    build_selection_prompt,
    build_translation_prompt,
    fit_passages,
    new_nonce,
)
from repomind.qa.sufficiency import check_sufficiency
from repomind.qa.verification import MAX_CHOSEN, VerifiedCitation, note_problem, verify_selection
from repomind.search.engine import SearchEngine, SearchHit
from repomind.search.translation import keep_technical_terms, looks_turkish, plain_sentence
from repomind.store.database import Store

# If the endpoint reports far fewer input tokens than we sent, it truncated the prompt
# silently (for example Ollama's small default context) and sources were never seen.
_TRUNCATION_RATIO = 0.5
# How much of a reply that could not be used is kept for looking into it.
_INVALID_REPLY_CHARACTERS = 2000
# What the reader is told about a reply that could not be used. Any other kind gets the general sentence.
_REPLY_WARNINGS = {
    "empty": "The model returned an empty reply.",
    "cut_off": "The model's reply was cut off at its output limit.",
}
_CHUNKS_WITHOUT_A_MODEL = 12
# Beyond those. With anything from 2 to 8 seats, the same number of questions reached the passages.
_GLOSSARY_SEATS = 5
_TRANSLATION_OUTPUT_TOKENS = 80
_MAX_TRANSLATION_CHARACTERS = 400
# A role and one sentence.
_JUDGE_OUTPUT_TOKENS = 90


@dataclass(frozen=True)
class Evidence:
    passage: Passage
    citation: VerifiedCitation


class AnswerCheck(str, Enum):
    """How far lines were checked against the question, which is a separate matter from existing in Git."""

    NOT_MADE = "not_made"  # The closest lines found. Nothing judged them.
    PASSED = "passed"  # The model chose them and they passed the evidence rule. A check, not a proof.
    FAILED = "failed"  # The model chose them and they did not pass, so they are only something to review.


@dataclass(frozen=True)
class Source:
    """Code shown to a reader, with two checks that must not be confused.

    ``exists_in_git`` says these lines are what the file holds at the indexed commit. That does not
    make them an answer: real lines can be about something else. ``answer_check`` is about that.
    """

    passage: Passage
    exists_in_git: bool
    answer_check: AnswerCheck
    # Shown because a line the model chose declares a constant these lines use. The model did not choose them.
    added_by_link: bool = False
    # Shown because lines the model chose use a constant these lines declare. The model did not choose them.
    declares_a_value: bool = False


@dataclass
class AskResult:
    """``passages`` are the closest lines found. Only ``evidence`` was chosen as an answer.

    ``review_candidates`` were chosen by the model and turned down by the evidence rule. They are
    something a reader can look at. They never affect the status, the answer or the citations.
    """

    status: str  # answered | insufficient | invalid_response | no_llm | no_results
    question: str
    answer: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    review_candidates: list[Passage] = field(default_factory=list)
    note: str = ""
    note_problem: str | None = None
    passages: list[Passage] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hits: list[SearchHit] = field(default_factory=list)
    translated_query: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_are_estimated: bool = False
    # The cost counts some input at the highest rate, because the server did not say how it was cached.
    cost_is_cautious: bool = False
    context_chars: int = 0
    sources_sent: int = 0
    sources_dropped: int = 0
    cost_usd: float = 0.0
    # Passage id and the role the model gave it, when it judges passages one at a time.
    judgements: list[tuple[str, str]] = field(default_factory=list)
    # Declarations of constants the passages use, added from the syntax tree, and the ones that were not.
    completions: list[Completion] = field(default_factory=list)
    # Evidence the model did not choose: the use of a constant whose declaration it chose. Only with linked evidence on.
    linked_uses: list[Passage] = field(default_factory=list)
    # With the status invalid_response, what was wrong with the reply: empty, cut_off, not_json,
    # no_choice_list, not_offered, or no_judgement when judging. None with any other status.
    reply_problem: str | None = None
    # The start of that reply, kept for whoever looks into it. It is never shown as an answer.
    invalid_reply: str | None = None
    # The passages that went into the prompt, and which rule ended the question before any model was asked.
    # Both are for repomind.qa.trace, which says where a right source was lost.
    sent: list[Passage] = field(default_factory=list)
    stopped_by: str | None = None
    # Declarations of constants that the chosen lines use, shown with them. The model chose the use,
    # and not these: they are here so that an answer naming a constant also shows its value.
    declared_values: list[Passage] = field(default_factory=list)

    @property
    def citations(self) -> list[VerifiedCitation]:
        return [item.citation for item in self.evidence]

    def sources(self) -> list[Source]:
        """Everything shown to a reader, with both checks. Only lines matching Git ever reach ``passages``."""
        passed = {item.passage.id for item in self.evidence}
        failed = {passage.id for passage in self.review_candidates}
        linked = {passage.id for passage in self.linked_uses}
        declared = {passage.id for passage in self.declared_values}

        def check(passage: Passage) -> AnswerCheck:
            if passage.id in passed:
                return AnswerCheck.PASSED
            return AnswerCheck.FAILED if passage.id in failed else AnswerCheck.NOT_MADE

        return [Source(passage, True, check(passage), passage.id in linked, passage.id in declared) for passage in self.passages]


def assemble_answer(evidence: list[Evidence], note: str = "") -> str:
    """Written by the backend. The code comes first, and the model's sentence is labelled as its own."""
    parts = [
        f"{item.citation.path}:{item.citation.start_line}-{item.citation.end_line}\n{item.passage.text}" for item in evidence
    ]
    if note:
        parts.append(f"Model's reading, not verified: {note}")
    return "\n\n".join(parts)


class QAEngine:
    def __init__(
        self,
        store: Store,
        search: SearchEngine,
        config: LLMConfig | None,
        client: LLMClient | None,
    ) -> None:
        self.store = store
        self.search = search
        self.config = config
        self.client = client
        self._verifiers: dict[tuple[str, str], CitationVerifier] = {}

    def _verifier(self, repo: str, commit: str) -> CitationVerifier:
        key = (repo, commit)
        if key not in self._verifiers:
            snapshot = next(s for s in self.store.active_snapshots([repo]) if s.commit_sha == commit)
            self._verifiers[key] = CitationVerifier(GitRepository(snapshot.source_path), commit)
        return self._verifiers[key]

    def _completed(self, passages: list[Passage], result: AskResult) -> list[Passage]:
        """The passages with the declarations of constants they use, when all come from one commit."""
        commits = {(p.chunk.repo, p.chunk.commit_sha) for p in passages}
        if len(commits) != 1:
            return passages
        repo, commit = next(iter(commits))
        snapshot = next((s for s in self.store.active_snapshots([repo]) if s.commit_sha == commit), None)
        if snapshot is None:
            return passages
        sources = SnapshotSources(self.store, snapshot.id, self._verifier(repo, commit).git, commit)
        completed, result.completions = complete(passages, sources)
        return completed

    @staticmethod
    def _without_credentials(passages: list[Passage], result: AskResult) -> list[Passage]:
        """The last check before lines are verified, offered to a model or shown: none may hold a credential.

        Search already leaves such chunks out. This is for whatever reaches the passages another way,
        as a completed declaration does. What is left out is named by its place, never by its text.
        """
        kept = []
        for passage in passages:
            if find_credential(passage.text, passage.chunk.language):
                result.rejected.append(f"{passage.path}:{passage.start_line}-{passage.end_line}: left out, because it may hold a credential")
            else:
                kept.append(passage)
        if len(kept) == len(passages):
            return passages
        return [replace(passage, id=f"P{number}") for number, passage in enumerate(kept, start=1)]

    def _verified_passages(self, passages: list[Passage], result: AskResult) -> list[Passage]:
        """Against Git rather than RepoMind's database, so a tampered row is never shown."""
        verified = []
        for passage in passages:
            chunk = passage.chunk
            check = self._verifier(chunk.repo, chunk.commit_sha).check(
                chunk.path, passage.start_line, passage.end_line, passage.text
            )
            if check.valid:
                verified.append(passage)
            else:
                result.rejected.append(f"{chunk.path}:{passage.start_line}-{passage.end_line}: {check.reason}")
        return verified

    def _enforce_budget(self, prompt: PreparedPrompt) -> None:
        config = self.config
        if config.is_free:
            return
        spent = self.store.total_llm_cost()
        worst_case = config.worst_case_cost(prompt.estimated_input_tokens)
        if spent + worst_case > config.max_total_cost_usd:
            raise BudgetReached(
                f"This request could cost ${worst_case:.6f}, and ${spent:.6f} of the "
                f"${config.max_total_cost_usd:.6f} budget is already spent. Nothing was sent.",
                hint="Raise REPOMIND_LLM_MAX_TOTAL_COST_USD if this spending is intended.",
            )

    def _ask_model(self, prompt: PreparedPrompt, max_output_tokens: int, result: AskResult) -> LLMResponse:
        """The one place that talks to the model, so that no request escapes the budget or the accounting."""
        self._enforce_budget(prompt)
        try:
            response = self.client.complete(prompt.system, prompt.user, max_output_tokens)
        except RepoMindError as exc:
            self._count_what_it_may_have_cost(exc, prompt, max_output_tokens)
            raise

        # No prompt is empty, so 0 input tokens is a missing count and not a small one. An endpoint that
        # ends a reply without its closing figures reports it. Counting it as 0 would undercount what was
        # spent, and would be taken for a truncated prompt.
        reported_input = response.input_tokens or None
        estimated = reported_input is None or response.output_tokens is None
        result.tokens_are_estimated = result.tokens_are_estimated or estimated
        input_tokens = reported_input if reported_input is not None else prompt.estimated_input_tokens
        output_tokens = response.output_tokens if response.output_tokens is not None else max(1, len(response.text) // 3)
        # Counts of cache reads and writes belong to the reported input, and mean nothing beside an estimate.
        cached = getattr(response, "cached_tokens", None) if reported_input is not None else None
        written = getattr(response, "cache_write_tokens", None) if reported_input is not None else None
        cost, cautious = usage_cost(self.config, input_tokens, output_tokens, cached, written)
        result.cost_is_cautious = result.cost_is_cautious or cautious
        result.input_tokens += input_tokens
        result.output_tokens += output_tokens
        result.cost_usd += cost
        result.context_chars = max(result.context_chars, prompt.context_chars)
        self.store.record_llm_usage(
            self.config.base_url,
            self.config.model,
            input_tokens,
            output_tokens,
            cost,
            "estimated" if estimated or cautious else "reported",
        )
        self._warn_if_truncated(reported_input, prompt, result)
        return response

    def _count_what_it_may_have_cost(self, exc: RepoMindError, prompt: PreparedPrompt, max_output_tokens: int) -> None:
        """A paid request that failed after it may have reached the model is counted at the most it could cost.

        Whether such a request is charged is not known. Counting it at its most keeps the spending recorded
        at or above what was charged, so the budget is not crossed by requests that were never counted.
        """
        if self.config.is_free or not getattr(exc, "may_have_been_charged", False):
            return
        cost = self.config.worst_case_cost(prompt.estimated_input_tokens, max_output_tokens)
        self.store.record_llm_usage(
            self.config.base_url, self.config.model, prompt.estimated_input_tokens, max_output_tokens, cost, "worst_case"
        )

    def _call(self, prompt: PreparedPrompt, max_output_tokens: int, result: AskResult) -> str:
        return self._ask_model(prompt, max_output_tokens, result).text

    @staticmethod
    def _warn_if_truncated(reported: int | None, prompt: PreparedPrompt, result: AskResult) -> None:
        if (
            reported is not None
            and reported < _TRUNCATION_RATIO * prompt.estimated_input_tokens
            and not any("truncated" in warning for warning in result.warnings)
        ):
            result.warnings.append(
                f"The endpoint reports {reported} input tokens for a prompt of about "
                f"{prompt.estimated_input_tokens}: it may have truncated the source."
            )

    def _translate_with_the_model(self, question: str, result: AskResult) -> str | None:
        """Far better than opus-mt on software terms. None falls back to the local translator."""
        if self.client is None or self.config is None or not looks_turkish(question):
            return None
        try:
            text = self._call(build_translation_prompt(question), _TRANSLATION_OUTPUT_TOKENS, result)
            english = json.loads(text)["english"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        if not isinstance(english, str) or not english.strip() or len(english) > _MAX_TRANSLATION_CHARACTERS:
            return None
        return keep_technical_terms(question, plain_sentence(english.strip()))

    def _with_name_seat(
        self, repo: str, asked: Wording, passages: list[Passage], wording: str, asked_wording: str, chunk_limit: int
    ) -> list[Passage]:
        """Searches again with the question as asked, for lines that name the project."""
        as_asked = self.search.search(
            asked.question,
            "hybrid",
            [repo],
            chunk_limit,
            translated=asked.translated,
            more_wordings=[asked.second] if asked.second else (),
            glossary_seats=_GLOSSARY_SEATS,
        )
        queries = [text for text in (asked.question, as_asked.translated_query, asked.second) if text]
        found = select_passages(
            [hit.chunk for hit in as_asked.hits], queries, self.search.embedder, more_terms=as_asked.glossary_terms
        )
        return with_name_seat(repo, found, passages, wording, asked_wording)

    def ask(self, question: str, repos: list[str] | None = None) -> AskResult:
        chunk_limit = self.config.max_context_chunks if self.config else _CHUNKS_WITHOUT_A_MODEL
        result = AskResult(status="no_llm", question=question)
        # Before the search: a refused question should not leave anything half done.
        self.search.check_repositories(repos)
        english = self._translate_with_the_model(question, result)
        # A second translator's wording is searched too, so one wrong translation cannot decide alone.
        second = self.search.local_translation(question, result.warnings) if english else None

        asked = searched = Wording(question, english, second)
        # Asked about one repository, its name says nothing about where in it to look.
        repo = repos[0] if repos and len(repos) == 1 else None
        if repo and stands_alone(repo, question):
            asked = Wording(question, english or self.search.local_translation(question, result.warnings), second)
            searched = without_project_name(repo, asked) or asked
        name_left_out = searched is not asked

        retrieved = self.search.search(
            searched.question,
            "hybrid",
            repos,
            chunk_limit,
            translated=searched.translated,
            more_wordings=[searched.second] if searched.second else (),
            glossary_seats=_GLOSSARY_SEATS,
        )
        asked_translation = asked.translated if name_left_out else retrieved.translated_query
        result.hits = retrieved.hits
        result.translated_query = asked_translation
        result.warnings.extend(retrieved.warnings)
        if not retrieved.hits:
            result.status = "no_results"
            return result

        # Decided before any model call, which also spends nothing on such questions. Always on the
        # question as asked: without its first word, a name such as Kafka would no longer look like one.
        sufficiency = check_sufficiency(
            question,
            asked_translation,
            self.search.vocabulary(question, asked_translation, repos),
        )
        if sufficiency.message:
            result.warnings.append(sufficiency.message)
        if sufficiency.insufficient:
            result.status = "insufficient"
            result.stopped_by = "words of the question are nowhere in the code"
            return result

        queries = [text for text in (searched.question, retrieved.translated_query, searched.second) if text]
        found = select_passages(
            [hit.chunk for hit in retrieved.hits], queries, self.search.embedder, more_terms=retrieved.glossary_terms
        )
        # Code is in English, so the English wording is what can be compared with it.
        wording = retrieved.translated_query or searched.question
        asked_wording = asked_translation or question
        # A translation can change what a question is about, and then no right line shares its words.
        # So the second translator's wording counts as well: lines are evidence when they mention
        # enough of either. Each wording is compared on its own, so that the words of a wrong one
        # are never added to those of a right one.
        wordings = [text for text in dict.fromkeys((wording, searched.second)) if text]

        def counts_as_evidence(passage: Passage) -> bool:
            if any(is_evidence(passage, text) for text in wordings):
                return True
            return name_left_out and counts_with_name(repo, passage, wording, asked_wording)

        if name_left_out:
            result.warnings.append("The repository's name was left out of the search, because every result is in this repository.")
            found = self._with_name_seat(repo, asked, found, wording, asked_wording, chunk_limit)
        if found:
            found = self._completed(found, result)
        found = self._without_credentials(found, result)
        result.passages = self._verified_passages(found, result)
        if not result.passages:
            result.status = "insufficient"
            result.warnings.append("Search found code, but no lines that mention what the question asks about.")
            result.stopped_by = "no line shares a word with the question"
            return result
        if not any(counts_as_evidence(passage) for passage in result.passages):
            result.status = "insufficient"
            result.warnings.append("The closest lines mention too little of the question to count as evidence.")
            result.stopped_by = "no passage passes the evidence rule"
            return result

        if self.client is None or self.config is None:
            return result
        if self.config.selection == "judge":
            return self._judge_each(question, english, result, counts_as_evidence)

        nonce = new_nonce()
        offered, result.sources_dropped = fit_passages(
            question, result.passages, self.config.max_input_tokens, nonce, english
        )
        result.model = self.config.model
        result.sources_sent = len(offered)
        result.sent = list(offered)
        if not offered:
            result.status = "insufficient"
            result.warnings.append("The passages do not fit within the input token limit, so the model was not called.")
            return result

        prompt = build_selection_prompt(question, offered, nonce, english)
        response = self._ask_model(prompt, self.config.max_output_tokens, result)
        selection = verify_selection(response.text, offered, question, getattr(response, "finish_reason", None))
        if self.config.linked_evidence:
            # Experimental: a declaration completion added counts beside a use that passes the rule.
            chosen, turned_down, linked = linked_evidence(selection.chosen, offered, result.completions, wording, counts_as_evidence)
            result.linked_uses = [passage for passage in chosen if passage not in selection.chosen]
        else:
            # The evidence rule alone. A declaration is evidence only if it passes the rule itself.
            chosen = [passage for passage in selection.chosen if counts_as_evidence(passage)]
            turned_down = [passage for passage in selection.chosen if not counts_as_evidence(passage)]
            linked = []
        # Without the model's note, which is the least trustworthy thing about a choice the rule turned down.
        result.review_candidates = turned_down
        if selection.outcome == "invalid":
            # Neither an answer nor a refusal: the model said nothing that can be used, and that is kept apart.
            result.status = "invalid_response"
            result.reply_problem = selection.problem
            result.invalid_reply = response.text[:_INVALID_REPLY_CHARACTERS]
            result.rejected.append(selection.reason)
            result.warnings.append(_REPLY_WARNINGS.get(selection.problem, "The model did not return the required JSON."))
        elif selection.outcome == "none":
            result.status = "insufficient"
        elif not chosen:
            result.status = "insufficient"
            result.rejected.append("the model chose lines that mention too little of the question")
        else:
            result.status = "answered"
            # The declaration of a constant the chosen lines use is shown with them. It was offered to
            # the model as well, and the model's choice of the use is what brings it here.
            result.declared_values = declared_values(chosen, result.completions, offered)
            # Shown as a value, it is no longer something the reader is only asked to review.
            result.review_candidates = [p for p in turned_down if all(p is not shown for shown in result.declared_values)]
            chosen = [*chosen, *result.declared_values]
            result.evidence = _evidence(chosen)
            result.warnings.extend(linked)
            # A value the chosen lines only name, and do not show, is not to be taken for granted.
            result.warnings.extend(unshown_values(chosen, result.completions))
            # A note was written about everything chosen, so it may describe lines that were just left out.
            complete = not result.review_candidates
            result.note = selection.note if complete else ""
            result.note_problem = selection.note_problem if complete else "it may describe lines that were left out"
            result.answer = assemble_answer(result.evidence, result.note)
        return result

    def _judge_each(self, question: str, english: str | None, result: AskResult, counts_as_evidence) -> AskResult:
        """One request per passage, so that neither the other passages nor their order sway a judgement."""
        nonce = new_nonce()
        result.model = self.config.model
        judged: list[tuple[Passage, Judgement | None]] = []
        for passage in result.passages:
            prompt = build_judge_prompt(question, passage, nonce, english)
            if prompt.estimated_input_tokens > self.config.max_input_tokens:
                result.sources_dropped += 1
                continue
            reply = self._call(prompt, min(_JUDGE_OUTPUT_TOKENS, self.config.max_output_tokens), result)
            judged.append((passage, parse_judgement(reply)))
        result.sources_sent = len(judged)
        result.sent = [passage for passage, _ in judged]
        result.judgements = [(passage.id, judgement.role if judgement else "invalid") for passage, judgement in judged]
        if not judged:
            result.status = "insufficient"
            result.warnings.append("The passages do not fit within the input token limit, so the model was not called.")
            return result
        if all(judgement is None for _, judgement in judged):
            result.status = "invalid_response"
            result.reply_problem = "no_judgement"
            result.rejected.append("no judgement had one of the four roles")
            result.warnings.append("The model did not return the required JSON.")
            return result

        answering = [passage for passage, judgement in judged if judgement and judgement.role == "answers"]
        # The model says these answer and the evidence rule does not: something to review, never an answer.
        result.review_candidates = [passage for passage in answering if not counts_as_evidence(passage)]
        chosen = [passage for passage in answering if counts_as_evidence(passage)][:MAX_CHOSEN]
        if not chosen:
            result.status = "insufficient"
            if answering:
                result.rejected.append("the model judged lines that mention too little of the question to answer it")
            return result

        result.status = "answered"
        first = chosen[0]
        result.declared_values = declared_values(chosen, result.completions, [passage for passage, _ in judged])
        chosen = [*chosen, *result.declared_values]
        result.evidence = _evidence(chosen)
        result.warnings.extend(unshown_values(chosen, result.completions))
        reason = next(judgement.reason for passage, judgement in judged if passage is first)
        problem = note_problem(reason, [first], question) if reason else None
        result.note = "" if problem else reason
        result.note_problem = problem
        result.answer = assemble_answer(result.evidence, result.note)
        return result


def _evidence(chosen: list[Passage]) -> list[Evidence]:
    return [
        Evidence(
            passage,
            VerifiedCitation(passage.id, passage.chunk.repo, passage.chunk.commit_sha, passage.path, passage.start_line, passage.end_line),
        )
        for passage in chosen
    ]
