"""How far the right code gets before any model, on every question set so far. No LLM.

    python eval/tools/accuracy_funnel.py [--output rows.json]

Stored 7B translations stand in for the model's, so nothing is loaded and nothing is random.
All of these questions have been read during development, so this measures development progress
and proves nothing independent. It never writes to the index.

Per answerable question: is the right code among the search candidates, in a passage, wholly
inside one passage rather than cut by it, and in a passage that passes the evidence rule.
Per unanswerable question: does it reach the model, where it could be given a wrong source.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8")

from repomind.config import Settings  # noqa: E402
from repomind.embeddings.provider import FastEmbedProvider  # noqa: E402
from repomind.evaluation.dataset import (  # noqa: E402
    V2_DATASET_PATH,
    V3_DATASET_PATH,
    V4_DATASET_PATH,
    V5_DATASET_PATH,
    load_dataset,
    load_holdout_unanswerable,
)
from repomind.evaluation.metrics import overlaps, touches  # noqa: E402
from repomind.qa.passages import is_evidence, select_passages  # noqa: E402
from repomind.qa.sufficiency import check_sufficiency  # noqa: E402
from repomind.search.engine import SearchConfig, SearchEngine  # noqa: E402
from repomind.search.translation import LocalTurkishTranslator  # noqa: E402
from repomind.store.database import Store  # noqa: E402

CHUNKS = 12
SEATS = 5
RESULTS = PROJECT / "eval" / "results"


def stored_translations() -> dict[str, str]:
    known = json.loads((RESULTS / "dev-phase3-translations.json").read_text(encoding="utf-8"))
    for name in ("v4-improved-7b-tr.json", "v5-7b-tr.json"):
        results = json.loads((RESULTS / name).read_text(encoding="utf-8"))
        known.update({row["question"]: row["translated_query"] for row in results["answerable"] + results["unanswerable"] if row.get("translated_query")})
    return known


def question_sets() -> dict[str, tuple]:
    first = load_dataset()
    sets = {"first": (first.questions, (*first.unanswerable, *load_holdout_unanswerable()))}
    for name, path in (("second", V2_DATASET_PATH), ("third", V3_DATASET_PATH), ("fourth", V4_DATASET_PATH), ("fifth", V5_DATASET_PATH)):
        dataset = load_dataset(path)
        sets[name] = (dataset.questions, dataset.unanswerable)
    return sets


def squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.output and arguments.output.exists():
        parser.error(f"{arguments.output} already exists, and is not overwritten.")

    translations = stored_translations()
    settings = Settings.load()
    embedder = FastEmbedProvider(settings.embedding_model, settings.models_dir)
    rows: list[dict] = []
    started = time.perf_counter()
    with Store(settings.database_url, apply_schema=False) as store:
        engine = SearchEngine(store, embedder, SearchConfig(), LocalTurkishTranslator(settings.models_dir))
        engine.search("warm up", "hybrid", ["stockguard"], 1)
        for language in ("tr", "en"):
            for set_name, (answerable, negatives) in question_sets().items():
                for question in (*answerable, *negatives):
                    if language == "tr":
                        asked, english, second = question.tr, translations.get(question.id), engine.local_translation(question.tr, [])
                        if english is None:
                            english, second = second, None
                    else:
                        asked, english, second = question.en, None, None
                    found = engine.search(asked, "hybrid", [question.repo], CHUNKS, translated=english, more_wordings=[second] if second else (), glossary_seats=SEATS)
                    vocabulary = engine.vocabulary(asked, found.translated_query, [question.repo])
                    gate = check_sufficiency(asked, found.translated_query, vocabulary).insufficient
                    wording = found.translated_query or asked
                    queries = [text for text in (asked, found.translated_query, second) if text]
                    chunks = [hit.chunk for hit in found.hits]
                    passages = [] if gate else select_passages(chunks, queries, embedder, more_terms=found.glossary_terms)
                    good = [p for p in passages if is_evidence(p, wording)]
                    row = {
                        "language": language,
                        "set": set_name,
                        "id": question.id,
                        "negative": not hasattr(question, "accepted"),
                        "gate": gate,
                        "reaches_model": not gate and bool(good),
                        "passages_offered": len(passages),
                        "lines_shown": sum(p.end_line - p.start_line + 1 for p in passages),
                        "offered": [[p.path, p.start_line, p.end_line, is_evidence(p, wording)] for p in passages],
                    }
                    accepted = getattr(question, "accepted", ())
                    if accepted:
                        text = squeeze("\n".join(p.text for p in passages))
                        row.update(
                            candidates=any(overlaps(c, s) for c in chunks for s in accepted),
                            passages=any(touches(p, s) for p in passages for s in accepted),
                            whole=any(p.path == s.path and p.start_line <= s.start_line and s.end_line <= p.end_line for p in passages for s in accepted),
                            anchor=any(squeeze(s.anchor) in text for s in accepted),
                            evidence=any(touches(p, s) for p in good for s in accepted),
                        )
                    rows.append(row)

    def summary(group: list[dict]) -> dict:
        answerable = [r for r in group if not r["negative"]]
        negatives = [r for r in group if r["negative"]]
        count = lambda key: sum(bool(r[key]) for r in answerable)  # noqa: E731
        return {
            "answerable": len(answerable),
            "candidates": count("candidates"),
            "passages": count("passages"),
            "whole": count("whole"),
            "anchor": count("anchor"),
            "evidence": count("evidence"),
            "refused": sum(not r["reaches_model"] for r in answerable),
            "unanswerable": len(negatives),
            "unanswerable_reaching_model": sum(r["reaches_model"] for r in negatives),
            "mean_lines_shown": round(sum(r["lines_shown"] for r in answerable) / max(1, len(answerable)), 1),
        }

    by_language: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_language[row["language"]].append(row)
    report = {language: summary(group) for language, group in by_language.items()}
    print(f"{'':<4}{'n':>5}{'cand':>6}{'pass':>6}{'whole':>7}{'anch':>6}{'evid':>6}{'refused':>9}{'neg->model':>12}{'lines':>7}")
    for language, item in report.items():
        print(
            f"{language:<4}{item['answerable']:>5}{item['candidates']:>6}{item['passages']:>6}{item['whole']:>7}{item['anchor']:>6}{item['evidence']:>6}"
            f"{item['refused']:>9}{item['unanswerable_reaching_model']:>8}/{item['unanswerable']:<3}{item['mean_lines_shown']:>7}"
        )
    print(f"{time.perf_counter() - started:.0f} s")
    if arguments.output:
        arguments.output.write_text(json.dumps({"summary": report, "questions": rows}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
