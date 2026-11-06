"""Measures the question-answering layer with a real model.

Whether an answer is well written is not scored here. What is measured is what can be
checked mechanically: is it grounded in the gold code, are its citations real, and does
it refuse when the repository cannot answer?
"""

from __future__ import annotations

import time
from typing import Sequence

from repomind.evaluation.citations import CitationVerifier
from repomind.evaluation.dataset import Dataset
from repomind.evaluation.metrics import overlaps
from repomind.ingest.git_source import GitRepository
from repomind.qa.engine import AskResult, QAEngine

_REFUSALS = {"insufficient", "no_results"}


def _row(identifier: str, repo: str, language: str, result: AskResult, seconds: float) -> dict:
    return {
        "question": identifier,
        "repo": repo,
        "language": language,
        "status": result.status,
        "answer": result.answer,
        "citations": [
            {"source": c.source_id, "path": c.path, "start_line": c.start_line, "end_line": c.end_line}
            for c in result.citations
        ],
        "rejected": result.rejected,
        "warnings": result.warnings,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "tokens_estimated": result.tokens_are_estimated,
        "cost_usd": result.cost_usd,
        "context_chars": result.context_chars,
        "sources_sent": result.sources_sent,
        "sources_dropped": result.sources_dropped,
        "seconds": round(seconds, 1),
    }


def run_ask_evaluation(
    qa: QAEngine,
    dataset: Dataset,
    languages: Sequence[str] = ("tr",),
    progress=None,
) -> dict:
    snapshots = {s.repo: s for s in qa.store.active_snapshots()}
    verifiers = {
        name: CitationVerifier(GitRepository(snapshots[name].source_path), pin.commit)
        for name, pin in dataset.repositories.items()
    }
    answerable: list[dict] = []
    unanswerable: list[dict] = []
    total = (len(dataset.questions) + len(dataset.unanswerable)) * len(languages)

    def report() -> None:
        if progress:
            progress(len(answerable) + len(unanswerable), total)

    for language in languages:
        for question in dataset.questions:
            started = time.perf_counter()
            result = qa.ask(question.text(language), [question.repo])
            row = _row(question.id, question.repo, language, result, time.perf_counter() - started)
            verifier = verifiers[question.repo]
            row["citations_recheck_valid"] = sum(
                verifier.check(c.path, c.start_line, c.end_line).valid for c in result.citations
            )
            row["cites_gold"] = any(overlaps(c, span) for c in result.citations for span in question.gold)
            row["gold_in_context"] = any(
                overlaps(hit.chunk, span) for hit in result.hits[: result.sources_sent] for span in question.gold
            )
            answerable.append(row)
            report()
        for question in dataset.unanswerable:
            started = time.perf_counter()
            result = qa.ask(question.text(language), [question.repo])
            row = _row(question.id, question.repo, language, result, time.perf_counter() - started)
            row["refused"] = result.status in _REFUSALS
            unanswerable.append(row)
            report()

    rows = answerable + unanswerable
    answered = [r for r in answerable if r["status"] == "answered"]
    cited = sum(len(r["citations"]) for r in rows)
    valid = sum(r.get("citations_recheck_valid", 0) for r in answerable) + sum(
        len(r["citations"]) for r in unanswerable
    )
    return {
        "model": qa.config.model if qa.config else None,
        "base_url": qa.config.base_url if qa.config else None,
        "dataset_sha256": dataset.sha256,
        "summary": {
            "answerable_questions": len(answerable),
            "answered": len(answered),
            "answered_citing_gold": sum(r["cites_gold"] for r in answered),
            "gold_was_in_context": sum(r["gold_in_context"] for r in answerable),
            "refused_answerable": sum(r["status"] in _REFUSALS for r in answerable),
            "invalid_responses": sum(r["status"] == "invalid_response" for r in rows),
            "unanswerable_questions": len(unanswerable),
            "unanswerable_refused": sum(r["refused"] for r in unanswerable),
            "citations_shown": cited,
            "citations_rejected_by_backend": sum(len(r["rejected"]) for r in rows),
            "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
            "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
            "max_context_chars": max((r["context_chars"] for r in rows), default=0),
            "mean_seconds": round(sum(r["seconds"] for r in rows) / max(len(rows), 1), 1),
        },
        "answerable_citations_rechecked_valid": sum(r.get("citations_recheck_valid", 0) for r in answerable),
        "answerable_citations_shown": sum(len(r["citations"]) for r in answerable),
        "answerable": answerable,
        "unanswerable": unanswerable,
    }
