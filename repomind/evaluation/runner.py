"""Runs every question through every retrieval method and aggregates the results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from typing import Sequence

from repomind.errors import RepoMindError
from repomind.evaluation.citations import CitationVerifier
from repomind.evaluation.dataset import Dataset, Question, split_of
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
from repomind.search.engine import SearchEngine, SearchHit
from repomind.store.database import Store

K = 5


@dataclass(frozen=True)
class _Location:
    path: str
    start_line: int
    end_line: int


def _as_location(hit: SearchHit) -> _Location:
    return _Location(hit.chunk.path, hit.chunk.start_line, hit.chunk.end_line)


def _verifiers(store: Store, dataset: Dataset) -> dict[str, CitationVerifier]:
    snapshots = {s.repo: s for s in store.active_snapshots()}
    verifiers = {}
    for name, pin in dataset.repositories.items():
        snapshot = snapshots.get(name)
        command = f'python -m repomind index --path "<{name} clone>" --name {name} --ref {pin.commit}'
        if snapshot is None:
            raise RepoMindError(f"Evaluation repository '{name}' is not indexed.", hint=f"Run: {command}")
        if snapshot.commit_sha != pin.commit:
            raise RepoMindError(
                f"'{name}' is indexed at {snapshot.commit_sha[:12]}, but the gold labels are pinned to {pin.commit[:12]}.",
                hint=f"Run: {command}",
            )
        verifiers[name] = CitationVerifier(GitRepository(snapshot.source_path), pin.commit)
    return verifiers


def _evaluate_question(
    engine: SearchEngine, question: Question, language: str, mode: str, verifier: CitationVerifier, depth: int
) -> dict:
    hits = engine.search(question.text(language), mode=mode, repos=[question.repo], limit=depth).hits
    locations = [_as_location(hit) for hit in hits]
    citations = [
        verifier.check(h.chunk.path, h.chunk.start_line, h.chunk.end_line, h.chunk.content) for h in hits[:K]
    ]
    gold_ranks = {}
    for span in question.gold:
        rank = next((position for position, loc in enumerate(locations, start=1) if overlaps(loc, span)), None)
        gold_ranks[f"{span.path}:{span.start_line}-{span.end_line}"] = rank
    return {
        "question": question.id,
        "repo": question.repo,
        "language": language,
        "mode": mode,
        "split": None,
        "recall_at_5": recall_at_k(locations, question.gold, K),
        "hit_at_5": hit_at_k(locations, question.gold, K),
        "reciprocal_rank": reciprocal_rank(locations, question.gold),
        "file_recall_at_5": recall_at_k(locations, question.gold, K, match=same_file),
        "gold_ranks": gold_ranks,
        "citations_checked": len(citations),
        "citations_valid": sum(c.valid for c in citations),
        "top_5": [
            {
                "path": h.chunk.path,
                "start_line": h.chunk.start_line,
                "end_line": h.chunk.end_line,
                "kind": h.chunk.kind,
                "symbol": h.chunk.symbol,
                "is_test": h.chunk.is_test,
            }
            for h in hits[:K]
        ],
    }


def _summary(rows: Sequence[dict]) -> dict:
    def stat(metric: str) -> dict:
        ci = bootstrap_ci([r[metric] for r in rows])
        return {"mean": round(ci.mean, 4), "ci95": [round(ci.low, 4), round(ci.high, 4)]}

    return {
        "questions": len(rows),
        "recall_at_5": stat("recall_at_5"),
        "hit_at_5": stat("hit_at_5"),
        "mrr": stat("reciprocal_rank"),
        "file_recall_at_5": stat("file_recall_at_5"),
        "citations_checked": sum(r["citations_checked"] for r in rows),
        "citations_valid": sum(r["citations_valid"] for r in rows),
        "test_chunks_in_top_5": sum(sum(t["is_test"] for t in r["top_5"]) for r in rows),
        "results_in_top_5": sum(len(r["top_5"]) for r in rows),
    }


def run_evaluation(
    engine: SearchEngine,
    store: Store,
    dataset: Dataset,
    modes: Sequence[str] = ("keyword", "semantic", "hybrid"),
    languages: Sequence[str] = ("tr", "en"),
    depth: int = 50,
) -> dict:
    """Depth is the same for all methods, so MRR compares them on equal terms."""
    verifiers = _verifiers(store, dataset)
    rows: list[dict] = []
    for question in dataset.questions:
        for language in languages:
            for mode in modes:
                row = _evaluate_question(engine, question, language, mode, verifiers[question.repo], depth)
                row["split"] = split_of(question.id, dataset.questions)
                rows.append(row)

    summaries: dict = {}
    for split in ("all", "dev", "test"):
        for language in (*languages, "both"):
            for mode in modes:
                selected = [
                    r
                    for r in rows
                    if r["mode"] == mode
                    and (split == "all" or r["split"] == split)
                    and (language == "both" or r["language"] == language)
                ]
                if selected:
                    summaries.setdefault(split, {}).setdefault(language, {})[mode] = _summary(selected)

    def paired_rows(mode: str, language: str, split: str) -> list[dict]:
        selected = (
            r
            for r in rows
            if r["mode"] == mode and r["language"] == language and (split == "all" or r["split"] == split)
        )
        return sorted(selected, key=lambda r: r["question"])

    comparisons: dict = {}
    if "hybrid" in modes:
        for split in ("all", "test"):
            for language in languages:
                for other in (m for m in modes if m != "hybrid"):
                    for metric in ("recall_at_5", "reciprocal_rank"):
                        ci = paired_difference_ci(
                            [r[metric] for r in paired_rows("hybrid", language, split)],
                            [r[metric] for r in paired_rows(other, language, split)],
                        )
                        comparisons[f"{split}/{language}/hybrid-minus-{other}/{metric}"] = {
                            "mean_difference": round(ci.mean, 4),
                            "ci95": [round(ci.low, 4), round(ci.high, 4)],
                            "significant": not (ci.low <= 0.0 <= ci.high),
                        }

    config = engine.config
    return {
        "dataset_sha256": dataset.sha256,
        "repositories": {name: pin.commit for name, pin in dataset.repositories.items()},
        "embedding_model": engine.embedder.model_name if engine.embedder else None,
        "query_translation": engine.translator is not None,
        "config": asdict(config) if is_dataclass(config) else {},
        "summaries": summaries,
        "comparisons": comparisons,
        "per_question": rows,
    }


def format_summary(results: dict, split: str = "all") -> str:
    lines = [
        f"Embedding model: {results['embedding_model']}",
        f"Evaluation set sha256: {results['dataset_sha256'][:16]}   split: {split}",
        "",
        f"{'language':<8} {'method':<9} {'n':>3}  {'Recall@5':>21}  {'Hit@5':>6}  {'MRR':>21}  {'FileR@5':>7}  {'citations':>9}",
    ]
    for language, modes in results["summaries"].get(split, {}).items():
        for mode, s in modes.items():
            recall, mrr = s["recall_at_5"], s["mrr"]
            lines.append(
                f"{language:<8} {mode:<9} {s['questions']:>3}  "
                f"{recall['mean']:.3f} [{recall['ci95'][0]:.3f}, {recall['ci95'][1]:.3f}]  "
                f"{s['hit_at_5']['mean']:>6.3f}  "
                f"{mrr['mean']:.3f} [{mrr['ci95'][0]:.3f}, {mrr['ci95'][1]:.3f}]  "
                f"{s['file_recall_at_5']['mean']:>7.3f}  "
                f"{s['citations_valid']}/{s['citations_checked']}"
            )
    return "\n".join(lines)
