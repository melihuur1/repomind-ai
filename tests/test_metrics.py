"""Metrics against hand-computed values, and citation verification against real blobs."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from repomind.evaluation.citations import CitationVerifier
from repomind.evaluation.metrics import (
    bootstrap_ci,
    hit_at_k,
    overlaps,
    paired_difference_ci,
    recall_at_k,
    reciprocal_rank,
    same_file,
)
from repomind.ingest.git_source import GitRepository
from tests.conftest import git


@dataclass(frozen=True)
class Span:
    path: str
    start_line: int
    end_line: int


A = Span("a.ts", 10, 20)
B = Span("b.ts", 1, 5)
C = Span("c.ts", 50, 60)


class TestOverlap:
    def test_different_file_never_matches(self):
        assert not overlaps(Span("x.ts", 10, 20), A)

    def test_needs_three_lines(self):
        assert overlaps(Span("a.ts", 18, 30), A)
        assert not overlaps(Span("a.ts", 19, 30), A)
        assert not overlaps(Span("a.ts", 20, 30), A)

    def test_short_gold_needs_all_its_lines(self):
        short = Span("a.ts", 10, 11)
        assert overlaps(Span("a.ts", 10, 11), short)
        assert overlaps(Span("a.ts", 1, 40), short)
        assert not overlaps(Span("a.ts", 11, 40), short)

    def test_no_overlap(self):
        assert not overlaps(Span("a.ts", 21, 30), A)


class TestRecall:
    def test_all_found(self):
        assert recall_at_k([A, B, C], [A, B], 5) == 1.0

    def test_partial(self):
        assert recall_at_k([A, C], [A, B], 5) == 0.5

    def test_only_top_k_counts(self):
        assert recall_at_k([C, C, A], [A], 2) == 0.0
        assert recall_at_k([C, C, A], [A], 3) == 1.0

    def test_two_hits_on_one_gold_count_once(self):
        assert recall_at_k([A, Span("a.ts", 12, 18)], [A, B], 5) == 0.5

    def test_one_hit_can_cover_two_gold_spans(self):
        whole = Span("a.ts", 1, 100)
        assert recall_at_k([whole], [Span("a.ts", 5, 10), Span("a.ts", 20, 30)], 5) == 1.0

    def test_no_hits(self):
        assert recall_at_k([], [A], 5) == 0.0

    def test_no_gold_is_an_error(self):
        with pytest.raises(ValueError):
            recall_at_k([A], [], 5)

    def test_file_level_is_more_lenient(self):
        elsewhere = Span("a.ts", 100, 110)
        assert recall_at_k([elsewhere], [A], 5) == 0.0
        assert recall_at_k([elsewhere], [A], 5, match=same_file) == 1.0


class TestReciprocalRank:
    def test_first_position(self):
        assert reciprocal_rank([A, B], [A]) == 1.0

    def test_third_position(self):
        assert reciprocal_rank([B, C, A], [A]) == pytest.approx(1 / 3)

    def test_uses_first_match_of_any_gold(self):
        assert reciprocal_rank([C, B, A], [A, B]) == pytest.approx(1 / 2)

    def test_absent(self):
        assert reciprocal_rank([B, C], [A]) == 0.0


def test_hit_at_k():
    assert hit_at_k([C, A], [A, B], 5) == 1.0
    assert hit_at_k([C, A], [A, B], 1) == 0.0


class TestBootstrap:
    def test_constant_values_have_zero_width(self):
        interval = bootstrap_ci([0.5, 0.5, 0.5])
        assert (interval.mean, interval.low, interval.high) == (0.5, 0.5, 0.5)

    def test_interval_contains_mean_and_is_reproducible(self):
        values = [0, 1, 1, 0, 1, 1, 1, 0, 1, 0]
        first, second = bootstrap_ci(values), bootstrap_ci(values)
        assert first == second
        assert first.low < first.mean < first.high

    def test_identical_paired_lists_show_no_difference(self):
        interval = paired_difference_ci([1, 0, 1], [1, 0, 1])
        assert (interval.mean, interval.low, interval.high) == (0, 0, 0)

    def test_clear_paired_improvement_excludes_zero(self):
        interval = paired_difference_ci([1.0] * 20, [0.0] * 18 + [1.0] * 2)
        assert interval.low > 0

    def test_noise_spans_zero(self):
        a = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]
        b = [0, 1, 0, 1, 0, 1, 0, 1, 1, 0]
        interval = paired_difference_ci(a, b)
        assert interval.low <= 0 <= interval.high

    def test_unequal_lengths_rejected(self):
        with pytest.raises(ValueError):
            paired_difference_ci([1], [1, 2])


class TestCitationVerifier:
    @pytest.fixture
    def verifier(self, sample_repo):
        repository = GitRepository(sample_repo)
        return CitationVerifier(repository, git(sample_repo, "rev-parse", "HEAD"))

    def test_correct_citation(self, verifier):
        content = "export function refundPayment(id: string) {\n  return charge(id, -1);\n}"
        assert verifier.check("src/payment.ts", 3, 5, content).valid

    def test_missing_file(self, verifier):
        check = verifier.check("src/invented.ts", 1, 2)
        assert not check.valid
        assert "does not exist" in check.reason

    @pytest.mark.parametrize(("start", "end"), [(0, 2), (5, 3), (1, 999), (-1, 1)])
    def test_impossible_lines(self, verifier, start, end):
        check = verifier.check("src/payment.ts", start, end)
        assert not check.valid

    def test_wrong_content_is_detected(self, verifier):
        check = verifier.check("src/payment.ts", 3, 5, "something else")
        assert not check.valid
        assert "differs" in check.reason

    def test_shifted_lines_are_detected(self, verifier):
        content = "export function refundPayment(id: string) {\n  return charge(id, -1);\n}"
        assert not verifier.check("src/payment.ts", 2, 4, content).valid

    def test_verifies_against_the_commit_not_the_working_tree(self, sample_repo, verifier):
        (sample_repo / "src" / "payment.ts").write_text("// changed on disk\n", encoding="utf-8")
        content = "export function refundPayment(id: string) {\n  return charge(id, -1);\n}"
        assert verifier.check("src/payment.ts", 3, 5, content).valid
