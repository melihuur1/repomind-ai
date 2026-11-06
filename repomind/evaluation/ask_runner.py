"""Measures the question-answering layer with a real model.

Whether an answer is well written is not scored here. What is measured is what can be
checked mechanically: is it grounded in the gold code, are its citations real, and does
it refuse when the repository cannot answer?

A run can take hours on a CPU, so each finished question goes to a checkpoint straight
away. A run that is stopped or killed loses at most the question it was on, and running
the same command again continues it.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from repomind.errors import RepoMindError
from repomind.evaluation.citations import CitationVerifier
from repomind.evaluation.dataset import Dataset, Question, UnanswerableQuestion
from repomind.evaluation.memory import available_memory_gb
from repomind.evaluation.metrics import overlaps, touches
from repomind.ingest.git_source import GitRepository
from repomind.qa.engine import AskResult, QAEngine
from repomind.qa.prompt import SYSTEM_PROMPT, TRANSLATION_PROMPT

_REFUSALS = {"insufficient", "no_results"}
_SECTIONS = ("answerable", "unanswerable", "holdout_unanswerable")


class Checkpoint:
    """One JSON line per finished question, on disk before the next question starts.

    The first line says what produced the answers, so that answers from another model
    or another prompt can never be mixed into a run without anyone noticing.
    """

    def __init__(self, path: Path, identity: dict) -> None:
        self.path = path
        self.identity = identity

    def load(self) -> dict[tuple[str, str, str], dict]:
        """Returns the finished rows, by section, question and language."""
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        parsed = []
        for line in lines:
            try:
                parsed.append(json.loads(line))
            except ValueError:
                # A process killed while writing leaves a torn last line. That question simply runs again.
                break
        if not parsed:
            self._rewrite([{"identity": self.identity}])
            return {}
        if parsed[0].get("identity") != self.identity:
            raise RepoMindError(
                f"The checkpoint {self.path} was made by a different model, prompt or question set.",
                hint="Use another --output, or pass --overwrite to start this run again from nothing.",
            )
        if len(parsed) != len(lines):
            self._rewrite(parsed)
        return {(p["section"], p["row"]["question"], p["row"]["language"]): p["row"] for p in parsed[1:]}

    def add(self, section: str, row: dict) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"section": section, "row": row}, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _rewrite(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")
        os.replace(temporary, self.path)


def run_identity(qa: QAEngine, dataset: Dataset) -> dict:
    """What has to stay the same for answers to belong to one run."""
    prompts = hashlib.sha256((SYSTEM_PROMPT + TRANSLATION_PROMPT).encode()).hexdigest()
    return {
        "model": qa.config.model if qa.config else None,
        "dataset_sha256": dataset.sha256,
        "prompts_sha256": prompts,
        "max_context_chunks": qa.config.max_context_chunks if qa.config else None,
    }


def evenly_spaced(items: Sequence, count: int) -> list:
    """A small sample across the whole list, which runs from answerable questions to negatives."""
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[0]]
    step = (len(items) - 1) / (count - 1)
    return [items[round(position * step)] for position in range(count)]


def _row(identifier: str, repo: str, language: str, result: AskResult, seconds: float) -> dict:
    free = available_memory_gb()
    return {
        "question": identifier,
        "repo": repo,
        "language": language,
        "status": result.status,
        "translated_query": result.translated_query,
        "answer": result.answer,
        "note": result.note,
        "note_problem": result.note_problem,
        "citations": [
            {"source": c.source_id, "path": c.path, "start_line": c.start_line, "end_line": c.end_line}
            for c in result.citations
        ],
        "passages": [
            {
                "id": p.id,
                "path": p.path,
                "start_line": p.start_line,
                "end_line": p.end_line,
                "shared_terms": list(p.shared_terms),
                "chosen": any(c.source_id == p.id for c in result.citations),
                "text": p.text,
            }
            for p in result.passages
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
        "free_memory_gb": round(free, 1) if free is not None else None,
    }


def _ask(qa: QAEngine, question, language: str) -> tuple[AskResult, dict]:
    started = time.perf_counter()
    result = qa.ask(question.text(language), [question.repo])
    return result, _row(question.id, question.repo, language, result, time.perf_counter() - started)


def _squeezed(text: str) -> str:
    return "".join(text.split()).casefold()


def _answerable_row(qa: QAEngine, question: Question, language: str, verifier: CitationVerifier) -> dict:
    result, row = _ask(qa, question, language)
    row["citations_recheck_valid"] = sum(verifier.check(c.path, c.start_line, c.end_line).valid for c in result.citations)
    # Everything shown is rechecked, including closest lines listed on a refusal.
    row["references_shown"] = len(result.passages)
    row["references_recheck_valid"] = sum(verifier.check(p.path, p.start_line, p.end_line).valid for p in result.passages)
    row["cites_gold"] = any(touches(c, span) for c in result.citations for span in question.gold)
    row["gold_in_context"] = any(touches(p, span) for p in result.passages for span in question.gold)
    row["gold_in_search"] = any(overlaps(hit.chunk, span) for hit in result.hits for span in question.gold)
    anchors = [_squeezed(span.anchor) for span in question.gold]
    chosen_lines = _squeezed("\n".join(item.passage.text for item in result.evidence))
    offered_lines = _squeezed("\n".join(p.text for p in result.passages))
    row["anchor_in_chosen"] = any(anchor in chosen_lines for anchor in anchors)
    row["anchor_in_passages"] = any(anchor in offered_lines for anchor in anchors)
    return row


def _unanswerable_row(qa: QAEngine, question: UnanswerableQuestion, language: str, verifier: CitationVerifier) -> dict:
    result, row = _ask(qa, question, language)
    row["refused"] = result.status in _REFUSALS
    row["citations_recheck_valid"] = sum(verifier.check(c.path, c.start_line, c.end_line).valid for c in result.citations)
    row["references_shown"] = len(result.passages)
    row["references_recheck_valid"] = sum(verifier.check(p.path, p.start_line, p.end_line).valid for p in result.passages)
    return row


@dataclass(frozen=True)
class _Planned:
    section: str
    question: Question | UnanswerableQuestion
    language: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.section, self.question.id, self.language)


def run_ask_evaluation(
    qa: QAEngine,
    dataset: Dataset,
    languages: Sequence[str] = ("tr",),
    progress=None,
    holdout: Sequence[UnanswerableQuestion] = (),
    checkpoint: Checkpoint | None = None,
    limit: int | None = None,
    should_stop: Callable[[], str | None] | None = None,
) -> dict:
    """``limit`` runs an evenly spaced sample. ``should_stop`` is asked before each question, and a reason stops the run."""
    snapshots = {s.repo: s for s in qa.store.active_snapshots()}
    verifiers = {
        name: CitationVerifier(GitRepository(snapshots[name].source_path), pin.commit)
        for name, pin in dataset.repositories.items()
    }
    plan = [
        _Planned(section, question, language)
        for language in languages
        for section, questions in zip(_SECTIONS, (dataset.questions, dataset.unanswerable, holdout))
        for question in questions
    ]
    selected = evenly_spaced(plan, limit) if limit else plan
    rows = checkpoint.load() if checkpoint else {}
    restored = sum(item.key in rows for item in selected)
    stopped_because = None

    for item in selected:
        if item.key in rows:
            continue
        stopped_because = should_stop() if should_stop else None
        if stopped_because:
            break
        try:
            if item.section == "answerable":
                row = _answerable_row(qa, item.question, item.language, verifiers[item.question.repo])
            else:
                row = _unanswerable_row(qa, item.question, item.language, verifiers[item.question.repo])
        except RepoMindError as exc:
            stopped_because = f"error: {exc}"
            break
        except KeyboardInterrupt:
            stopped_because = "interrupted"
            break
        rows[item.key] = row
        if checkpoint:
            checkpoint.add(item.section, row)
        if progress:
            progress(sum(i.key in rows for i in selected), len(selected))

    done = [item for item in selected if item.key in rows]
    complete = len(done) == len(plan)
    results = _results(qa, dataset, {section: [rows[i.key] for i in done if i.section == section] for section in _SECTIONS})
    results["run"] = {
        "complete": complete,
        "status": "complete" if complete else f"incomplete — {stopped_because or f'limited to {limit} questions'}",
        "questions_planned": len(plan),
        "questions_selected": len(selected),
        "questions_done": len(done),
        "questions_restored_from_checkpoint": restored,
        "stopped_because": stopped_because,
    }
    return results


def _results(qa: QAEngine, dataset: Dataset, sections: dict[str, list[dict]]) -> dict:
    answerable, unanswerable, holdout_rows = (sections[name] for name in _SECTIONS)
    rows = answerable + unanswerable + holdout_rows
    answered = [r for r in answerable if r["status"] == "answered"]
    cited = sum(len(r["citations"]) for r in rows)
    free = [r["free_memory_gb"] for r in rows if r.get("free_memory_gb") is not None]
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
            "holdout_unanswerable_questions": len(holdout_rows),
            "holdout_unanswerable_refused": sum(r["refused"] for r in holdout_rows),
            "citations_shown": cited,
            "references_shown": sum(r["references_shown"] for r in rows),
            "references_recheck_valid": sum(r["references_recheck_valid"] for r in rows),
            "notes_left_out": sum(r["note_problem"] is not None for r in rows),
            "rejected_by_backend": sum(len(r["rejected"]) for r in rows),
            "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
            "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
            "max_context_chars": max((r["context_chars"] for r in rows), default=0),
            "mean_seconds": round(sum(r["seconds"] for r in rows) / max(len(rows), 1), 1),
            "lowest_free_memory_gb": min(free, default=None),
        },
        "answerable_citations_rechecked_valid": sum(r.get("citations_recheck_valid", 0) for r in answerable),
        "answerable_citations_shown": sum(len(r["citations"]) for r in answerable),
        "answerable": answerable,
        "unanswerable": unanswerable,
        "holdout_unanswerable": holdout_rows,
    }
