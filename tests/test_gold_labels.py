"""The evaluation sets must be factually true about the code at the pinned commits."""

from __future__ import annotations

import re

import pytest

from repomind.evaluation.dataset import V2_DATASET_PATH, load_dataset, load_holdout_unanswerable, split_of
from repomind.ingest.filters import classify_content, classify_path, normalize_source
from repomind.ingest.git_source import GitRepository

DATASET = load_dataset()
DATASET_V2 = load_dataset(V2_DATASET_PATH)
ALL_QUESTIONS = DATASET.questions + DATASET_V2.questions
ALL_UNANSWERABLE = DATASET.unanswerable + load_holdout_unanswerable() + DATASET_V2.unanswerable


def _repository(name: str) -> GitRepository:
    pin = DATASET.repositories[name]
    if not pin.local_path:
        pytest.skip(f"{pin.path_env} is not configured")
    return GitRepository(pin.local_path)


def test_set_has_about_thirty_questions_over_two_repositories():
    assert 28 <= len(DATASET.questions) <= 35
    assert {q.repo for q in DATASET.questions} == {"stockguard", "syncnotes"}
    assert len(DATASET.unanswerable) >= 4


def test_every_question_has_both_languages_and_gold():
    for question in ALL_QUESTIONS:
        assert question.tr.strip() and question.en.strip()
        assert question.gold


def test_split_is_stable_and_covers_every_question():
    dev, test = DATASET.split("dev"), DATASET.split("test")
    assert len(dev) + len(test) == len(DATASET.questions)
    assert not {q.id for q in dev} & {q.id for q in test}
    for repo in ("stockguard", "syncnotes"):
        assert any(q.repo == repo for q in dev)
        assert any(q.repo == repo for q in test)
    assert split_of(dev[0].id, DATASET.questions) == "dev"


def test_new_set_is_independent_of_the_first():
    first_ids = {q.id for q in DATASET.questions} | {q.id for q in DATASET.unanswerable}
    assert not first_ids & {q.id for q in DATASET_V2.questions}
    first_spans = {(s.path, s.start_line, s.end_line) for q in DATASET.questions for s in q.gold}
    assert not first_spans & {(s.path, s.start_line, s.end_line) for q in DATASET_V2.questions for s in q.gold}
    assert all(q.expected for q in DATASET_V2.questions)
    assert {q.kind for q in DATASET_V2.questions} == {"code", "sql", "yaml"}


@pytest.mark.gold
@pytest.mark.parametrize(
    "question,span",
    [(q, s) for q in ALL_QUESTIONS for s in q.gold],
    ids=lambda value: getattr(value, "id", None) or f"{getattr(value, 'path', '')}:{getattr(value, 'start_line', '')}",
)
def test_gold_span_exists_and_contains_its_anchor(question, span):
    git = _repository(question.repo)
    commit = DATASET.repositories[question.repo].commit
    assert git.resolve_commit(commit) == commit

    raw = git.read_file(commit, span.path)
    assert raw is not None, f"{span.path} does not exist at {commit}"
    lines = normalize_source(raw).split("\n")
    assert 1 <= span.start_line <= span.end_line <= len(lines)
    assert span.anchor in "\n".join(lines[span.start_line - 1 : span.end_line])


@pytest.mark.gold
@pytest.mark.parametrize("question", ALL_QUESTIONS, ids=lambda q: q.id)
def test_gold_files_are_indexable(question):
    for span in question.gold:
        assert classify_path(span.path).included, f"{span.path} can never be retrieved"


@pytest.mark.gold
@pytest.mark.parametrize("question", ALL_UNANSWERABLE, ids=lambda q: q.id)
def test_unanswerable_terms_are_absent_from_all_indexable_source(question):
    git = _repository(question.repo)
    commit = DATASET.repositories[question.repo].commit
    pattern = re.compile("|".join(rf"\b{re.escape(term)}\b" for term in question.absent_terms), re.IGNORECASE)
    entries = [e for e in git.list_files(commit) if classify_path(e.path, e.size).included]
    hits = []
    for entry, raw in git.read_blobs(entries):
        text, _ = classify_content(raw)
        if text and (match := pattern.search(text)):
            hits.append(f"{entry.path}: {match.group(0)}")
    assert not hits, f"{question.id} is not unanswerable: {hits[:5]}"
