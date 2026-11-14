"""Retrieve, narrow down to a few real lines, and let the model choose among them or decline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from repomind.errors import RepoMindError
from repomind.evaluation.citations import CitationVerifier
from repomind.ingest.git_source import GitRepository
from repomind.qa.completion import Completion, SnapshotSources, complete, linked_evidence, unshown_values
from repomind.qa.judge import Judgement, build_judge_prompt, parse_judgement
from repomind.qa.llm import LLMClient, LLMConfig, cost_usd
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
from repomind.search.translation import keep_technical_terms, looks_turkish
from repomind.store.database import Store

# If the endpoint reports far fewer input tokens than we sent, it truncated the prompt
# silently (for example Ollama's small default context) and sources were never seen.
_TRUNCATION_RATIO = 0.5
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
    context_chars: int = 0
    sources_sent: int = 0
    sources_dropped: int = 0
    cost_usd: float = 0.0
    # Passage id and the role the model gave it, when it judges passages one at a time.
    judgements: list[tuple[str, str]] = field(default_factory=list)
    # Declarations of constants the passages use, added from the syntax tree, and the ones that were not.
    completions: list[Completion] = field(default_factory=list)

    @property
    def citations(self) -> list[VerifiedCitation]:
        return [item.citation for item in self.evidence]

    def sources(self) -> list[Source]:
        """Everything shown to a reader, with both checks. Only lines matching Git ever reach ``passages``."""
        passed = {item.passage.id for item in self.evidence}
        failed = {passage.id for passage in self.review_candidates}

        def check(passage: Passage) -> AnswerCheck:
            if passage.id in passed:
                return AnswerCheck.PASSED
            return AnswerCheck.FAILED if passage.id in failed else AnswerCheck.NOT_MADE

        return [Source(passage, True, check(passage)) for passage in self.passages]


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
            raise RepoMindError(
                f"This request could cost ${worst_case:.6f}, and ${spent:.6f} of the "
                f"${config.max_total_cost_usd:.6f} budget is already spent. Nothing was sent.",
                hint="Raise REPOMIND_LLM_MAX_TOTAL_COST_USD if this spending is intended.",
            )

    def _call(self, prompt: PreparedPrompt, max_output_tokens: int, result: AskResult) -> str:
        """The one place that talks to the model, so that no request escapes the budget or the accounting."""
        self._enforce_budget(prompt)
        response = self.client.complete(prompt.system, prompt.user, max_output_tokens)

        estimated = response.input_tokens is None or response.output_tokens is None
        result.tokens_are_estimated = result.tokens_are_estimated or estimated
        input_tokens = response.input_tokens if response.input_tokens is not None else prompt.estimated_input_tokens
        output_tokens = response.output_tokens if response.output_tokens is not None else max(1, len(response.text) // 3)
        cost = cost_usd(self.config, input_tokens, output_tokens)
        result.input_tokens += input_tokens
        result.output_tokens += output_tokens
        result.cost_usd += cost
        result.context_chars = max(result.context_chars, prompt.context_chars)
        self.store.record_llm_usage(self.config.base_url, self.config.model, input_tokens, output_tokens, cost)
        self._warn_if_truncated(response.input_tokens, prompt, result)
        return response.text

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
        return keep_technical_terms(question, english.strip())

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
            return result

        queries = [text for text in (searched.question, retrieved.translated_query, searched.second) if text]
        found = select_passages(
            [hit.chunk for hit in retrieved.hits], queries, self.search.embedder, more_terms=retrieved.glossary_terms
        )
        # Code is in English, so the English wording is what can be compared with it.
        wording = retrieved.translated_query or searched.question
        asked_wording = asked_translation or question

        def counts_as_evidence(passage: Passage) -> bool:
            if is_evidence(passage, wording):
                return True
            return name_left_out and counts_with_name(repo, passage, wording, asked_wording)

        if name_left_out:
            result.warnings.append("The repository's name was left out of the search, because every result is in this repository.")
            found = self._with_name_seat(repo, asked, found, wording, asked_wording, chunk_limit)
        if found:
            found = self._completed(found, result)
        result.passages = self._verified_passages(found, result)
        if not result.passages:
            result.status = "insufficient"
            result.warnings.append("Search found code, but no lines that mention what the question asks about.")
            return result
        if not any(counts_as_evidence(passage) for passage in result.passages):
            result.status = "insufficient"
            result.warnings.append("The closest lines mention too little of the question to count as evidence.")
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
        if not offered:
            result.status = "insufficient"
            result.warnings.append("The passages do not fit within the input token limit, so the model was not called.")
            return result

        prompt = build_selection_prompt(question, offered, nonce, english)
        reply = self._call(prompt, self.config.max_output_tokens, result)
        selection = verify_selection(reply, offered, question)
        # A declaration completion added counts only beside a use that passes the rule. See linked_evidence.
        chosen, turned_down, linked = linked_evidence(selection.chosen, offered, result.completions, wording, counts_as_evidence)
        # Without the model's note, which is the least trustworthy thing about a choice the rule turned down.
        result.review_candidates = turned_down
        if selection.outcome == "invalid":
            result.status = "invalid_response"
            result.rejected.append(selection.reason)
            result.warnings.append("The model did not return the required JSON.")
        elif selection.outcome == "none":
            result.status = "insufficient"
        elif not chosen:
            result.status = "insufficient"
            result.rejected.append("the model chose lines that mention too little of the question")
        else:
            result.status = "answered"
            result.evidence = _evidence(chosen)
            result.warnings.extend(linked)
            # A value the chosen lines only name, and do not show, is not to be taken for granted.
            result.warnings.extend(unshown_values(chosen, result.completions))
            # A note was written about everything chosen, so it may describe lines that were just left out.
            complete = not turned_down
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
        result.judgements = [(passage.id, judgement.role if judgement else "invalid") for passage, judgement in judged]
        if not judged:
            result.status = "insufficient"
            result.warnings.append("The passages do not fit within the input token limit, so the model was not called.")
            return result
        if all(judgement is None for _, judgement in judged):
            result.status = "invalid_response"
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
        result.evidence = _evidence(chosen)
        result.warnings.extend(unshown_values(chosen, result.completions))
        reason = next(judgement.reason for passage, judgement in judged if passage is chosen[0])
        problem = note_problem(reason, chosen[:1], question) if reason else None
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
