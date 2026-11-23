"""search_code: RepoMind's own search, on one listed repository, with every result checked against Git.

Nothing new is searched for here. SearchEngine finds the chunks, as it does for the web app and the
command line, and leaves out any that may hold a credential. This module decides what may be searched,
and what of the result may be returned.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

from repomind import PIPELINE_VERSION
from repomind.config import Settings
from repomind.embeddings.provider import Embedder, FastEmbedProvider
from repomind.errors import RepoMindError
from repomind.evaluation.citations import CitationCheck, CitationVerifier
from repomind.ingest.filters import find_credential, normalize_source
from repomind.ingest.git_source import GitRepository
from repomind.mcp_server.config import McpConfig
from repomind.mcp_server.database import read_only_store
from repomind.mcp_server.repositories import LOCAL_PATH, NOTICE
from repomind.parsing.credentials import hidden_lines
from repomind.search.engine import SearchConfig, SearchEngine
from repomind.search.keyword import KeywordIndex
from repomind.store.database import ChunkRow, SnapshotInfo

MAX_PREVIEW_LINES = 30
MAX_PREVIEW_CHARACTERS = 2000
# An address of any kind, as a database address can appear in a lower layer's message.
_ADDRESS = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)

Mode = Literal["hybrid", "keyword", "semantic"]


class SearchRefused(Exception):
    """Why a search was not made, or could not finish. The message holds no path, address or secret."""


class SearchResultItem(BaseModel):
    rank: int
    path: str = Field(description="The file, relative to the repository.")
    start_line: int
    end_line: int
    language: str
    symbol: str | None = None
    is_test: bool
    preview: str = Field(description="The lines as the file holds them at the indexed commit, cut to the preview limit.")
    preview_truncated: bool
    role: Literal["search_result"] = Field(
        default="search_result", description="Found by search. It is code to read, not an answer that has been checked."
    )
    exists_in_git: bool = Field(description="True only when these exact lines were found in Git at the indexed commit.")


class SearchResponse(BaseModel):
    repo: str
    commit: str = Field(description="Every result is from this commit.")
    index_version: str
    query: str
    mode: Mode
    results: list[SearchResultItem]
    left_out: int = Field(
        description="Results search found that are not returned, because they did not match Git or may hold a credential."
    )
    warnings: list[str] = Field(default_factory=list)
    notice: str = NOTICE


def clean(text: str) -> str:
    """A lower layer's message, without local folders or addresses."""
    return LOCAL_PATH.sub("<local path>", _ADDRESS.sub("<address>", text))


def _preview(content: str) -> tuple[str, bool]:
    lines = content.split("\n")
    shown = "\n".join(lines[:MAX_PREVIEW_LINES])
    truncated = len(lines) > MAX_PREVIEW_LINES
    if len(shown) > MAX_PREVIEW_CHARACTERS:
        cut = shown[:MAX_PREVIEW_CHARACTERS]
        shown = cut[: cut.rfind("\n")] if "\n" in cut else cut
        truncated = True
    return shown, truncated


def _model_is_here(models_dir: Path, model_name: str) -> bool:
    """Whether the model's files are in the models folder, so that loading it can never mean downloading it."""
    folder = models_dir / f"models--{model_name.replace('/', '--')}"
    return folder.is_dir() and any(folder.rglob("*.onnx"))


class CodeSearch:
    """One per server. It keeps the embedding model and the keyword indexes between calls, behind one lock."""

    def __init__(self, config: McpConfig, embedder_factory: Callable[[], Embedder] | None = None) -> None:
        self.config = config
        self._embedder_factory = embedder_factory
        self._embedder: Embedder | None = None
        self._keyword_indexes: dict[tuple, KeywordIndex] = {}
        self._lock = threading.Lock()

    def _embedding_model(self, mode: str) -> Embedder:
        if self._embedder is not None:
            return self._embedder
        if self._embedder_factory is not None:
            self._embedder = self._embedder_factory()
            return self._embedder
        settings = None if self.config.models_dir else Settings.load()
        models_dir = self.config.models_dir or settings.models_dir
        model = self.config.embedding_model or settings.embedding_model
        if not _model_is_here(models_dir, model):
            raise SearchRefused(
                f"{mode.capitalize()} search needs the local embedding model {model}, and it is not in the models folder. "
                "Nothing is downloaded. Keyword mode works without it."
            )
        self._embedder = FastEmbedProvider(model, models_dir)
        return self._embedder

    def search(self, repo: str, query: str, mode: Mode, limit: int, include_tests: bool | None) -> SearchResponse:
        name = repo.strip().lower()
        if name not in self.config.repositories:
            # Whether it exists in the database is not said: only listed repositories are anyone's business.
            raise SearchRefused("This repository is not one this server may show. list_repositories names those it may.")
        query = query.strip()
        if not query:
            raise SearchRefused("The query is empty.")
        if "\x00" in query:
            raise SearchRefused("The query holds a character that cannot be searched for.")
        with self._lock, read_only_store(self.config) as store:
            snapshots = [snapshot for snapshot in store.active_snapshots([name]) if snapshot.repo == name]
            if not snapshots:
                raise SearchRefused("This repository is not indexed in the database the server reads.")
            snapshot = snapshots[0]
            if not snapshot.is_current:
                raise SearchRefused(
                    f"This repository was indexed by an older version of RepoMind (index version {snapshot.pipeline_version}; "
                    f"this version reads {PIPELINE_VERSION}). It cannot be searched until it is indexed again."
                )
            embedder = None if mode == "keyword" else self._embedding_model(mode)
            # No translator: the server asks no model, and a question is searched as it was written.
            engine = SearchEngine(store, embedder, SearchConfig(include_tests=include_tests), None, keyword_indexes=self._keyword_indexes)
            try:
                found = engine.search(query, mode, [name], limit)
            except RepoMindError as exc:
                text = str(exc)
                if "No embeddings exist" in text:
                    raise SearchRefused(
                        f"This repository has no embeddings for the local model, so {mode} search cannot run here. Keyword mode works."
                    ) from None
                if "embedding model" in text:
                    raise SearchRefused(
                        f"The local embedding model could not be loaded, so {mode} search cannot run. Keyword mode works."
                    ) from None
                raise SearchRefused(f"The search could not run: {clean(text)}") from None
            results, left_out = _GitCheck(snapshot).results(found.hits)
        warnings = [clean(warning) for warning in found.warnings]
        if left_out:
            warnings.append(f"{left_out} result(s) found by search are not returned: they did not match Git, or may hold a credential.")
        return SearchResponse(
            repo=snapshot.repo,
            commit=snapshot.commit_sha,
            index_version=snapshot.pipeline_version,
            query=query,
            mode=mode,
            results=results,
            left_out=left_out,
            warnings=warnings,
        )


class _GitCheck:
    """What may be returned of a search: this repository at its indexed commit, as Git holds it, with no credential.

    The lines are compared with the file in Git, not with RepoMind's database, so a changed row is never
    returned. The file's credential statements are found again from Git as indexing finds them, so a
    row that reaches into one is left out even when no line of it looks like a credential on its own.
    """

    def __init__(self, snapshot: SnapshotInfo) -> None:
        self.snapshot = snapshot
        try:
            self.git = GitRepository(snapshot.source_path)
            resolved = self.git.resolve_commit(snapshot.commit_sha)
        except RepoMindError:
            raise SearchRefused("The local copy of this repository cannot be read, so no result can be checked against Git.") from None
        if resolved != snapshot.commit_sha:
            raise SearchRefused("The indexed commit is not in the local copy of this repository, so no result can be checked against Git.")
        self.verifier = CitationVerifier(self.git, snapshot.commit_sha)
        self._hidden: dict[tuple[str, str | None], frozenset[int]] = {}

    def _hidden_lines(self, path: str, language: str | None) -> frozenset[int]:
        key = (path, language)
        if key not in self._hidden:
            raw = self.git.read_file(self.snapshot.commit_sha, path)
            text = None if raw is None else normalize_source(raw)
            self._hidden[key] = frozenset(hidden_lines(language, text)) if text else frozenset()
        return self._hidden[key]

    def _check(self, chunk: ChunkRow) -> CitationCheck:
        if (chunk.repo, chunk.commit_sha) != (self.snapshot.repo, self.snapshot.commit_sha):
            return CitationCheck(False, "not this repository at its indexed commit")
        if find_credential(chunk.content, chunk.language):
            return CitationCheck(False, "may hold a credential")
        try:
            check = self.verifier.check(chunk.path, chunk.start_line, chunk.end_line, chunk.content)
        except RepoMindError:
            return CitationCheck(False, "Git could not be read")
        if not check.valid:
            return check
        if not self._hidden_lines(chunk.path, chunk.language).isdisjoint(range(chunk.start_line, chunk.end_line + 1)):
            return CitationCheck(False, "reaches into a statement that holds a credential")
        return check

    def results(self, hits) -> tuple[list[SearchResultItem], int]:
        results: list[SearchResultItem] = []
        left_out = 0
        for hit in hits:
            chunk = hit.chunk
            if not self._check(chunk).valid:
                left_out += 1
                continue
            preview, truncated = _preview(chunk.content)
            results.append(
                SearchResultItem(
                    rank=len(results) + 1,
                    path=chunk.path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    language=chunk.language,
                    symbol=chunk.symbol,
                    is_test=bool(chunk.is_test),
                    preview=preview,
                    preview_truncated=truncated,
                    exists_in_git=True,
                )
            )
        return results, left_out
