"""Retrieve, read each source separately, keep only what the source itself proves."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from repomind.errors import RepoMindError
from repomind.evaluation.citations import CitationVerifier
from repomind.ingest.git_source import GitRepository
from repomind.qa.llm import LLMClient, LLMConfig, cost_usd
from repomind.qa.prompt import (
    ContextSource,
    PreparedPrompt,
    build_reading_prompt,
    build_translation_prompt,
    new_nonce,
    select_sources,
)
from repomind.qa.sufficiency import check_sufficiency
from repomind.qa.verification import Evidence, Reading, VerifiedCitation, verify_reading
from repomind.search.engine import SearchEngine, SearchHit
from repomind.search.translation import looks_turkish
from repomind.store.database import Store

# If the endpoint reports far fewer input tokens than we sent, it truncated the prompt
# silently (for example Ollama's small default context) and sources were never seen.
_TRUNCATION_RATIO = 0.5
_TRANSLATION_OUTPUT_TOKENS = 80
_MAX_TRANSLATION_CHARACTERS = 400


@dataclass
class AskResult:
    status: str  # answered | insufficient | invalid_response | no_llm | no_results
    question: str
    answer: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    readings: list[Reading] = field(default_factory=list)
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


def assemble_answer(evidence: list[Evidence]) -> str:
    """Written by the backend, so that no sentence can appear without its proof."""
    return "\n".join(f"{item.statement} [{item.citation.source_id}]" for item in evidence)


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

    def _git_check(self, source: ContextSource, start: int, end: int):
        chunk = source.chunk
        expected = "\n".join(chunk.content.split("\n")[start - chunk.start_line : end - chunk.start_line + 1])
        return self._verifier(chunk.repo, chunk.commit_sha).check(chunk.path, start, end, expected)

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

        if self.client is None or self.config is None:
            return result

        nonce = new_nonce()
        sources, result.sources_dropped = select_sources(
            question,
            [hit.chunk for hit in retrieved.hits],
            self.config.max_context_chunks,
            self.config.max_input_tokens,
            nonce,
            english,
        )
        result.model = self.config.model
        result.sources_sent = len(sources)
        if not sources:
            result.status = "insufficient"
            result.warnings.append("No source fits within the input token limit, so the model was not called.")
            return result

        for source in sources:
            prompt = build_reading_prompt(question, source, nonce, english)
            reply = self._call(prompt, self.config.max_output_tokens, result)
            reading = verify_reading(reply, source, self._git_check, question)
            result.readings.append(reading)
            if reading.evidence:
                result.evidence.append(reading.evidence)
            elif reading.reason:
                result.rejected.append(f"{source.id}: {reading.reason}")

        if result.evidence:
            result.status = "answered"
            result.answer = assemble_answer(result.evidence)
            if len(result.evidence) == 1:
                result.warnings.append("Only one source supports this answer, so it may be incomplete.")
        elif all(reading.outcome == "invalid" for reading in result.readings):
            result.status = "invalid_response"
            result.warnings.append("The model did not return the required JSON.")
        else:
            result.status = "insufficient"
        return result
