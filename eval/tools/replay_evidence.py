"""Replays stored model readings through candidate evidence rules. Calls no model at all.

Both data sets have been read before, so these numbers guide design. They do not measure it.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from repomind.evaluation.dataset import load_dataset, load_holdout_unanswerable  # noqa: E402
from repomind.search.tokenizer import tokenize  # noqa: E402

RESULTS = ROOT / "eval" / "results"
_BACKTICKED = re.compile(r"`([^`]+)`")
_IDENTIFIER = re.compile(r"\b(?:[a-z]+[A-Z]\w*|[A-Z][a-z]+[A-Z]\w*|[A-Za-z]+_\w+|[A-Z]{3,}\w*)\b")
_NUMBER = re.compile(r"\b\d[\d_.,]*\d\b")
_VALUE = re.compile(r"(?<![\w.])\d(?:[\d_.,]*\d)?(?![\w])")
_WORD = re.compile(r"[A-Za-z_][\w-]*|\d[\d_.,]*")


def squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def technical_claims(statement: str) -> set[str]:
    claims = set(_IDENTIFIER.findall(statement)) | set(_NUMBER.findall(statement))
    for quoted in _BACKTICKED.findall(statement):
        claims.update(_WORD.findall(quoted))
    return {c for c in claims if len(c) > 1}


def claims_in_quote(reading: dict) -> bool:
    quote = squeeze(reading["quote"])
    return all(squeeze(claim) in quote for claim in technical_claims(reading["statement"]))


def values_in_quote(reading: dict) -> bool:
    """Only numbers, because a true statement often names its class, which the quote need not contain."""
    quote = squeeze(reading["quote"])
    return all(squeeze(value) in quote for value in _VALUE.findall(reading["statement"]))


def shared_terms(reading: dict, terms: set[str]) -> int:
    return len(terms & set(tokenize(reading["quote"])))


def v2_rows() -> tuple[list[dict], dict]:
    dataset = load_dataset(ROOT / "eval" / "questions_v2.yaml")
    gold = {q.id: q.gold for q in dataset.questions}
    english = {q.id: q.text("en") for q in (*dataset.questions, *dataset.unanswerable)}
    data = json.loads((RESULTS / "v2-ask-phase2-3b.json").read_text(encoding="utf-8"))
    rows = []
    for row in data["answerable"] + data["unanswerable"]:
        qid = row["question"]
        readings = []
        for r in row["readings"]:
            is_gold = any(
                r["path"] == s.path and r["start_line"] <= s.end_line and s.start_line <= r["end_line"]
                for s in gold.get(qid, ())
            )
            readings.append({**r, "gold": is_gold})
        rows.append(
            {"id": qid, "answerable": qid in gold, "question": row["translated_query"] or english[qid], "readings": readings}
        )
    grades = {
        g["question"]: g["grade"]
        for g in json.loads((RESULTS / "v2-grades-3b-tr.json").read_text(encoding="utf-8"))
        if g["run"] == "v2-ask-phase2-3b.json"
    }
    return rows, grades


def dev_rows() -> tuple[list[dict], dict]:
    dataset = load_dataset(ROOT / "eval" / "questions.yaml")
    all_questions = (*dataset.questions, *dataset.unanswerable, *load_holdout_unanswerable())
    # The model's own translations were not stored in this run, so the hand-written English stands in.
    english = {q.id: q.text("en") for q in all_questions}
    rows = json.loads((RESULTS / "dev-phase2-7b-tr.json").read_text(encoding="utf-8"))
    return [
        {"id": r["id"], "answerable": r["answerable"], "question": english[r["id"]], "readings": r["readings"]} for r in rows
    ], {}


def show_ranks(name: str, rows: list[dict]) -> None:
    print(f"\n{name}: by retrieval rank (read / gold read / statements / gold statements)")
    table: dict[int, Counter] = {}
    for row in rows:
        if not row["answerable"]:
            continue
        for rank, r in enumerate(row["readings"], start=1):
            counts = table.setdefault(rank, Counter())
            counts["read"] += 1
            counts["gold"] += r["gold"]
            counts["said"] += r["outcome"] == "supported"
            counts["gold_said"] += r["gold"] and r["outcome"] == "supported"
    for rank, c in sorted(table.items()):
        print(f"  {rank}: {c['read']:>3} / {c['gold']:>2} / {c['said']:>3} / {c['gold_said']:>2}")


def replay(name: str, rows: list[dict], grades: dict) -> None:
    rules = {
        "as it is today": (lambda r, t: True, None),
        "numbers all in the quote": (lambda r, t: values_in_quote(r), None),
        "numbers in quote, 1+ terms": (lambda r, t: values_in_quote(r) and shared_terms(r, t) >= 1, None),
        "numbers in quote, 1+ terms, top 2": (lambda r, t: values_in_quote(r) and shared_terms(r, t) >= 1, 2),
        "top 3 by retrieval rank": (lambda r, t: True, 3),
        "quote shares 1+ question terms": (lambda r, t: shared_terms(r, t) >= 1, None),
        "quote shares 2+ question terms": (lambda r, t: shared_terms(r, t) >= 2, None),
        "technical claims all in the quote": (lambda r, t: claims_in_quote(r), None),
        "claims in quote, 1+ terms": (lambda r, t: claims_in_quote(r) and shared_terms(r, t) >= 1, None),
        "claims in quote, 2+ terms": (lambda r, t: claims_in_quote(r) and shared_terms(r, t) >= 2, None),
        "claims in quote, 1+ terms, top 3": (lambda r, t: claims_in_quote(r) and shared_terms(r, t) >= 1, 3),
        "claims in quote, 2+ terms, top 3": (lambda r, t: claims_in_quote(r) and shared_terms(r, t) >= 2, 3),
    }
    answerable = [r for r in rows if r["answerable"]]
    negatives = [r for r in rows if not r["answerable"]]
    print(f"\n{name}: {len(answerable)} answerable, {len(negatives)} negative")
    print(f"  {'rule':<36}{'answered':>9}{'gold ans':>9}{'gold st':>8}{'other st':>9}{'prec':>6}{'neg ans':>8}{'st/ans':>7}{'wrong->ref':>11}{'part->ref':>10}")
    for label, (keep, cap) in rules.items():
        answered = gold_answers = gold_st = other_st = negative_answers = wrong_gone = partial_gone = 0
        for row in rows:
            terms = set(tokenize(row["question"]))
            kept = [r for r in row["readings"] if r["outcome"] == "supported" and keep(r, terms)]
            kept = kept[:cap] if cap else kept
            if not row["answerable"]:
                negative_answers += bool(kept)
                continue
            answered += bool(kept)
            gold_answers += any(r["gold"] for r in kept)
            gold_st += sum(r["gold"] for r in kept)
            other_st += sum(not r["gold"] for r in kept)
            grade = grades.get(row["id"])
            wrong_gone += grade == "wrong" and not kept
            partial_gone += grade == "partial" and not kept
        total = gold_st + other_st
        print(
            f"  {label:<36}{answered:>9}{gold_answers:>9}{gold_st:>8}{other_st:>9}"
            f"{gold_st / max(total, 1):>6.2f}{negative_answers:>8}{total / max(answered, 1):>7.1f}{wrong_gone:>11}{partial_gone:>10}"
        )


v2, v2_grades = v2_rows()
dev, _ = dev_rows()
show_ranks("3B, second set", v2)
show_ranks("7B, first set, Turkish", dev)
replay("3B, second set", v2, v2_grades)
replay("7B, first set, Turkish", dev, {})

print("\nHow much each model was given, and how well it told sources apart (answerable questions)")
for name, rows in (("3B, second set", v2), ("7B, first set", dev)):
    readings = [r for row in rows if row["answerable"] for r in row["readings"]]
    gold = [r for r in readings if r["gold"]]
    other = [r for r in readings if not r["gold"]]
    said = lambda group: sum(r["outcome"] == "supported" for r in group)  # noqa: E731
    print(
        f"  {name}: gold sources gave a statement {said(gold)}/{len(gold)} ({said(gold) / len(gold):.0%}), "
        f"other sources {said(other)}/{len(other)} ({said(other) / len(other):.0%}); "
        f"gold in the first 4 sources {sum(r['gold'] for row in rows if row['answerable'] for r in row['readings'][:4])}/{len(gold)}"
    )
lines = [r["end_line"] - r["start_line"] + 1 for row in v2 for r in row["readings"]]
print(f"  lines of code per source read: mean {sum(lines) / len(lines):.0f}, so about {8 * sum(lines) / len(lines):.0f} per question")

print("\nStatements with a technical claim missing from their own quote")
for name, rows in (("3B", v2), ("7B", dev)):
    said = [r for row in rows for r in row["readings"] if r["outcome"] == "supported"]
    missing = [r for r in said if not claims_in_quote(r)]
    print(f"  {name}: {len(missing)} of {len(said)}; gold {sum(r['gold'] for r in missing)} of {sum(r['gold'] for r in said)}")
    for r in missing[:4]:
        absent = sorted(c for c in technical_claims(r["statement"]) if squeeze(c) not in squeeze(r["quote"]))
        print(f"    {'gold ' if r['gold'] else 'other'} missing {absent[:4]}")
