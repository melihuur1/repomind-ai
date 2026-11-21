"""What the API sends and accepts. The web client's types are generated from these."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

MAX_QUESTION_CHARACTERS = 1000
MAX_QUERY_CHARACTERS = 500
MAX_PATH_CHARACTERS = 1024
MAX_REPOSITORY_NAME_CHARACTERS = 200


class ApiErrorBody(BaseModel):
    code: str
    message: str


# misconfigured: settings are present and not valid. auth_failed: the key was refused. budget_reached:
# the next request could go over the budget. limited: a quota, spending or rate limit at the provider.
# rejected: the provider did not accept RepoMind's request, or answered with something unreadable.
AiState = Literal[
    "ready",
    "offline",
    "model_missing",
    "not_configured",
    "low_memory",
    "misconfigured",
    "auth_failed",
    "budget_reached",
    "limited",
    "rejected",
]


class AiStatus(BaseModel):
    state: AiState
    model: str | None = None
    detail: str
    remote: bool = Field(
        default=False,
        description="The model runs on another machine: each question and the lines of code chosen for it are sent there.",
    )
    endpoint: str | None = Field(default=None, description="The host of a remote model server. Never a key, and nothing for a local one.")


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool
    ai: AiStatus
    free_memory_gb: float | None
    ui_built: bool


class Repository(BaseModel):
    name: str
    commit: str
    committed_at: datetime
    subject: str
    indexed_at: datetime
    files_in_commit: int
    files_indexed: int
    chunks: int
    symbols: int
    languages: dict[str, int]
    skipped: dict[str, int]
    embedded_chunks: int
    origin_url: str | None = None


class AddRepositoryRequest(BaseModel):
    url: str = Field(min_length=1, max_length=200)


class IndexJob(BaseModel):
    id: int
    repo: str
    url: str
    status: Literal["queued", "running", "done", "failed"]
    phase: str
    done: int
    total: int
    outcome: str | None
    commit_sha: str | None
    error_code: str | None
    message: str | None
    created_at: datetime
    updated_at: datetime


class TreeEntry(BaseModel):
    path: str
    language: str
    line_count: int
    has_syntax_errors: bool
    is_test: bool


class Tree(BaseModel):
    repo: str
    commit: str
    files: list[TreeEntry]
    files_in_commit: int
    skipped: dict[str, int]


class FileContent(BaseModel):
    repo: str
    commit: str
    path: str
    language: str
    lines: list[str]
    hidden_lines: list[int] = Field(description="Lines hidden because they may hold a credential. Their text is empty.")
    has_syntax_errors: bool
    origin_url: str | None = None


SearchMode = Literal["keyword", "semantic", "hybrid"]


class SearchResultItem(BaseModel):
    rank: int
    score: float
    path: str
    language: str
    kind: str
    symbol: str | None
    start_line: int
    end_line: int
    is_test: bool
    preview: str
    reasons: dict


class SearchResponse(BaseModel):
    repo: str
    commit: str
    query: str
    mode: SearchMode
    translated_query: str | None
    results: list[SearchResultItem]
    warnings: list[str]
    seconds: float


class AskRequest(BaseModel):
    repo: str = Field(min_length=1, max_length=MAX_REPOSITORY_NAME_CHARACTERS)
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARACTERS)


SourceRole = Literal["answer", "declaration", "linked_use", "review_candidate", "closest"]
AnswerCheck = Literal["passed", "failed", "not_made"]


class AskSource(BaseModel):
    """Two checks that must not be confused. ``exists_in_git`` does not mean the lines answer the question."""

    role: SourceRole = Field(
        description=(
            "answer: the model chose these lines and they were accepted. "
            "declaration: the model did not choose these lines. They declare a constant that chosen lines use, "
            "and RepoMind shows them so that the value is seen with the lines that name it. "
            "linked_use: the model did not choose these lines. RepoMind added them because they use a constant "
            "whose declaration the model chose. Only with the experimental linked evidence setting. "
            "review_candidate: the model chose these lines and the evidence rule turned them down. "
            "closest: the closest lines found, which nothing judged."
        )
    )
    repo: str
    commit: str
    path: str
    language: str
    symbol: str | None
    start_line: int
    end_line: int
    text: str
    exists_in_git: bool
    answer_check: AnswerCheck


Verdict = Literal["lines_chosen", "not_enough_evidence", "invalid_response", "ai_unavailable"]
ReplyProblem = Literal["empty", "cut_off", "not_json", "no_choice_list", "not_offered", "no_judgement"]


class AskResponse(BaseModel):
    experimental: Literal[True] = True
    verdict: Verdict
    message: str
    reply_problem: ReplyProblem | None = Field(
        default=None,
        description=(
            "With the verdict invalid_response, what was wrong with the model's reply. An invalid reply is "
            "neither an answer nor a refusal by the model, and it gives no source."
        ),
    )
    ai_used: bool
    ai: AiStatus
    model_reading: str | None = Field(description="The model's own sentence. It is unverified, and may be false.")
    model_reading_removed_because: str | None
    sources: list[AskSource]
    translated_query: str | None
    warnings: list[str]
    discarded: list[str]
    model: str | None
    input_tokens: int
    output_tokens: int
    seconds: float
