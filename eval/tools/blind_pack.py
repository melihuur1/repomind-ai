"""Shuffles answers from several runs and hides which run wrote each, so they can be graded blind.

Usage: blind_pack.py <work folder> <language> <label=result file> [<label=result file> ...]
Writes blind_items.txt (grade from this) and blind_key.json (do not open until every grade is written).
"""

import json
import random
import re
import sys
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
SEED = 20261106

work = Path(sys.argv[1])
language = sys.argv[2]
runs = dict(argument.split("=", 1) for argument in sys.argv[3:])
raw = yaml.safe_load((ROOT / "eval" / "questions_v2.yaml").read_bytes())
questions = {q["id"]: q for q in raw["questions"]}
negatives = {q["id"]: q for q in raw["unanswerable"]}

items = []
for label, name in runs.items():
    data = json.loads((ROOT / "eval" / "results" / name).read_text(encoding="utf-8"))
    for row in data["answerable"] + data["unanswerable"]:
        if row["status"] != "answered" or row["language"] != language:
            continue
        entry = questions.get(row["question"]) or negatives[row["question"]]
        items.append(
            {
                "config": label,
                "question": row["question"],
                "language": language,
                "answerable": row["question"] in questions,
                "text": entry[language],
                "expected": entry.get("expected") or "(unanswerable: the repository does not have this)",
                "answer": re.sub(r"\s+", " ", re.sub(r"\s*\[(?:S\d+|unverified)\]", "", row["answer"])).strip(),
            }
        )

random.Random(SEED).shuffle(items)
for number, item in enumerate(items, start=1):
    item["id"] = number
work.mkdir(parents=True, exist_ok=True)
(work / "blind_key.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
lines = []
current = None
for item in sorted(items, key=lambda i: (i["question"], i["id"])):
    if item["question"] != current:
        current = item["question"]
        lines.append(f"\n### {current}\nQUESTION: {item['text']}\nEXPECTED: {item['expected']}")
    lines.append(f"#{item['id']}: {item['answer']}")
(work / "blind_items.txt").write_text("\n".join(lines), encoding="utf-8")
print(len(items), "answers to grade")
