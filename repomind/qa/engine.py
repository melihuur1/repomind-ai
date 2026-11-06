"""Retrieve, narrow down to a few real lines, and let the model choose among them or decline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from repomind.errors import RepoMindError
from repomind.evaluation.citations import CitationVerifier
from repomind.ingest.git_source import GitRepository
from repomind.qa.llm import LLMClient, LLMConfig, cost_usd
from repomind.qa.passages import Passage, is_evidence, select_passages
from repomind.qa.prompt import (
    PreparedPrompt,
    build_selection_prompt,
    build_translation_prompt,
    fit_passages,
    new_nonce,
)
from repomind.qa.sufficiency import check_sufficiency
from repomind.qa.verification import VerifiedCitation, verify_selection
from repomind.search.engine import SearchEngine, SearchHit
from repomind.search.translation import looks_turkish
from repomind.store.database import Store

# If the endpoint reports far fewer input tokens than we sent, it truncated the prompt
# silently (for example Ollama's small default context) and sources were never seen.
_TRUNCATION_RATIO = 0.5
_TRANSLATION_OUTPUT_TOKENS = 80
_MAX_TRANSLATION_CHARACTERS = 400


@dataclass(frozen=True)
class Evidence:
    passage: Passage
    citation: VerifiedCitation


@dataclass
class AskResult:
    """``passages`` are the closest lines found. Only ``evidence`` was chosen as an answer."""

    status: str  # answered | insufficient | invalid_response | no_llm | no_results
    question: str
    answer: str = ""
    evidence: list[Evidence] = field(default_factory=list)
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

    @property
    def citations(self) -> list[VerifiedCitation]:
        return [item.citation for item in self.evidence]


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
        return english.strip()

    def ask(self, question: str, repos: list[str] | None = None) -> AskResult:
        chunk_limit = self.config.max_context_chunks if self.config else 8
        result = AskResult(status="no_llm", question=question)
        # Before the search: a refused question should not leave anything half done.
        self.search.check_repositories(repos)
        english = self._translate_with_the_model(question, result)
        retrieved = self.search.search(question, "hybrid", repos, chunk_limit, translated=english)
        result.hits = retrieved.hits
        result.translated_query = retrieved.translated_query
        result.warnings.extend(retrieved.warnings)
        if not retrieved.hits:
            result.status = "no_results"
            return result

        # Decided before any model call, which also spends nothing on such questions.
        sufficiency = check_sufficiency(
            question,
            retrieved.translated_query,
            self.search.vocabulary(question, retrieved.translated_query, repos),
        )
        if sufficiency.message:
            result.warnings.append(sufficiency.message)
        if sufficiency.insufficient:
            result.status = "insufficient"
            return result

        queries = [text for text in (question, retrieved.translated_query) if text]
        found = select_passages([hit.chunk for hit in retrieved.hits], queries, self.search.embedder)
        result.passages = self._verified_passages(found, result)
        if not result.passages:
            result.status = "insufficient"
            result.warnings.append("Search found code, but no lines that mention what the question asks about.")
            return result
        # Code is in English, so the English wording is what can be compared with it.
        wording = retrieved.translated_query or question
        if not any(is_evidence(passage, wording) for passage in result.passages):
            result.status = "insufficient"
            result.warnings.append("The closest lines mention too little of the question to count as evidence.")
            return result

        if self.client is None or self.config is None:
            return result

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
        if selection.outcome == "invalid":
            result.status = "invalid_response"
            result.rejected.append(selection.reason)
            result.warnings.append("The model did not return the required JSON.")
        elif selection.outcome == "none":
            result.status = "insufficient"
        elif not (chosen := [passage for passage in selection.chosen if is_evidence(passage, wording)]):
            result.status = "insufficient"
            result.rejected.append("the model chose lines that mention too little of the question")
        else:
            result.status = "answered"
            result.evidence = [
                Evidence(
                    passage,
                    VerifiedCitation(
                        passage.id,
                        passage.chunk.repo,
                        passage.chunk.commit_sha,
                        passage.path,
                        passage.start_line,
                        passage.end_line,
                    ),
                )
                for passage in chosen
            ]
            # A note was written about everything chosen, so it may describe lines that were just left out.
            complete = len(chosen) == len(selection.chosen)
            result.note = selection.note if complete else ""
            result.note_problem = selection.note_problem if complete else "it may describe lines that were left out"
            result.answer = assemble_answer(result.evidence, result.note)
        return result
