"""The three retrieval methods behind one interface."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from repomind.embeddings.provider import Embedder
from repomind.errors import RepoMindError
from repomind.search.hybrid import reciprocal_rank_fusion
from repomind.search.keyword import BM25Config, KeywordIndex
from repomind.search.rerank import Reranker, rerank_text
from repomind.search.tokenizer import fold
from repomind.search.translation import QueryTranslator, looks_turkish
from repomind.store.database import ChunkRow, Store

MODES = ("keyword", "semantic", "hybrid", "reranked")

_ASKS_ABOUT_TESTS = re.compile(r"\b(tests?|testing|test[a-z]+|specs?|e2e)\b")


def asks_about_tests(query: str) -> bool:
    return bool(_ASKS_ABOUT_TESTS.search(fold(query)))


@dataclass(frozen=True)
class SearchConfig:
    rrf_k: int = 60
    candidates: int = 50
    # None: leave tests out unless the query asks about them. Tests repeat the
    # vocabulary of the code they exercise, and otherwise crowd out the implementation.
    include_tests: bool | None = None
    # "pure" orders candidates by the cross-encoder alone; "rrf" fuses that order with
    # the hybrid order, so that one bad cross-encoder score cannot bury a strong candidate.
    rerank_blend: str = "pure"
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


class SearchEngine:
    def __init__(
        self,
        store: Store,
        embedder: Embedder | None = None,
        config: SearchConfig = SearchConfig(),
        translator: QueryTranslator | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.config = config
        self.translator = translator
        self.reranker = reranker
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
        self, query: str, translated: str | None, snapshot_ids: list[int], include_tests: bool, limit: int
    ) -> list[SearchHit]:
        # Identifiers usually survive in the original text, so both versions count.
        text = f"{translated} {query}" if translated else query
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

    def search(self, query: str, mode: str = "hybrid", repos: list[str] | None = None, limit: int = 5) -> SearchResult:
        if not query.strip():
            raise RepoMindError("The search query is empty.")
        if mode not in MODES:
            raise RepoMindError(f"Unknown search mode: {mode}", hint=f"Choose one of: {', '.join(MODES)}.")

        warnings: list[str] = []
        snapshot_ids = self._snapshot_ids(repos)
        translated = self._translate(query, warnings)
        include_tests = self._tests_included(query, translated)
        depth = max(limit, self.config.candidates)

        if mode == "reranked" and self.reranker is None:
            raise RepoMindError("Reranked search needs a reranker model, and none is loaded.")

        lists: dict[str, list[SearchHit]] = {}
        if mode in ("keyword", "hybrid", "reranked"):
            lists["keyword"] = self._keyword_hits(query, translated, snapshot_ids, include_tests, depth)
        if mode in ("semantic", "hybrid", "reranked"):
            self._require_embeddings(snapshot_ids, warnings)
            lists["semantic"] = self._semantic_hits(query, snapshot_ids, include_tests, depth)
            if translated:
                lists["semantic_translated"] = self._semantic_hits(translated, snapshot_ids, include_tests, depth)

        if mode == "reranked":
            hits = self._rerank(translated or query, self._fuse(lists, depth), limit)
        elif len(lists) == 1:
            hits = next(iter(lists.values()))[:limit]
        else:
            hits = self._fuse(lists, limit)
        return SearchResult(query, mode, hits, translated, warnings)

    def _rerank(self, query: str, candidates: list[SearchHit], limit: int) -> list[SearchHit]:
        if not candidates:
            return []
        scores = self.reranker.score(query, [rerank_text(hit.chunk) for hit in candidates])
        by_score = sorted(range(len(candidates)), key=lambda i: (-scores[i], i))
        if self.config.rerank_blend == "rrf":
            fused = reciprocal_rank_fusion(
                {"retrieval": list(range(len(candidates))), "cross_encoder": by_score}, k=self.config.rrf_k
            )
            order = [item.key for item in fused]
        else:
            order = by_score
        return [
            SearchHit(
                candidates[i].chunk,
                rank,
                scores[i],
                {**candidates[i].explanation, "rerank_score": round(scores[i], 4), "retrieval_rank": i + 1},
            )
            for rank, i in enumerate(order[:limit], start=1)
        ]
