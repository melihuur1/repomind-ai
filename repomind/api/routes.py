"""Thin routes over the engine. Nothing here searches, ranks or decides what counts as an answer."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from repomind.api import schemas
from repomind.api.errors import ApiError
from repomind.api.services import Services, public_message
from repomind.errors import RepoMindError
from repomind.ingest.filters import is_test_path, normalize_source
from repomind.parsing.credentials import hidden_lines
from repomind.ingest.git_source import GitRepository
from repomind.ingest.github import IndexFailure
from repomind.qa.engine import AnswerCheck, AskResult
from repomind.store.database import ChunkRow, SnapshotInfo, Store

router = APIRouter(prefix="/api")

RepoName = Annotated[str, Query(min_length=1, max_length=schemas.MAX_REPOSITORY_NAME_CHARACTERS)]
FilePath = Annotated[str, Query(min_length=1, max_length=schemas.MAX_PATH_CHARACTERS)]

_FILE_NOT_AVAILABLE = (
    "This file cannot be opened. Only indexed files can be, and hidden files, build output, "
    "binaries and anything that may hold a credential are left out of the index."
)

_MESSAGES = {
    "lines_chosen": "The model chose these lines. They exist at this commit, and that does not prove they answer the question.",
    "not_enough_evidence": "I do not know. The indexed code does not give enough evidence to answer this.",
    "invalid_response": "I do not know. The model did not return a valid response, so none of it is shown.",
    "ai_unavailable": "AI is unavailable, so this is not an AI answer. These are the lines closest to the question.",
}
_VERDICTS = {
    "answered": "lines_chosen",
    "insufficient": "not_enough_evidence",
    "no_results": "not_enough_evidence",
    "invalid_response": "invalid_response",
    "no_llm": "ai_unavailable",
}


def _services(request: Request) -> Services:
    return request.app.state.services


def _snapshot(store: Store, repo: str) -> SnapshotInfo:
    # PostgreSQL rejects a NUL byte outright, so it cannot match anything anyway.
    snapshots = store.active_snapshots([repo]) if "\x00" not in repo else []
    if not snapshots:
        raise ApiError(404, "repository_not_found", "This repository is not indexed.")
    return snapshots[0]


def _repository(store: Store, snapshot: SnapshotInfo, embedding_model: str | None) -> schemas.Repository:
    counts = store.snapshot_counts(snapshot.id)
    embedded = store.count_embedded_chunks(embedding_model, [snapshot.id])[0] if embedding_model else 0
    return schemas.Repository(
        name=snapshot.repo,
        commit=snapshot.commit_sha,
        committed_at=snapshot.committed_at,
        subject=snapshot.commit_subject,
        indexed_at=snapshot.indexed_at,
        files_in_commit=snapshot.stats.get("files_in_commit", counts["files"]),
        files_indexed=counts["files"],
        chunks=counts["chunks"],
        symbols=counts["symbols"],
        languages=snapshot.stats.get("languages", {}),
        skipped=snapshot.stats.get("skipped", {}),
        embedded_chunks=embedded,
        origin_url=snapshot.origin_url,
    )


@router.get("/health", response_model=schemas.Health)
def health(request: Request) -> schemas.Health:
    services = _services(request)
    try:
        with services.store() as store:
            store.connection.execute("SELECT 1")
        database = True
    except RepoMindError:
        database = False
    free = services.free_memory()
    return schemas.Health(
        status="ok" if database else "degraded",
        database=database,
        ai=services.ai_status(),
        free_memory_gb=None if free is None else round(free, 1),
        ui_built=bool(services.web_root and (services.web_root / "index.html").is_file()),
    )


@router.get("/repositories", response_model=list[schemas.Repository])
def list_repositories(request: Request) -> list[schemas.Repository]:
    services = _services(request)
    model = services.embedder.model_name if services.embedder else None
    with services.store() as store:
        return [_repository(store, snapshot, model) for snapshot in store.active_snapshots()]


def _worker(request: Request):
    worker = request.app.state.worker
    if worker is None:
        raise ApiError(503, "indexing_unavailable", "Adding repositories is not available right now.")
    return worker


@router.post("/repositories", response_model=schemas.IndexJob, status_code=202)
def add_repository(request: Request, body: schemas.AddRepositoryRequest) -> schemas.IndexJob:
    """Returns straight away. The page follows the job, and search and browsing keep working meanwhile."""
    try:
        return schemas.IndexJob(**_worker(request).submit(body.url))
    except IndexFailure as exc:
        raise ApiError(422, exc.code, str(exc)) from exc


@router.delete("/repositories", status_code=204)
def remove_repository(request: Request, repo: RepoName) -> Response:
    """Only for a repository added by URL. A local one's files are the user's own, and are never touched."""
    services = _services(request)
    with services.store() as store:
        snapshot = _snapshot(store, repo)
        if not snapshot.origin_url:
            raise ApiError(403, "local_repository", "This repository was indexed from a local folder, and cannot be removed here.")
        store.delete_repository(snapshot.repo)
        store.forget_finished_jobs(snapshot.repo)
    services.keyword_indexes.clear()
    _worker(request).remove_clone(snapshot.repo)
    return Response(status_code=204)


@router.get("/jobs", response_model=list[schemas.IndexJob])
def list_jobs(request: Request) -> list[schemas.IndexJob]:
    with _services(request).store() as store:
        return [schemas.IndexJob(**job) for job in store.recent_jobs()]


@router.get("/jobs/{job_id}", response_model=schemas.IndexJob)
def get_job(request: Request, job_id: int) -> schemas.IndexJob:
    with _services(request).store() as store:
        job = store.job(job_id)
    if job is None:
        raise ApiError(404, "job_not_found", "There is no such job.")
    return schemas.IndexJob(**job)


@router.delete("/jobs/{job_id}", status_code=204)
def dismiss_job(request: Request, job_id: int) -> Response:
    """Clears a finished job's repository from the list. Whatever was indexed stays indexed."""
    with _services(request).store() as store:
        job = store.job(job_id)
        if job is None:
            raise ApiError(404, "job_not_found", "There is no such job.")
        if job["status"] not in ("done", "failed"):
            raise ApiError(409, "job_not_finished", "This job is still running.")
        store.forget_finished_jobs(job["repo"])
    return Response(status_code=204)


@router.post("/jobs/{job_id}/retry", response_model=schemas.IndexJob, status_code=202)
def retry_job(request: Request, job_id: int) -> schemas.IndexJob:
    with _services(request).store() as store:
        job = store.job(job_id)
    if job is None:
        raise ApiError(404, "job_not_found", "There is no such job.")
    if job["status"] != "failed":
        raise ApiError(409, "job_not_failed", "Only a failed job can be retried.")
    return schemas.IndexJob(**_worker(request).submit(job["url"]))


@router.get("/tree", response_model=schemas.Tree)
def tree(request: Request, repo: RepoName) -> schemas.Tree:
    with _services(request).store() as store:
        snapshot = _snapshot(store, repo)
        files = store.indexed_files(snapshot.id)
    return schemas.Tree(
        repo=snapshot.repo,
        commit=snapshot.commit_sha,
        files=[schemas.TreeEntry(**entry, is_test=is_test_path(entry["path"])) for entry in files],
        files_in_commit=snapshot.stats.get("files_in_commit", len(files)),
        skipped=snapshot.stats.get("skipped", {}),
    )


@router.get("/file", response_model=schemas.FileContent)
def file_content(request: Request, repo: RepoName, path: FilePath, commit: str | None = Query(default=None, max_length=40)) -> schemas.FileContent:
    """The path is only ever compared with the database's list, and the content only ever comes from a Git object."""
    with _services(request).store() as store:
        snapshot = _snapshot(store, repo)
        entry = store.indexed_file(snapshot.id, path) if "\x00" not in path else None
    if commit is not None and not snapshot.commit_sha.startswith(commit.lower()):
        raise ApiError(
            409,
            "commit_not_indexed",
            f"This link points at commit {commit[:12]}, and the repository is now indexed at {snapshot.commit_sha[:12]}.",
        )
    if entry is None:
        raise ApiError(404, "file_not_available", _FILE_NOT_AVAILABLE)
    try:
        raw = GitRepository(snapshot.source_path).read_file(snapshot.commit_sha, path)
    except RepoMindError as exc:
        raise ApiError(503, "source_unavailable", "The local copy of this repository cannot be read.") from exc
    text = normalize_source(raw) if raw is not None else None
    if text is None:
        raise ApiError(404, "file_not_available", _FILE_NOT_AVAILABLE)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    hidden = hidden_lines(entry["language"], text)
    for number in hidden:
        lines[number - 1] = ""
    return schemas.FileContent(
        repo=snapshot.repo,
        commit=snapshot.commit_sha,
        path=path,
        language=entry["language"],
        lines=lines,
        hidden_lines=hidden,
        has_syntax_errors=entry["has_syntax_errors"],
        origin_url=snapshot.origin_url,
    )


def _search_item(hit) -> schemas.SearchResultItem:
    chunk: ChunkRow = hit.chunk
    return schemas.SearchResultItem(
        rank=hit.rank,
        score=float(hit.score),
        path=chunk.path,
        language=chunk.language,
        kind=chunk.kind,
        symbol=chunk.symbol,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        is_test=chunk.is_test,
        preview=chunk.content,
        reasons=hit.explanation,
    )


@router.get("/search", response_model=schemas.SearchResponse)
def search(
    request: Request,
    repo: RepoName,
    q: Annotated[str, Query(min_length=1, max_length=schemas.MAX_QUERY_CHARACTERS)],
    mode: schemas.SearchMode = "hybrid",
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    include_tests: bool | None = None,
) -> schemas.SearchResponse:
    services = _services(request)
    started = time.perf_counter()
    with services.store() as store:
        snapshot = _snapshot(store, repo)
        try:
            found = services.search_engine(store, include_tests).search(q, mode, [repo], limit)
        except RepoMindError as exc:
            raise ApiError(400, "search_failed", public_message(str(exc))) from exc
    return schemas.SearchResponse(
        repo=repo,
        commit=snapshot.commit_sha,
        query=q,
        mode=mode,
        translated_query=found.translated_query,
        results=[_search_item(hit) for hit in found.hits],
        warnings=[public_message(warning) for warning in found.warnings],
        seconds=round(time.perf_counter() - started, 3),
    )


def _sources(result: AskResult) -> list[schemas.AskSource]:
    """What the model chose first, then a line added beside it, anything to review, and the closest lines.

    Each role keeps the engine's order. A line RepoMind added because a chosen declaration is used
    there has a role of its own, so that it is never shown as the model's choice.
    """
    roles = {
        AnswerCheck.PASSED: "answer",
        AnswerCheck.FAILED: "review_candidate",
        AnswerCheck.NOT_MADE: "closest",
    }
    order = ("answer", "linked_use", "review_candidate", "closest")

    def role(source) -> str:
        return "linked_use" if source.added_by_link else roles[source.answer_check]

    ordered = sorted(result.sources(), key=lambda source: order.index(role(source)))
    return [
        schemas.AskSource(
            role=role(source),
            repo=source.passage.chunk.repo,
            commit=source.passage.chunk.commit_sha,
            path=source.passage.path,
            language=source.passage.chunk.language,
            symbol=source.passage.chunk.symbol,
            start_line=source.passage.start_line,
            end_line=source.passage.end_line,
            text=source.passage.text,
            exists_in_git=source.exists_in_git,
            answer_check=source.answer_check.value,
        )
        for source in ordered
    ]


@router.post("/ask", response_model=schemas.AskResponse)
def ask(request: Request, body: schemas.AskRequest) -> schemas.AskResponse:
    services = _services(request)
    question = body.question.strip()
    if not question:
        raise ApiError(422, "empty_question", "The question is empty.")
    # One generation at a time, as the local model server is configured. A second one would only queue there.
    if not services.ask_lock.acquire(blocking=False):
        raise ApiError(429, "busy", "Another question is being answered. Try again in a moment.", headers={"Retry-After": "10"})
    started = time.perf_counter()
    try:
        with services.store() as store:
            _snapshot(store, body.repo)
            try:
                result, ai, used = services.ask(store, body.repo, question)
            except RepoMindError as exc:
                raise ApiError(400, "ask_failed", public_message(str(exc))) from exc
    finally:
        services.ask_lock.release()
    verdict = _VERDICTS[result.status]
    return schemas.AskResponse(
        verdict=verdict,
        message=_MESSAGES[verdict],
        reply_problem=result.reply_problem,
        ai_used=used,
        ai=ai,
        model_reading=result.note or None,
        model_reading_removed_because=result.note_problem,
        sources=_sources(result),
        translated_query=result.translated_query,
        warnings=[public_message(warning) for warning in result.warnings],
        discarded=[public_message(item) for item in result.rejected],
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        seconds=round(time.perf_counter() - started, 2),
    )
