"""Retrieve, build a bounded prompt, call the model if there is one, verify the result."""

from __future__ import annotations

from dataclasses import dataclass, field

from repomind.errors import RepoMindError
from repomind.evaluation.citations import CitationVerifier
from repomind.ingest.git_source import GitRepository
from repomind.qa.llm import LLMClient, LLMConfig, cost_usd
from repomind.qa.prompt import ContextSource, PreparedPrompt, build_prompt
from repomind.qa.verification import VerifiedCitation, verify_answer
from repomind.search.engine import SearchEngine, SearchHit
from repomind.store.database import Store

# If the endpoint reports far fewer input tokens than we sent, it truncated the prompt
# silently (for example Ollama's small default context) and sources were never seen.
_TRUNCATION_RATIO = 0.5


@dataclass
class AskResult:
    status: str  # answered | insufficient | invalid_response | no_llm | no_results
    question: str
    answer: str = ""
    citations: list[VerifiedCitation] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hits: list[SearchHit] = field(default_factory=list)
    translated_query: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    tokens_are_estimated: bool = False
    context_chars: int = 0
    sources_sent: int = 0
    sources_dropped: int = 0
    cost_usd: float = 0.0


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

    def ask(self, question: str, repos: list[str] | None = None) -> AskResult:
        chunk_limit = self.config.max_context_chunks if self.config else 8
        retrieved = self.search.search(question, "hybrid", repos, chunk_limit)
        result = AskResult(
            status="no_llm",
            question=question,
            hits=retrieved.hits,
            translated_query=retrieved.translated_query,
            warnings=list(retrieved.warnings),
        )
        if not retrieved.hits:
            result.status = "no_results"
            return result
        if self.client is None or self.config is None:
            return result

        prompt = build_prompt(
            question,
            [hit.chunk for hit in retrieved.hits],
            self.config.max_context_chunks,
            self.config.max_input_tokens,
        )
        result.model = self.config.model
        result.context_chars = prompt.context_chars
        result.sources_sent = len(prompt.sources)
        result.sources_dropped = prompt.dropped_chunks
        if not prompt.sources:
            result.status = "insufficient"
            result.warnings.append("No source fits within the input token limit, so the model was not called.")
            return result

        self._enforce_budget(prompt)
        response = self.client.complete(prompt.system, prompt.user, self.config.max_output_tokens)

        result.tokens_are_estimated = response.input_tokens is None or response.output_tokens is None
        result.input_tokens = response.input_tokens if response.input_tokens is not None else prompt.estimated_input_tokens
        result.output_tokens = (
            response.output_tokens if response.output_tokens is not None else max(1, len(response.text) // 3)
        )
        result.cost_usd = cost_usd(self.config, result.input_tokens, result.output_tokens)
        self.store.record_llm_usage(
            self.config.base_url, self.config.model, result.input_tokens, result.output_tokens, result.cost_usd
        )
        if response.input_tokens is not None and response.input_tokens < _TRUNCATION_RATIO * prompt.estimated_input_tokens:
            result.warnings.append(
                f"The endpoint reports {response.input_tokens} input tokens for a prompt of about "
                f"{prompt.estimated_input_tokens}: it may have truncated the sources."
            )

        verified = verify_answer(response.text, prompt.sources, self._git_check)
        result.status = verified.status
        result.answer = verified.answer
        result.citations = verified.citations
        result.rejected = verified.rejected
        result.warnings.extend(verified.warnings)
        return result
