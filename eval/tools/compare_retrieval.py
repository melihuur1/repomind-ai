"""Compares retrieval on the second question set: frozen phase 1, current code and the rejected reranker."""

import json
import random
import sys
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "eval" / "results"
raw = yaml.safe_load((ROOT / "eval" / "questions_v2.yaml").read_bytes())
KIND = {q["id"]: q["kind"] for q in raw["questions"]}
GROUPS = {"all": lambda k: True, "code": lambda k: k == "code", "sql+yaml": lambda k: k in ("sql", "yaml")}
MODES = ("keyword", "semantic", "hybrid", "reranked")


def load(name):
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))["per_question"]


def ci(diffs, rounds=4000, seed=7):
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(rng.choices(diffs, k=n)) / n for _ in range(rounds))
    return sum(diffs) / n, means[int(rounds * 0.025)], means[int(rounds * 0.975)]


def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else float("nan")


def recall_at(row, depth):
    """Uses the rank of each gold span, which the results keep down to the search depth of 50."""
    ranks = row["gold_ranks"].values()
    return sum(rank is not None and rank <= depth for rank in ranks) / len(ranks)


def paired(label, a_rows, a_mode, b_rows, b_mode):
    print(f"\n{label}")
    for group, keep in GROUPS.items():
        for language in ("tr", "en"):
            a = {r["question"]: r for r in a_rows if r["mode"] == a_mode and r["language"] == language and keep(KIND[r["question"]])}
            b = {r["question"]: r for r in b_rows if r["mode"] == b_mode and r["language"] == language and keep(KIND[r["question"]])}
            ids = sorted(a)
            if not ids:
                continue
            d_recall = [a[i]["recall_at_5"] - b[i]["recall_at_5"] for i in ids]
            d_mrr = [a[i]["reciprocal_rank"] - b[i]["reciprocal_rank"] for i in ids]
            x, y = ci(d_recall), ci(d_mrr)
            print(
                f"  {group:<9} {language:<3} n={len(ids):<3} dR@5 {x[0]:+.3f} [{x[1]:+.3f},{x[2]:+.3f}]  "
                f"dMRR {y[0]:+.3f} [{y[1]:+.3f},{y[2]:+.3f}]  better {sum(v > 0 for v in d_recall)} worse {sum(v < 0 for v in d_recall)}"
            )


phase1 = load("v2-retrieval-phase1.json")
phase2 = load("v2-retrieval-phase2.json")
reranker = load("v2-retrieval-reranker-bge.json")

print(f"{'run':<7} {'group':<9} {'lang':<4} {'mode':<9} {'n':>3} {'R@5':>6} {'Hit@5':>6} {'MRR':>6} {'p50ms':>7} {'p95ms':>7} {'citations':>10}")
for run, rows in {"phase1": phase1, "phase2": phase2, "bge": reranker}.items():
    for group, keep in GROUPS.items():
        for language in ("tr", "en"):
            for mode in MODES:
                selected = [r for r in rows if r["mode"] == mode and r["language"] == language and keep(KIND[r["question"]])]
                if not selected:
                    continue
                # Phase 1 did not record latency per question.
                latencies = sorted(r.get("latency_ms", 0) for r in selected)
                print(
                    f"{run:<7} {group:<9} {language:<4} {mode:<9} {len(selected):>3} "
                    f"{mean(r['recall_at_5'] for r in selected):>6.3f} {mean(r['hit_at_5'] for r in selected):>6.3f} "
                    f"{mean(r['reciprocal_rank'] for r in selected):>6.3f} "
                    f"{latencies[len(latencies) // 2]:>7.0f} {latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)]:>7.0f} "
                    f"{sum(r['citations_valid'] for r in selected):>5}/{sum(r['citations_checked'] for r in selected)}"
                )

paired("phase 2 hybrid minus phase 1 hybrid", phase2, "hybrid", phase1, "hybrid")
paired("bge reranker minus phase 2 hybrid", reranker, "reranked", phase2, "hybrid")
paired("phase 2 hybrid minus phase 2 keyword", phase2, "hybrid", phase2, "keyword")
paired("phase 2 hybrid minus phase 2 semantic", phase2, "hybrid", phase2, "semantic")

print("\nDoes reading more plain hybrid results make up for not reranking?")
for language in ("tr", "en"):
    rows = [r for r in phase2 if r["mode"] == "hybrid" and r["language"] == language]
    ranked = [r for r in reranker if r["language"] == language]
    print(
        f"  {language}: hybrid R@5 {mean(recall_at(r, 5) for r in rows):.3f}, R@8 {mean(recall_at(r, 8) for r in rows):.3f}, "
        f"Hit@8 {mean(recall_at(r, 8) > 0 for r in rows):.3f} | bge R@5 {mean(r['recall_at_5'] for r in ranked):.3f}, "
        f"R@8 {mean(recall_at(r, 8) for r in ranked):.3f}, Hit@8 {mean(recall_at(r, 8) > 0 for r in ranked):.3f}"
    )

dev = RESULTS / "reranker-dev"
base = {(r["question"], r["language"]): r for r in json.loads((dev / "base.json").read_text(encoding="utf-8"))["per_question"] if r["mode"] == "hybrid"}
old = json.loads((dev / "bge-reranker-v2-m3-int8-pure.json").read_text(encoding="utf-8"))["per_question"]
print("\nbge reranker minus hybrid, both question sets pooled (code questions only in the second set)")
for language in ("tr", "en"):
    d_recall = [r["recall_at_5"] - base[(r["question"], language)]["recall_at_5"] for r in old if r["language"] == language]
    d_mrr = [r["reciprocal_rank"] - base[(r["question"], language)]["reciprocal_rank"] for r in old if r["language"] == language]
    n_old = len(d_recall)
    plain = {r["question"]: r for r in phase2 if r["mode"] == "hybrid" and r["language"] == language}
    for r in reranker:
        if r["language"] == language and KIND[r["question"]] == "code":
            d_recall.append(r["recall_at_5"] - plain[r["question"]]["recall_at_5"])
            d_mrr.append(r["reciprocal_rank"] - plain[r["question"]]["reciprocal_rank"])
    x, y = ci(d_recall), ci(d_mrr)
    print(f"  {language} n={n_old}+{len(d_recall) - n_old}: dR@5 {x[0]:+.3f} [{x[1]:+.3f},{x[2]:+.3f}]  dMRR {y[0]:+.3f} [{y[1]:+.3f},{y[2]:+.3f}]")

print("\nOld 31 questions: hybrid with SQL and YAML in the index, minus phase 1")
before = {(r["question"], r["language"]): r for r in load("final.json") if r["mode"] == "hybrid"}
for language in ("tr", "en"):
    keys = sorted(k for k in base if k[1] == language)
    d_recall = [base[k]["recall_at_5"] - before[k]["recall_at_5"] for k in keys]
    d_mrr = [base[k]["reciprocal_rank"] - before[k]["reciprocal_rank"] for k in keys]
    x, y = ci(d_recall), ci(d_mrr)
    print(
        f"  {language} n={len(keys)}: R@5 {mean(before[k]['recall_at_5'] for k in keys):.3f} -> {mean(base[k]['recall_at_5'] for k in keys):.3f}, "
        f"MRR {mean(before[k]['reciprocal_rank'] for k in keys):.3f} -> {mean(base[k]['reciprocal_rank'] for k in keys):.3f}; "
        f"dR@5 {x[0]:+.3f} [{x[1]:+.3f},{x[2]:+.3f}]  dMRR {y[0]:+.3f} [{y[1]:+.3f},{y[2]:+.3f}]"
    )

print("\nQuestions with no gold in the top 5, phase 2 hybrid")
for language in ("tr", "en"):
    missed = [r["question"] for r in phase2 if r["mode"] == "hybrid" and r["language"] == language and not r["hit_at_5"]]
    print(f"  {language}: {len(missed)}: {', '.join(missed)}")
