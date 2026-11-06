"""The evaluation set must be factually true about the code at the pinned commits."""

from __future__ import annotations

import re

import pytest

from repomind.evaluation.dataset import load_dataset, split_of
from repomind.ingest.filters import classify_content, classify_path, normalize_source
from repomind.ingest.git_source import GitRepository

DATASET = load_dataset()


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
    for question in DATASET.questions:
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


@pytest.mark.gold
@pytest.mark.parametrize(
    "question,span",
    [(q, s) for q in DATASET.questions for s in q.gold],
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
@pytest.mark.parametrize("question", DATASET.questions, ids=lambda q: q.id)
def test_gold_files_are_indexable(question):
    for span in question.gold:
        assert classify_path(span.path).included, f"{span.path} can never be retrieved"


@pytest.mark.gold
@pytest.mark.parametrize("question", DATASET.unanswerable, ids=lambda q: q.id)
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
