"""Where each question of the sixth set went wrong, from the stored rows only.

    python eval/tools/v6_diagnosis.py

Reads eval/results/v6-*.json and eval/questions_v6.yaml. It asks no model, searches nothing and
re-runs nothing, so it can only say what those rows hold: which passages were offered, which were
sent, what was chosen. The rows do not hold the search candidates before passages were made, so
"never found" and "found, and not made a passage" cannot be told apart here.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parents[2]
RESULTS = PROJECT / "eval" / "results"


def overlap(first: dict, second: dict) -> bool:
    return first["path"] == second["path"] and first["start_line"] <= second["end_line"] and second["start_line"] <= first["end_line"]


def places(question: dict, sources: list[dict]) -> list[str]:
    return [p["name"] for p in question.get("places", []) if any(overlap(s, a) for s in sources for a in p["accepted"])]


def stage(question: dict, row: dict, outcome: str) -> str:
    """The first thing that went wrong for an answerable question, by what its row holds."""
    need = len(question["places"])
    offered = places(question, row["offered"])
    if outcome == "correct":
        return "correct"
    if outcome == "invalid":
        return "invalid reply from the model"
    if not row["reached_model"]:
        return "refused before the model, " + ("with a right place among the passages" if offered else "with no passages")
    if not offered:
        return "no right place among the passages, and the model " + ("chose other lines" if outcome == "wrong" else "gave no answer")
    if len(offered) < need:
        return "one of two needed places among the passages"
    if outcome == "partial":
        return "both places offered, and the model chose one"
    if outcome in ("wrong", "mixed"):
        return "a right place offered, and the model chose other lines"
    return "a right place offered, and " + ("the evidence rule turned the choice down" if row["review"] else "the model chose nothing")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    data = yaml.safe_load((PROJECT / "eval" / "questions_v6.yaml").read_text(encoding="utf-8"))
    questions = {q["id"]: {**q, "answerable": True} for q in data["questions"]} | {q["id"]: {**q, "answerable": False} for q in data["unanswerable"]}
    report = json.loads((RESULTS / "v6-report.json").read_text(encoding="utf-8"))
    for name, file in (("v1 A1", "v1-part-a-run1"), ("v2 A1", "v2-part-a-run1"), ("v1 B", "v1-part-b"), ("v2 B", "v2-part-b")):
        rows = {r["id"]: r for r in json.loads((RESULTS / f"v6-{file}.json").read_text(encoding="utf-8"))["rows"]}
        outcomes = report["runs"][name]["outcomes"]
        for language, label in (("tr", "Turkish"), ("en", "English")):
            answerable = [q for q in rows if questions[q]["language"] == language and questions[q]["answerable"]]
            if not answerable:
                continue
            staged = {q: stage(questions[q], rows[q], outcomes[q]) for q in answerable}
            print(f"--- {name}, {label}: {len(answerable)} answerable")
            for what, count in Counter(staged.values()).most_common():
                print(f"   {count:>2}  {what}: {', '.join(q.removeprefix('v6-') for q in staged if staged[q] == what) if what != 'correct' else ''}".rstrip(": "))
            negatives = [q for q in rows if questions[q]["language"] == language and not questions[q]["answerable"]]
            accepted = [q.removeprefix("v6-") for q in negatives if outcomes[q] == "wrongly accepted"]
            print(f"       unanswerable: {len(negatives)}, reached the model {sum(rows[q]['reached_model'] for q in negatives)}, wrongly accepted {len(accepted)} {accepted or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
