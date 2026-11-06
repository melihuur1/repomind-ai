"""The three retrieval methods behind one interface."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from repomind.embeddings.provider import Embedder
from repomind.errors import RepoMindError
from repomind.search.glossary import repository_terms
from repomind.search.hybrid import reciprocal_rank_fusion
from repomind.search.keyword import BM25Config, KeywordIndex
from repomind.search.tokenizer import fold
from repomind.search.translation import QueryTranslator, looks_turkish
from repomind.store.database import ChunkRow, Store

MODES = ("keyword", "semantic", "hybrid")

_ASKS_ABOUT_TESTS = re.compile(r"\b(tests?|testing|test[a-z]+|specs?|e2e)\b")


def asks_about_tests(query: str) -> bool:
    return bool(_ASKS_ABOUT_TESTS.search(fold(query)))


@dataclass(frozen=True)
class SearchConfig:
    # Low, so that a chunk ranked near the top of one list beats one ranked 20th in all of them. A
    # short configuration line is often found by only one wording. See docs/evaluation.md.
    rrf_k: int = 10
    candidates: int = 50
    # None: leave tests out unless the query asks about them. Tests repeat the
    # vocabulary of the code they exercise, and otherwise crowd out the implementation.
    include_tests: bool | None = None
    bm25: BM25Config = BM25Config()


@dataclass(frozen=True)
class SearchHit:
    chunk: ChunkRow
    rank: int
    score: float
    explanation: dict


@dataclass
class SearchResult:
    query: str
    mode: str
    hits: list[SearchHit]
    translated_query: str | None = None
    warnings: list[str] = field(default_factory=list)
    glossary_terms: list[str] = field(default_factory=list)


class SearchEngine:
    def __init__(
        self,
        store: Store,
        embedder: Embedder | None = None,
        config: SearchConfig = SearchConfig(),
        translator: QueryTranslator | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.config = config
        self.translator = translator
        self._indexes: dict[tuple, KeywordIndex] = {}
        self._translations: dict[str, str | None] = {}

    def _snapshot_ids(self, repos: list[str] | None) -> list[int]:
        snapshots = self.store.active_snapshots(repos)
        if repos:
            missing = set(repos) - {s.repo for s in snapshots}
            if missing:
                known = ", ".join(s.repo for s in self.store.active_snapshots()) or "none"
                raise RepoMindError(
                    f"Repository not indexed: {', '.join(sorted(missing))}.",
                    hint=f"Indexed repositories: {known}. Add one with `python -m repomind index --path <path>`.",
                )
        if not snapshots:
            raise RepoMindError(
                "Nothing has been indexed yet.",
                hint='Run `python -m repomind index --path "<repository>"` first.',
            )
        return [s.id for s in snapshots]

    def _tests_included(self, query: str, translated: str | None) -> bool:
        if self.config.include_tests is None:
            return asks_about_tests(query) or (translated is not None and asks_about_tests(translated))
        return self.config.include_tests

    def local_translation(self, query: str, warnings: list[str]) -> str | None:
        """The local translator's wording, which needs no LLM. None for a query that is not Turkish."""
        return self._translate(query, warnings)

    def _translate(self, query: str, warnings: list[str]) -> str | None:
        if self.translator is None or not looks_turkish(query):
            return None
        if query not in self._translations:
            try:
                self._translations[query] = self.translator.translate(query)
            except RepoMindError as exc:
                warnings.append(f"Continuing without query translation. {exc}")
                self._translations[query] = None
        return self._translations[query]

    def _keyword_index(self, snapshot_ids: list[int], include_tests: bool) -> KeywordIndex:
        key = (tuple(snapshot_ids), include_tests)
        if key not in self._indexes:
            chunks = self.store.load_chunks(snapshot_ids, include_tests)
            self._indexes[key] = KeywordIndex(chunks, self.config.bm25)
        return self._indexes[key]

    def vocabulary(self, query: str, translated: str | None, repos: list[str] | None = None):
        """Every term in the code this query would search, for checking what it never mentions."""
        index = self._keyword_index(self._snapshot_ids(repos), self._tests_included(query, translated))
        return index.postings

    def _keyword_hits(
        self, query: str, wordings: Sequence[str], snapshot_ids: list[int], include_tests: bool, limit: int
    ) -> list[SearchHit]:
        # Identifiers usually survive in the original text, so every version counts.
        text = " ".join([*wordings, query])
        return [
            SearchHit(
                chunk=match.chunk,
                rank=rank,
                score=match.score,
                explanation={"matched_terms": {t: round(s, 3) for t, s in match.matched_terms.items()}},
            )
            for rank, match in enumerate(self._keyword_index(snapshot_ids, include_tests).search(text, limit), start=1)
        ]

    def _semantic_hits(self, query: str, snapshot_ids: list[int], include_tests: bool, limit: int) -> list[SearchHit]:
        matches = self.store.semantic_search(
            self.embedder.embed_query(query), self.embedder.model_name, snapshot_ids, limit, include_tests
        )
        return [
            SearchHit(chunk=chunk, rank=rank, score=similarity, explanation={"cosine_similarity": round(similarity, 4)})
            for rank, (chunk, similarity) in enumerate(matches, start=1)
        ]

    def _require_embeddings(self, snapshot_ids: list[int], warnings: list[str]) -> None:
        if self.embedder is None:
            raise RepoMindError("Semantic search needs an embedding model, and none is loaded.")
        embedded, total = self.store.count_embedded_chunks(self.embedder.model_name, snapshot_ids)
        if embedded == 0:
            raise RepoMindError(
                f"No embeddings exist for model {self.embedder.model_name}.",
                hint=f"Run `python -m repomind embed --model {self.embedder.model_name}`.",
            )
        if embedded < total:
            warnings.append(f"Only {embedded} of {total} chunks have embeddings for {self.embedder.model_name}.")

    def _fuse(self, lists: dict[str, list[SearchHit]], limit: int) -> list[SearchHit]:
        """Reciprocal Rank Fusion of ranked lists, keeping each hit's reasons."""
        chunks: dict[int, ChunkRow] = {}
        details: dict[int, dict] = {}
        for hits in lists.values():
            for hit in hits:
                chunks[hit.chunk.id] = hit.chunk
                for key, value in hit.explanation.items():
                    details.setdefault(hit.chunk.id, {}).setdefault(key, value)
        fused = reciprocal_rank_fusion(
            {name: [hit.chunk.id for hit in hits] for name, hits in lists.items()}, k=self.config.rrf_k
        )
        return [
            SearchHit(chunks[item.key], rank, item.score, {"ranks": item.ranks, **details.get(item.key, {})})
            for rank, item in enumerate(fused[:limit], start=1)
        ]

    def _with_glossary_seats(
        self, hits: list[SearchHit], terms: Sequence[str], snapshot_ids: list[int], include_tests: bool, seats: int
    ) -> list[SearchHit]:
        """Adds the best chunks for the glossary's terms after the others.

        They get seats and not a vote. A fifth list hardly moves a chunk that only it finds, and
        voting also pushed out chunks that were already there.
        """
        have = {hit.chunk.id for hit in hits}
        found = self._keyword_hits(" ".join(terms), [], snapshot_ids, include_tests, seats)
        extra = [hit for hit in found if hit.chunk.id not in have]
        return [
            *hits,
            *(
                SearchHit(hit.chunk, rank, hit.score, {"ranks": {"glossary": hit.rank}, **hit.explanation})
                for rank, hit in enumerate(extra, start=len(hits) + 1)
            ),
        ]

    def check_repositories(self, repos: list[str] | None) -> None:
        """Raises the same helpful error a search would, without doing any work."""
        self._snapshot_ids(repos)

    def search(
        self,
        query: str,
        mode: str = "hybrid",
        repos: list[str] | None = None,
        limit: int = 5,
        translated: str | None = None,
        more_wordings: Sequence[str] = (),
        glossary_seats: int = 0,
    ) -> SearchResult:
        """``translated`` is an English wording the caller already has; otherwise the translator makes one.

        ``more_wordings`` are other wordings of the same question, each searched on its own, so
        that one wrong translation cannot decide alone what search finds. ``glossary_seats`` adds up
        to that many chunks beyond ``limit``, for a Turkish question, found by what its words can mean.
        """
        if not query.strip():
            raise RepoMindError("The search query is empty.")
        if mode not in MODES:
            raise RepoMindError(f"Unknown search mode: {mode}", hint=f"Choose one of: {', '.join(MODES)}.")

        warnings: list[str] = []
        snapshot_ids = self._snapshot_ids(repos)
        translated = translated or self._translate(query, warnings)
        include_tests = self._tests_included(query, translated)
        depth = max(limit, self.config.candidates)
        wordings = list(dict.fromkeys(w for w in (translated, *more_wordings) if w))

        lists: dict[str, list[SearchHit]] = {}
        if mode in ("keyword", "hybrid"):
            lists["keyword"] = self._keyword_hits(query, wordings, snapshot_ids, include_tests, depth)
        if mode in ("semantic", "hybrid"):
            self._require_embeddings(snapshot_ids, warnings)
            lists["semantic"] = self._semantic_hits(query, snapshot_ids, include_tests, depth)
            for number, wording in enumerate(wordings, start=1):
                name = "semantic_translated" if number == 1 else f"semantic_translated_{number}"
                lists[name] = self._semantic_hits(wording, snapshot_ids, include_tests, depth)

        if len(lists) == 1:
            hits = next(iter(lists.values()))[:limit]
        else:
            hits = self._fuse(lists, limit)
        terms: list[str] = []
        if glossary_seats and looks_turkish(query):
            terms = repository_terms(query, self._keyword_index(snapshot_ids, include_tests).postings)
            if terms:
                hits = self._with_glossary_seats(hits, terms, snapshot_ids, include_tests, glossary_seats)
        return SearchResult(query, mode, hits, translated, warnings, terms)
