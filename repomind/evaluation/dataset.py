"""Loads the evaluation set and derives its dev/test split."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from repomind.config import PROJECT_ROOT, get_setting
from repomind.errors import RepoMindError

DEFAULT_DATASET_PATH = PROJECT_ROOT / "eval" / "questions.yaml"
HOLDOUT_UNANSWERABLE_PATH = PROJECT_ROOT / "eval" / "unanswerable_holdout.yaml"

# One question in three is available for tuning; the rest stay held out.
DEV_SHARE = 3


@dataclass(frozen=True)
class GoldSpan:
    path: str
    start_line: int
    end_line: int
    anchor: str


@dataclass(frozen=True)
class Question:
    id: str
    repo: str
    tr: str
    en: str
    gold: tuple[GoldSpan, ...]

    def text(self, language: str) -> str:
        return self.tr if language == "tr" else self.en


@dataclass(frozen=True)
class UnanswerableQuestion:
    id: str
    repo: str
    tr: str
    en: str
    absent_terms: tuple[str, ...]

    def text(self, language: str) -> str:
        return self.tr if language == "tr" else self.en


@dataclass(frozen=True)
class RepositoryPin:
    name: str
    commit: str
    url: str
    path_env: str

    @property
    def local_path(self) -> str | None:
        return get_setting(self.path_env)


@dataclass(frozen=True)
class Dataset:
    repositories: dict[str, RepositoryPin]
    questions: tuple[Question, ...]
    unanswerable: tuple[UnanswerableQuestion, ...]
    sha256: str

    def split(self, name: str) -> tuple[Question, ...]:
        if name == "all":
            return self.questions
        return tuple(q for q in self.questions if split_of(q.id, self.questions) == name)


def split_of(question_id: str, questions: tuple[Question, ...]) -> str:
    """Within each repository, every third question in hash order goes to dev."""
    repo = next(q.repo for q in questions if q.id == question_id)
    ordered = sorted(
        (q.id for q in questions if q.repo == repo),
        key=lambda qid: hashlib.sha256(qid.encode()).hexdigest(),
    )
    return "dev" if ordered.index(question_id) % DEV_SHARE == 0 else "test"


def _unanswerable_questions(raw: dict) -> tuple[UnanswerableQuestion, ...]:
    return tuple(
        UnanswerableQuestion(
            id=entry["id"],
            repo=entry["repo"],
            tr=entry["tr"],
            en=entry["en"],
            absent_terms=tuple(entry["absent_terms"]),
        )
        for entry in raw["unanswerable"]
    )


def load_holdout_unanswerable(path: Path | str = HOLDOUT_UNANSWERABLE_PATH) -> tuple[UnanswerableQuestion, ...]:
    return _unanswerable_questions(yaml.safe_load(Path(path).read_bytes()))


def load_dataset(path: Path | str = DEFAULT_DATASET_PATH) -> Dataset:
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise RepoMindError(f"Cannot read evaluation set: {path}") from exc
    raw = yaml.safe_load(raw_bytes)

    repositories = {
        name: RepositoryPin(name, entry["commit"], entry["url"], entry["path_env"])
        for name, entry in raw["repositories"].items()
    }
    questions = tuple(
        Question(
            id=entry["id"],
            repo=entry["repo"],
            tr=entry["tr"],
            en=entry["en"],
            gold=tuple(GoldSpan(**span) for span in entry["gold"]),
        )
        for entry in raw["questions"]
    )
    unanswerable = _unanswerable_questions(raw)
    identifiers = [q.id for q in questions] + [q.id for q in unanswerable]
    if len(identifiers) != len(set(identifiers)):
        raise RepoMindError("Evaluation set contains duplicate question ids.")
    return Dataset(repositories, questions, unanswerable, hashlib.sha256(raw_bytes).hexdigest())
