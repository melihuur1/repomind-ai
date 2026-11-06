"""Where did each pilot question go wrong: search, passage selection, or the model's choice? Calls no model.

Usage: pilot_diagnosis.py <result file> [...]
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

RESULTS = ROOT / "eval" / "results"


def short(rows):
    return ", ".join(r["question"].replace("v3-", "") for r in rows) or "none"


def how_it_ended(row):
    """The model's reply is not stored, but each way of ending leaves a different trace."""
    if row["status"] == "answered":
        return "answered"
    if not row["sources_sent"]:
        return "refused by a rule, the model was not asked"
    if row["rejected"]:
        return "the model chose lines, and the evidence rule dropped them"
    return "the model declined"


for name in sys.argv[1:]:
    data = json.loads((RESULTS / name).read_text(encoding="utf-8"))
    answerable, negatives = data["answerable"], data["unanswerable"]
    print(f"\n{name} ({data['model']})")

    missed_by_search = [r for r in answerable if not r["gold_in_search"]]
    missed_by_passages = [r for r in answerable if r["gold_in_search"] and not r["gold_in_context"]]
    offered = [r for r in answerable if r["gold_in_context"]]
    print(f"  Where the {len(answerable)} answerable questions ended")
    print(f"    search missed the gold code           {len(missed_by_search):>2}  {short(missed_by_search)}")
    print(f"    in the chunks, not in the passages    {len(missed_by_passages):>2}  {short(missed_by_passages)}")
    print(f"    offered to the model                  {len(offered):>2}")
    print(f"      chosen correctly                    {sum(r['cites_gold'] for r in offered):>2}  {short([r for r in offered if r['cites_gold']])}")
    wrong = [r for r in offered if r["status"] == "answered" and not r["cites_gold"]]
    print(f"      the model chose other lines         {len(wrong):>2}  {short(wrong)}")
    for ending in ("the model declined", "the model chose lines, and the evidence rule dropped them", "refused by a rule, the model was not asked"):
        rows = [r for r in offered if how_it_ended(r) == ending]
        print(f"      {ending:<36}{len(rows):>2}  {short(rows)}")

    print("  When the gold code was NOT offered, what happened (declining is right here)")
    absent = missed_by_search + missed_by_passages
    print(f"    answered anyway from other lines      {sum(r['status'] == 'answered' for r in absent):>2}  {short([r for r in absent if r['status'] == 'answered'])}")
    for ending in ("the model declined", "the model chose lines, and the evidence rule dropped them", "refused by a rule, the model was not asked"):
        rows = [r for r in absent if how_it_ended(r) == ending]
        print(f"    {ending:<38}{len(rows):>2}  {short(rows)}")

    print(f"  The {len(negatives)} unanswerable questions")
    for ending in ("answered", "the model declined", "the model chose lines, and the evidence rule dropped them", "refused by a rule, the model was not asked"):
        rows = [r for r in negatives if how_it_ended(r) == ending]
        print(f"    {ending:<38}{len(rows):>2}  {short(rows)}")

    print("  Sources to review, which are never counted as answers")
    with_candidates = [r for r in answerable + negatives if r.get("review_candidates")]
    print(f"    questions with one                    {len(with_candidates):>2}  {short(with_candidates)}")
    gold_candidates = [r for r in answerable if r.get("review_candidate_is_gold")]
    print(f"    the right lines, turned down by the rule {len(gold_candidates):>2}  {short(gold_candidates)}")

    rows = answerable + negatives
    asked = [r for r in rows if r["sources_sent"]]
    print(f"  The model was asked {len(asked)} times and declined {sum(how_it_ended(r) == 'the model declined' for r in asked)} times")
    print(f"  Notes kept {sum(bool(r['note']) for r in rows)}, left out {sum(r['note_problem'] is not None for r in rows)}")
    print(f"  Anchor in the offered passages {sum(r['anchor_in_passages'] for r in answerable)}, in the chosen lines {sum(r['anchor_in_chosen'] for r in answerable)}")
    free = [r["free_memory_gb"] for r in rows if r.get("free_memory_gb") is not None]
    if free:
        print(f"  Free memory after each question: lowest {min(free)} GB, highest {max(free)} GB")
    print("  Translations")
    for row in rows:
        print(f"    {row['question'].replace('v3-', ''):<28}{row['translated_query']}")
