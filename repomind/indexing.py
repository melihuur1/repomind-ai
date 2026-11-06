"""Turns one commit of a repository into searchable chunks and vectors."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Sequence

from repomind import PIPELINE_VERSION
from repomind.embeddings.provider import Embedder
from repomind.embeddings.text import embedding_text, text_hash
from repomind.ingest.filters import classify_content, classify_path
from repomind.ingest.git_source import GitRepository
from repomind.parsing.chunker import parse_source
from repomind.parsing.models import ParsedFile
from repomind.store.database import Store

EMBEDDING_BATCH = 64


@dataclass
class EmbeddingReport:
    model: str
    computed: int = 0
    reused: int = 0


@dataclass
class IndexReport:
    repo: str
    commit: str
    subject: str
    unchanged: bool
    files_in_commit: int
    files_indexed: int
    chunks: int
    symbols: int
    imports: int
    skipped: dict[str, int] = field(default_factory=dict)
    skipped_for_secrets: list[str] = field(default_factory=list)
    files_with_syntax_errors: list[str] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)
    embedding: EmbeddingReport | None = None
    seconds: float = 0.0


def _read_and_parse(git: GitRepository, commit: str) -> tuple[list[tuple[ParsedFile, str]], dict]:
    entries = git.list_files(commit)
    skipped: Counter[str] = Counter()
    languages_by_path: dict[str, str] = {}
    selected = []
    for entry in entries:
        decision = classify_path(entry.path, entry.size)
        if decision.included:
            selected.append(entry)
            languages_by_path[entry.path] = decision.language
        else:
            skipped[decision.skip_reason] += 1

    parsed: list[tuple[ParsedFile, str]] = []
    secret_paths: list[str] = []
    for entry, raw in git.read_blobs(selected):
        text, reason = classify_content(raw)
        if text is None:
            skipped[reason] += 1
            if reason == "contains_secret":
                secret_paths.append(entry.path)
            continue
        parsed.append((parse_source(entry.path, languages_by_path[entry.path], text), entry.sha))

    stats = {
        "files_in_commit": len(entries),
        "files_indexed": len(parsed),
        "skipped": dict(sorted(skipped.items())),
        "skipped_for_secrets": secret_paths,
        "files_with_syntax_errors": [p.path for p, _ in parsed if p.has_syntax_errors],
        "languages": dict(sorted(Counter(p.language for p, _ in parsed).items())),
        "chunks": sum(len(p.chunks) for p, _ in parsed),
        "symbols": sum(len(p.symbols) for p, _ in parsed),
        "imports": sum(len(p.imports) for p, _ in parsed),
        "test_chunks": sum(c.is_test for p, _ in parsed for c in p.chunks),
    }
    return parsed, stats


def embed_missing(
    store: Store,
    embedder: Embedder,
    snapshot_ids: Sequence[int],
    progress: Callable[[int, int], None] | None = None,
) -> EmbeddingReport:
    """Compute vectors only for texts the cache has not seen with this model."""
    missing = store.chunks_missing_embedding(embedder.model_name, snapshot_ids)
    embedded, total = store.count_embedded_chunks(embedder.model_name, snapshot_ids)
    report = EmbeddingReport(model=embedder.model_name, computed=len(missing), reused=embedded)
    for start in range(0, len(missing), EMBEDDING_BATCH):
        batch = missing[start : start + EMBEDDING_BATCH]
        texts = [
            embedding_text(c.path, c.kind, c.symbol, c.signature, c.content, c.part_index) for c in batch
        ]
        vectors = embedder.embed_passages(texts)
        store.save_embeddings(embedder.model_name, [(c.embed_text_hash, v) for c, v in zip(batch, vectors)])
        if progress:
            progress(min(start + EMBEDDING_BATCH, len(missing)), len(missing))
    return report


def index_repository(
    store: Store,
    path: str,
    embedder: Embedder | None = None,
    name: str | None = None,
    ref: str = "HEAD",
    force: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> IndexReport:
    started = time.perf_counter()
    git = GitRepository(path)
    commit = git.resolve_commit(ref)
    repo_name = (name or git.default_name()).lower()
    info = git.commit_info(commit)
    repository_id = store.upsert_repository(repo_name, str(git.path))

    snapshot_id = None if force else store.find_snapshot(repository_id, commit, PIPELINE_VERSION)
    unchanged = snapshot_id is not None
    if unchanged:
        stats = next(s.stats for s in store.active_snapshots([repo_name]) if s.id == snapshot_id)
    else:
        parsed, stats = _read_and_parse(git, commit)
        hashes = {
            id(chunk): text_hash(
                embedding_text(chunk.path, chunk.kind, chunk.symbol, chunk.signature, chunk.content, chunk.part_index)
            )
            for parsed_file, _ in parsed
            for chunk in parsed_file.chunks
        }
        snapshot_id = store.replace_snapshot(
            repository_id, commit, PIPELINE_VERSION, info.committed_at, info.subject, stats, parsed, hashes
        )

    embedding = embed_missing(store, embedder, [snapshot_id], progress) if embedder else None
    return IndexReport(
        repo=repo_name,
        commit=commit,
        subject=info.subject,
        unchanged=unchanged,
        files_in_commit=stats["files_in_commit"],
        files_indexed=stats["files_indexed"],
        chunks=stats["chunks"],
        symbols=stats["symbols"],
        imports=stats["imports"],
        skipped=stats["skipped"],
        skipped_for_secrets=stats["skipped_for_secrets"],
        files_with_syntax_errors=stats["files_with_syntax_errors"],
        languages=stats["languages"],
        embedding=embedding,
        seconds=time.perf_counter() - started,
    )
