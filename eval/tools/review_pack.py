"""Grades sources to review without labels. They are never answers, so they are graded apart from answers.

    python eval/tools/review_pack.py pack results.json --pack pack.json --key key.json
    python eval/tools/review_pack.py report results.json --grades grades.json --key key.json

A grade says whether the lines themselves answer the question: answers, partly or does_not_answer.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
GRADES = {"answers", "partly", "does_not_answer"}


def questions() -> dict:
    found: dict = {}
    for name in ("questions_v4.yaml", "questions_v5.yaml"):
        raw = yaml.safe_load((ROOT / "eval" / name).read_bytes())
        found.update({q["id"]: q for q in (*raw["questions"], *raw["unanswerable"])})
    return found


def rows_with_candidates(results: Path) -> list[dict]:
    data = json.loads(results.read_text(encoding="utf-8"))
    return [row for row in data["answerable"] + data["unanswerable"] if row.get("review_candidates")]


def pack(results: Path, pack_path: Path, key_path: Path) -> int:
    known = questions()
    rows = rows_with_candidates(results)
    random.Random(20261107).shuffle(rows)
    entries, key = [], {}
    for number, row in enumerate(rows, start=1):
        label = f"R{number:02d}"
        question = known[row["question"]]
        by_id = {p["id"]: p for p in row["passages"]}
        key[label] = row["question"]
        entries.append({
            "label": label,
            "question": question["tr"],
            "question_en": question["en"],
            "expected": question.get("expected") or "UNANSWERABLE: the repository does not have this feature.",
            "lines": [f"{by_id[i]['path']}:{by_id[i]['start_line']}-{by_id[i]['end_line']}\n{by_id[i]['text']}" for i in row["review_candidates"]],
        })
    pack_path.write_text(json.dumps(entries, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    key_path.write_text(json.dumps(key, indent=1) + "\n", encoding="utf-8")
    print(f"{len(entries)} questions with sources to review")
    return 0


def report(results: Path, grades_path: Path, key_path: Path) -> int:
    data = json.loads(results.read_text(encoding="utf-8"))
    grades = json.loads(grades_path.read_text(encoding="utf-8"))
    key = json.loads(key_path.read_text(encoding="utf-8"))
    if set(grades.values()) - GRADES:
        sys.exit(f"Unknown grades: {set(grades.values()) - GRADES}")
    if set(key) != set(grades):
        sys.exit("Every source to review needs one grade.")
    by_question = {key[label]: grade for label, grade in grades.items()}
    negatives = {row["question"] for row in data["unanswerable"]}
    answerable = {row["question"]: row for row in data["answerable"]}
    print(f"on answerable questions: {Counter(g for q, g in by_question.items() if q not in negatives)}")
    print(f"on unanswerable questions: {Counter(g for q, g in by_question.items() if q in negatives)}")
    for question, grade in sorted(by_question.items()):
        row = answerable.get(question)
        touches_gold = bool(row and row.get("review_candidate_is_gold"))
        status = row["status"] if row else "unanswerable"
        print(f"  {question:<30} {grade:<16} touches gold {touches_gold!s:<6} status {status}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("pack")
    command.add_argument("results", type=Path)
    command.add_argument("--pack", type=Path, required=True)
    command.add_argument("--key", type=Path, required=True)
    command = commands.add_parser("report")
    command.add_argument("results", type=Path)
    command.add_argument("--grades", type=Path, required=True)
    command.add_argument("--key", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "pack":
        return pack(arguments.results, arguments.pack, arguments.key)
    return report(arguments.results, arguments.grades, arguments.key)


if __name__ == "__main__":
    sys.exit(main())
