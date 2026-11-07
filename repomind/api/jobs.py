"""Indexes repositories one at a time on a background thread, so that the web page never waits for it.

A thread and a table are all this needs. There is one user on one machine, and a job that a
restart interrupts is simply marked as failed, and can be retried.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from pathlib import Path

from repomind import PIPELINE_VERSION
from repomind.api.errors import ApiError
from repomind.api.services import Services, public_message
from repomind.errors import RepoMindError
from repomind.indexing import index_repository
from repomind.ingest.filters import classify_path
from repomind.ingest.git_source import GitRepository
from repomind.ingest.github import (
    GitHubRepo,
    IndexFailure,
    IndexLimits,
    RepoFacts,
    RepositorySource,
    parse_github_url,
    remove_tree,
)
from repomind.store.database import Store

logger = logging.getLogger("repomind.jobs")

MAX_WAITING_JOBS = 5
INTERRUPTED = "RepoMind restarted while this was running. You can retry it."


class IndexingWorker:
    def __init__(self, services: Services, source: RepositorySource, data_dir: Path, limits: IndexLimits) -> None:
        self.services = services
        self.source = source
        self.data_dir = data_dir
        self.limits = limits
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self.services.store() as store:
            store.fail_unfinished_jobs(INTERRUPTED)
        self._thread = threading.Thread(target=self._loop, name="repomind-indexing", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread:
            self._queue.put(None)
            self._thread.join(timeout=5)

    def submit(self, url: str) -> dict:
        repo = parse_github_url(url)
        with self.services.store() as store:
            existing = store.active_job(repo.local_name)
            if existing:
                return existing
            if store.count_active_jobs() >= MAX_WAITING_JOBS:
                raise ApiError(429, "too_many_jobs", "Several repositories are already waiting. Try again when they finish.", headers={"Retry-After": "60"})
            job, is_new = store.create_job(repo.local_name, repo.web_url)
        if is_new:
            self._queue.put(job["id"])
        return job

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            try:
                with self.services.store() as store:
                    self._run(store, job_id)
            except IndexFailure as exc:
                self._fail(job_id, exc.code, str(exc))
            except RepoMindError as exc:
                self._fail(job_id, "index_failed", public_message(str(exc)))
            except Exception:
                logger.exception("Indexing failed unexpectedly")
                self._fail(job_id, "internal_error", "Something went wrong while indexing this repository.")

    def _fail(self, job_id: int, code: str, message: str) -> None:
        try:
            with self.services.store() as store:
                store.update_job(job_id, status="failed", phase="failed", error_code=code, message=message)
        except RepoMindError:
            logger.exception("Could not record a failed job")

    def _run(self, store: Store, job_id: int) -> None:
        job = store.job(job_id)
        started = time.monotonic()
        free = self.services.free_memory()
        if free is not None and free < self.services.limits.min_free_memory_gb:
            raise IndexFailure("low_memory", f"Only {free:.1f} GB of memory is free, so indexing did not start.")

        store.update_job(job_id, status="running", phase="checking")
        facts = self.source.lookup(parse_github_url(job["url"]), self.limits)
        name = facts.repo.local_name
        head = self.source.head_commit(facts)
        store.update_job(job_id, repo=name, url=facts.repo.web_url, commit_sha=head)

        embedder = self.services.embedder
        current = next(iter(store.active_snapshots([name])), None)
        if current and current.commit_sha == head and store.find_snapshot_is_current(current.id, PIPELINE_VERSION):
            embedded, total = store.count_embedded_chunks(embedder.model_name, [current.id]) if embedder else (0, 0)
            if embedded == total:
                store.update_job(job_id, status="done", phase="done", outcome="unchanged", message="Already indexed at this commit, so nothing was repeated.")
                return

        target = self._clone(job_id, store, facts, head)
        self._check_contents(target, head)

        def progress(done: int, total: int) -> None:
            if time.monotonic() - started > self.limits.total_seconds:
                raise IndexFailure("timeout", f"Indexing took longer than {self.limits.total_seconds // 60} minutes, so it was stopped. Retrying continues from where it stopped.")
            store.update_job(job_id, phase="embedding", done=done, total=total)

        store.update_job(job_id, phase="indexing")
        report = index_repository(store, str(target), embedder, name=name, ref=head, progress=progress)
        store.set_origin_url(name, facts.repo.web_url)
        self.services.keyword_indexes.clear()
        self._remove_older_clones(facts.repo, head)
        store.update_job(
            job_id,
            status="done",
            phase="done",
            outcome="unchanged" if report.unchanged else "indexed",
            message=f"Indexed {report.files_indexed} of {report.files_in_commit} files.",
        )

    def _folder(self, repo: GitHubRepo) -> Path:
        return self.data_dir / "repos" / f"{repo.owner}__{repo.name}".lower()

    def _clone(self, job_id: int, store: Store, facts: RepoFacts, head: str) -> Path:
        folder = self._folder(facts.repo)
        target = folder / head
        if (target / ".git").is_dir():
            return target
        store.update_job(job_id, phase="cloning")
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f"tmp-{uuid.uuid4().hex}"
        try:
            self.source.clone(facts, temporary, self.limits)
            commit = GitRepository(temporary).resolve_commit("HEAD")
            if commit != head:
                raise IndexFailure("clone_failed", "The repository changed during cloning. Retry to index its latest commit.")
            temporary.rename(target)
        finally:
            remove_tree(temporary)
        return target

    def _check_contents(self, target: Path, head: str) -> None:
        entries = GitRepository(target).list_files(head)
        if len(entries) > self.limits.max_files:
            raise IndexFailure("too_many_files", f"This repository has {len(entries):,} files, and the limit is {self.limits.max_files:,}.")
        indexable = [entry for entry in entries if classify_path(entry.path, entry.size).included]
        if not indexable:
            raise IndexFailure("no_source", "This repository has no files RepoMind can read. It supports Java, TypeScript, JavaScript, SQL and YAML.")
        if len(indexable) > self.limits.max_indexable_files:
            raise IndexFailure("too_many_files", f"This repository has {len(indexable):,} source files, and the limit is {self.limits.max_indexable_files:,}.")
        size_mb = sum(entry.size for entry in indexable) / (1024 * 1024)
        if size_mb > self.limits.max_source_mb:
            raise IndexFailure("too_large", f"This repository has {size_mb:.0f} MB of source code, and the limit is {self.limits.max_source_mb} MB.")

    def _remove_older_clones(self, repo: GitHubRepo, keep: str) -> None:
        for child in self._folder(repo).iterdir():
            if child.name != keep:
                try:
                    remove_tree(child)
                except OSError:
                    logger.warning("Could not remove an old clone of %s", repo.full_name)

    def remove_clone(self, name: str) -> None:
        owner, _, repository = name.partition("/")
        remove_tree(self.data_dir / "repos" / f"{owner}__{repository}")
