"""Everything about the held-out answers that can be counted without grading them.

Usage: count_answers.py <label=result file> [<label=result file> ...]
"""

import json
import re
import sys
from pathlib import Path

import yaml

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "eval" / "results"
raw = yaml.safe_load((ROOT / "eval" / "questions_v2.yaml").read_bytes())
KIND = {q["id"]: q["kind"] for q in raw["questions"]}
REFUSED = ("insufficient", "no_results")
UNITS = {"KiB": 1 / 1024 / 1024, "MiB": 1 / 1024, "GiB": 1.0, "B": 1 / 1024**3}


def mean(values):
    values = list(values)
    return round(sum(values) / len(values), 1) if values else None


for argument in sys.argv[1:]:
    label, name = argument.split("=", 1)
    data = json.loads((RESULTS / name).read_text(encoding="utf-8"))
    print(f"\n=== {label} ({data['model']})")
    for language in ("tr", "en"):
        answerable = [r for r in data["answerable"] if r["language"] == language]
        negatives = [r for r in data["unanswerable"] if r["language"] == language]
        if not answerable:
            continue
        everything = answerable + negatives
        called = [r for r in everything if r["input_tokens"]]
        answered = [r for r in answerable if r["status"] == "answered"]
        print(
            f"  {language}: answerable {len(answerable)}: answered {len(answered)}, "
            f"refused {sum(r['status'] in REFUSED for r in answerable)}, "
            f"invalid {sum(r['status'] == 'invalid_response' for r in answerable)}; "
            f"citing gold {sum(r['cites_gold'] for r in answered)}, gold in context {sum(r['gold_in_context'] for r in answerable)}"
        )
        for kinds, group in ((("code",), "code"), (("sql", "yaml"), "sql+yaml")):
            part = [r for r in answerable if KIND[r["question"]] in kinds]
            print(
                f"      {group}: {len(part)} questions, answered {sum(r['status'] == 'answered' for r in part)}, "
                f"citing gold {sum(r['status'] == 'answered' and r['cites_gold'] for r in part)}, "
                f"gold in context {sum(r['gold_in_context'] for r in part)}"
            )
        print(
            f"      negatives {len(negatives)}: refused {sum(r['refused'] for r in negatives)}; "
            f"answered: {', '.join(r['question'] for r in negatives if not r['refused']) or 'none'}"
        )
        print(
            f"      seconds: mean all {mean(r['seconds'] for r in everything)}, answerable {mean(r['seconds'] for r in answerable)}, "
            f"negatives {mean(r['seconds'] for r in negatives)}, when the model was called {mean(r['seconds'] for r in called)}, "
            f"max {max(r['seconds'] for r in everything)}"
        )
        print(
            f"      tokens in/out {sum(r['input_tokens'] or 0 for r in everything)}/{sum(r['output_tokens'] or 0 for r in everything)}; "
            f"citations shown {sum(len(r['citations']) for r in answerable)}, rechecked valid {sum(r['citations_recheck_valid'] for r in answerable)}; "
            f"discarded {sum(len(r['rejected']) for r in everything)}"
        )
        print(f"      refused answerable: {', '.join(r['question'] for r in answerable if r['status'] != 'answered') or 'none'}")
        print(f"      gold not in context: {', '.join(r['question'] for r in answerable if not r['gold_in_context']) or 'none'}")

print("\n=== memory samples, every 20 s, from eval/results/resources")
for path in sorted((RESULTS / "resources").glob("stats-*.txt")):
    memory, cpu = [], []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        found = re.match(r"\s*([\d.]+)(KiB|MiB|GiB|B) / \S+ ([\d.]+)%", line)
        if found:
            memory.append(float(found[1]) * UNITS[found[2]])
            cpu.append(float(found[3]))
    if memory:
        print(
            f"{path.stem}: {len(memory)} samples, Ollama container peak {max(memory):.2f} GiB, "
            f"mean {sum(memory) / len(memory):.2f} GiB, CPU mean {sum(cpu) / len(cpu):.0f}%"
        )
