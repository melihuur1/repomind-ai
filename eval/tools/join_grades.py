"""Joins blind grades with the key and counts them. Run only after every answer has a grade.

Usage: join_grades.py <work folder> <output json> <label=result file> [<label=result file> ...]
Reads blind_grades.json and blind_key.json from the work folder.
"""

import json
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "eval" / "results"
ALLOWED = {"correct", "partial", "wrong", "fabricated", "harmless"}

work = Path(sys.argv[1])
output = RESULTS / sys.argv[2]
runs = dict(argument.split("=", 1) for argument in sys.argv[3:])
key = json.loads((work / "blind_key.json").read_text(encoding="utf-8"))
grades = json.loads((work / "blind_grades.json").read_text(encoding="utf-8"))
missing = [item["id"] for item in key if str(item["id"]) not in grades]
if missing:
    sys.exit(f"Not graded yet: {missing}")
if set(grades.values()) - ALLOWED:
    sys.exit(f"Unknown grades: {set(grades.values()) - ALLOWED}")
if output.exists():
    sys.exit(f"Refusing to overwrite {output}")
KIND: dict = {}
for dataset in ("questions_v2.yaml", "questions_v3.yaml"):
    raw = yaml.safe_load((ROOT / "eval" / dataset).read_bytes())
    KIND.update({q["id"]: q["kind"] for q in raw["questions"]})

unknown = {item["config"] for item in key} - set(runs)
if unknown:
    sys.exit(f"The key uses labels that were not given here: {unknown}")
joined = [
    {**item, "grade": grades[str(item["id"])], "kind": KIND.get(item["question"], "negative"), "run": runs[item["config"]]}
    for item in key
]
fields = ("run", "question", "language", "kind", "grade", "answer")
output.write_text(
    json.dumps(
        [{f: i[f] for f in fields} for i in sorted(joined, key=lambda i: (i["run"], i["question"]))],
        ensure_ascii=False,
        indent=1,
    ),
    encoding="utf-8",
)

language = key[0]["language"] if key else "tr"
for label, name in runs.items():
    data = json.loads((RESULTS / name).read_text(encoding="utf-8"))
    rows = [r for r in data["answerable"] if r["language"] == language]
    negatives = [r for r in data["unanswerable"] if r["language"] == language]
    mine = [i for i in joined if i["config"] == label]
    print(f"\n=== {label} ({language})")
    for group, kinds in (("all", ("code", "sql", "yaml")), ("code", ("code",)), ("sql+yaml", ("sql", "yaml"))):
        part = [i for i in mine if i["kind"] in kinds]
        counts = Counter(i["grade"] for i in part)
        total = sum(1 for r in rows if KIND[r["question"]] in kinds)
        print(
            f"  {group:<9} questions {total:>2}: correct {counts['correct']:>2}, partial {counts['partial']:>2}, "
            f"wrong {counts['wrong']:>2}, not answered {total - len(part):>2}"
        )
    bad = [i for i in mine if i["kind"] == "negative"]
    counts = Counter(i["grade"] for i in bad)
    print(f"  negatives {len(negatives)}: refused {len(negatives) - len(bad)}, fabricated {counts['fabricated']}, harmless {counts['harmless']}")
    for grade in ("correct", "partial", "wrong", "fabricated", "harmless"):
        names = sorted(i["question"] for i in mine if i["grade"] == grade)
        if names:
            print(f"  {grade}: {', '.join(names)}")
    answered = {i["question"] for i in mine}
    print(f"  not answered: {', '.join(sorted(r['question'] for r in rows if r['question'] not in answered)) or 'none'}")
