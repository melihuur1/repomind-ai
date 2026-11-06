"""Computes the pilot's outcomes, as defined in eval/PILOT.md, from stored results. Calls no model.

Usage: pilot_report.py <label=result file> [...]
A label containing "new" is checked against the bar, in Turkish.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from repomind.evaluation.citations import CitationVerifier  # noqa: E402
from repomind.evaluation.dataset import V3_DATASET_PATH, V4_DATASET_PATH, V5_DATASET_PATH, load_dataset  # noqa: E402
from repomind.evaluation.metrics import touches  # noqa: E402
from repomind.ingest.git_source import GitRepository  # noqa: E402

RESULTS = ROOT / "eval" / "results"
UNITS = {"KiB": 1 / 1024 / 1024, "MiB": 1 / 1024, "GiB": 1.0, "B": 1 / 1024**3}
datasets = [load_dataset(path) for path in (V3_DATASET_PATH, V4_DATASET_PATH, V5_DATASET_PATH)]
gold = {q.id: q.accepted for dataset in datasets for q in dataset.questions}
verifiers = {name: CitationVerifier(GitRepository(pin.local_path), pin.commit) for name, pin in datasets[0].repositories.items()}


class Span:
    def __init__(self, item: dict) -> None:
        self.path, self.start_line, self.end_line = item["path"], item["start_line"], item["end_line"]


def squeeze(text: str) -> str:
    return "".join(text.split()).casefold()


def shown_as_answer(row: dict) -> list[dict]:
    """With no model, the closest lines are all a reader gets, so they count unless the system refused."""
    if row["status"] == "no_llm":
        return row["passages"]
    return row["citations"] if row["status"] == "answered" else []


def everything_shown(row: dict) -> list[dict]:
    return row.get("passages") or row["citations"]


def shown_text(row: dict) -> str:
    if "readings" in row:
        return "\n".join(r["quote"] for r in row["readings"] if r["outcome"] == "supported")
    if row["status"] == "no_llm":
        return "\n".join(p["text"] for p in row["passages"])
    return row["answer"]


def report(label: str, name: str) -> None:
    data = json.loads((RESULTS / name).read_text(encoding="utf-8"))
    print(f"\n=== {label}: {name} ({data['model'] or 'no model'}), {data.get('run', {}).get('status', 'complete')}")
    for language in ("tr", "en"):
        answerable = [r for r in data["answerable"] if r["language"] == language]
        negatives = [r for r in data["unanswerable"] if r["language"] == language]
        if not answerable:
            continue
        correct, wrong, none = [], [], []
        anchors = 0
        lines = []
        for row in answerable:
            shown = shown_as_answer(row)
            lines.append(sum(s["end_line"] - s["start_line"] + 1 for s in shown))
            if not shown:
                none.append(row["question"])
            elif any(touches(Span(s), span) for s in shown for span in gold[row["question"]]):
                correct.append(row["question"])
            else:
                wrong.append(row["question"])
            text = squeeze(shown_text(row))
            anchors += any(squeeze(span.anchor) in text for span in gold[row["question"]])
        refused = [r for r in negatives if r["refused"]]
        rows = answerable + negatives
        references = [(r["repo"], s) for r in rows for s in everything_shown(r)]
        invalid = [
            f"{s['path']}:{s['start_line']}-{s['end_line']}"
            for repo, s in references
            if not verifiers[repo].check(s["path"], s["start_line"], s["end_line"]).valid
        ]
        everything = [r["seconds"] for r in rows]
        print(f"  {language}: answerable {len(answerable)}")
        print(f"    correct source      {len(correct):>2}  {', '.join(correct)}")
        print(f"    wrong source        {len(wrong):>2}  {', '.join(wrong)}")
        print(f"    no answer           {len(none):>2}  {', '.join(none)}")
        print(f"    correct evidence not found {len(wrong) + len(none)}; answer rate {(len(correct) + len(wrong)) / len(answerable):.0%}; gold anchor in the lines shown {anchors}")
        if "gold_in_search" in answerable[0]:
            print(f"    gold reached search (8 chunks) {sum(r['gold_in_search'] for r in answerable)}, offered as passages {sum(r['gold_in_context'] for r in answerable)}")
        print(f"    lines shown as an answer: mean {sum(lines) / len(lines):.1f}")
        print(f"    unanswerable refused {len(refused)}/{len(negatives)}; answered: {', '.join(r['question'] for r in negatives if not r['refused']) or 'none'}")
        print(f"    references shown {len(references)}, invalid {len(invalid)} {invalid or ''}")
        print(f"    seconds: mean {sum(everything) / len(everything):.1f}, answerable {sum(r['seconds'] for r in answerable) / len(answerable):.1f}, negatives {sum(r['seconds'] for r in negatives) / len(negatives):.1f}, max {max(everything)}")
        print(f"    tokens in/out {sum(r['input_tokens'] or 0 for r in rows)}/{sum(r['output_tokens'] or 0 for r in rows)}")
        if "new" in label and language == "tr":
            # The same proportions on any set size, rounded towards the stricter side.
            most_wrong = len(answerable) // 6
            least_refused = -(-5 * len(negatives) // 6)
            least_correct = -(-len(answerable) // 2)
            checks = {
                f"wrong source at most {most_wrong}": len(wrong) <= most_wrong,
                f"unanswerable refused at least {least_refused}": len(refused) >= least_refused,
                "no invalid reference": not invalid,
                f"correct source at least {least_correct}": len(correct) >= least_correct,
            }
            print("    BAR: " + "; ".join(f"{name}: {'met' if met else 'NOT met'}" for name, met in checks.items()))
            print("    BAR " + ("PASSED" if all(checks.values()) else "NOT PASSED"))


for argument in sys.argv[1:]:
    label, name = argument.split("=", 1)
    report(label, name)

print("\n=== memory samples")
for path in sorted((RESULTS / "resources").glob("stats-pilot-*.txt")):
    memory, python, free = [], [], []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        found = re.match(r"\s*([\d.]+)(KiB|MiB|GiB|B) / ", line)
        if found:
            memory.append(float(found[1]) * UNITS[found[2]])
        found = re.search(r"python (\d+)MB", line)
        if found:
            python.append(int(found[1]))
        found = re.search(r"free ([\d.,]+)GB commit ([\d.,]+)GB", line)
        if found:
            free.append(min(float(found[1].replace(",", ".")), float(found[2].replace(",", "."))))
    if memory:
        print(f"{path.stem}: {len(memory)} samples, Ollama peak {max(memory):.2f} GiB, Python peak {max(python, default=0)} MB, lowest free {min(free, default=0):.1f} GB")
