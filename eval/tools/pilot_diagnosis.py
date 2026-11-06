"""Where did each pilot question go wrong: search, passage selection, or the model's choice? Calls no model."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from repomind.evaluation.dataset import V3_DATASET_PATH, load_dataset  # noqa: E402

RESULTS = ROOT / "eval" / "results"
dataset = load_dataset(V3_DATASET_PATH)
gold_questions = {q.id for q in dataset.questions}

for name in ("v3-new-3b-tr.json", "v3-new-3b-en.json"):
    data = json.loads((RESULTS / name).read_text(encoding="utf-8"))
    print(f"\n{name}")
    stages = {"gold missed by search": [], "in search, not in the passages": [], "offered, model chose other lines": [], "offered, refused": [], "offered, chosen correctly": []}
    for row in data["answerable"]:
        if not row["gold_in_search"]:
            stages["gold missed by search"].append(row["question"])
        elif not row["gold_in_context"]:
            stages["in search, not in the passages"].append(row["question"])
        elif row["cites_gold"]:
            stages["offered, chosen correctly"].append(row["question"])
        elif row["status"] == "answered":
            stages["offered, model chose other lines"].append(row["question"])
        else:
            stages["offered, refused"].append(row["question"])
    for stage, questions in stages.items():
        print(f"  {stage:<34}{len(questions):>2}  {', '.join(q.replace('v3-', '') for q in questions)}")

    rows = data["answerable"] + data["unanswerable"]
    asked = [r for r in rows if r["input_tokens"] and r["sources_sent"]]
    declined = [r for r in asked if r["status"] == "insufficient" and not r["rejected"]]
    weak = [r for r in asked if r["status"] == "insufficient" and r["rejected"]]
    stopped_early = [r for r in rows if r["status"] == "insufficient" and not r["sources_sent"]]
    print(f"  the model was asked {len(asked)} times and declined {len(declined)} times: {', '.join(r['question'].replace('v3-', '') for r in declined) or 'none'}")
    print(f"  its choice was dropped by the evidence rule {len(weak)} times: {', '.join(r['question'].replace('v3-', '') for r in weak) or 'none'}")
    print(f"  refused before asking the model {len(stopped_early)} times: {', '.join(r['question'].replace('v3-', '') for r in stopped_early) or 'none'}")
    print(f"  notes kept {sum(bool(r['note']) for r in rows)}, left out {sum(r['note_problem'] is not None for r in rows)}")
    for row in stopped_early:
        print(f"    {row['question'].replace('v3-', '')}: {(row['warnings'] or [''])[-1][:90]}")
