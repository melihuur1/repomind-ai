"""Choosing among all passages at once against judging each one, with the real 7B model, on the same passages.

    python eval/tools/selection_trial.py --output rows.json

Development data only: every question here has been read, and several were picked because they
failed. Stored 7B translations stand in for translation, so both ways see exactly the same passages,
and only the choice differs. Nothing is written to the index or to the usage table.

It refuses to start with less than 12 GB of free memory and commit space, stops between questions
below 4 GB, and saves each question as it finishes, so that running it again continues.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8")

from dataclasses import replace  # noqa: E402

from repomind.config import Settings  # noqa: E402
from repomind.embeddings.provider import FastEmbedProvider  # noqa: E402
from repomind.evaluation.dataset import V2_DATASET_PATH, V4_DATASET_PATH, V5_DATASET_PATH, GoldSpan, Question, load_dataset  # noqa: E402
from repomind.evaluation.memory import available_memory_gb  # noqa: E402
from repomind.evaluation.metrics import touches  # noqa: E402
from repomind.qa.engine import QAEngine  # noqa: E402
from repomind.qa.llm import LLMResponse, OpenAICompatibleClient, load_llm_config  # noqa: E402
from repomind.qa.prompt import TRANSLATION_PROMPT  # noqa: E402
from repomind.search.engine import SearchConfig, SearchEngine  # noqa: E402
from repomind.search.translation import LocalTurkishTranslator  # noqa: E402
from repomind.store.database import Store  # noqa: E402

START_GB = 12.0
STOP_GB = 4.0
RESULTS = PROJECT / "eval" / "results"

# Earlier questions whose right lines were offered and whose choice went wrong or empty, and the
# unanswerable questions that whole units newly let through. Picked before this tool was run.
EXTRA_ANSWERABLE = ["sg-db-connection-error", "sg-oversell", "v2-sg-error-status-map", "v2-sn-redis-durability"]
EXTRA_UNANSWERABLE = ["v2-sg-neg-transfer", "v4-sg-neg-wishlist", "v4-sg-neg-reduce-quantity"]
# The question that started this work. Its accepted lines are the ones that prevent negative stock.
NEGATIVE_STOCK = Question(
    id="manual-negative-stock",
    repo="stockguard",
    tr="StockGuard stok miktarının negatife düşmesini nasıl engelliyor?",
    en="How does StockGuard prevent stock quantities from going negative?",
    gold=(
        GoldSpan("src/main/resources/db/migration/V2__catalog_and_stock.sql", 23, 25, "ck_stock_available_non_negative"),
        GoldSpan("src/main/java/com/stockguard/inventory/StockLevelRepository.java", 57, 58, "AND available_quantity >= :quantity"),
    ),
)
NEGATIVE_STOCK_ENGLISH = "How does StockGuard prevent stock quantities from going negative?"


def stored_translations() -> dict[str, str]:
    known = json.loads((RESULTS / "dev-phase3-translations.json").read_text(encoding="utf-8"))
    for name in ("v4-improved-7b-tr.json", "v5-7b-tr.json"):
        results = json.loads((RESULTS / name).read_text(encoding="utf-8"))
        known.update({row["question"]: row["translated_query"] for row in results["answerable"] + results["unanswerable"] if row.get("translated_query")})
    known[NEGATIVE_STOCK.id] = NEGATIVE_STOCK_ENGLISH
    return known


class StoredTranslation:
    """The real model for choosing, and the stored wording for translating."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.english: str | None = None

    def complete(self, system, user, max_output_tokens):
        if system == TRANSLATION_PROMPT:
            return LLMResponse(json.dumps({"english": self.english}), 1, 1)
        return self.inner.complete(system, user, max_output_tokens)


def questions() -> list[tuple[object, bool]]:
    fifth = load_dataset(V5_DATASET_PATH)
    by_id = {}
    for dataset in (load_dataset(), load_dataset(V2_DATASET_PATH), load_dataset(V4_DATASET_PATH)):
        by_id.update({q.id: q for q in (*dataset.questions, *dataset.unanswerable)})
    chosen = [(q, False) for q in fifth.questions] + [(q, True) for q in fifth.unanswerable]
    chosen += [(by_id[i], False) for i in EXTRA_ANSWERABLE] + [(by_id[i], True) for i in EXTRA_UNANSWERABLE]
    chosen.append((NEGATIVE_STOCK, False))
    return chosen


def outcome(result, question, negative: bool) -> str:
    if result.status == "invalid_response":
        return "invalid"
    if not result.citations:
        return "none"
    if negative:
        return "wrong"
    return "correct" if any(touches(c, s) for c in result.citations for s in question.accepted) else "wrong"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    done = {}
    if arguments.output.exists():
        done = {(r["id"], r["selection"]): r for r in json.loads(arguments.output.read_text(encoding="utf-8"))["rows"]}

    # Read twice, a second apart, and printed, so that a start above the limit can be seen afterwards.
    readings = [available_memory_gb()]
    time.sleep(1)
    readings.append(available_memory_gb())
    print(f"Free memory before the model loads: {', '.join('unknown' if r is None else f'{r:.2f} GB' for r in readings)}", flush=True)
    if any(r is None or r < START_GB for r in readings):
        print(f"Not started: this needs {START_GB:.0f} GB free, measured both times, before the model loads.")
        return 3

    config = load_llm_config()
    if config is None:
        print("No model is configured.")
        return 2
    settings = Settings.load()
    embedder = FastEmbedProvider(settings.embedding_model, settings.models_dir)
    translations = stored_translations()
    client = StoredTranslation(OpenAICompatibleClient(config))
    rows = list(done.values())
    with Store(settings.database_url, apply_schema=False) as store:
        store.record_llm_usage = lambda *args, **kwargs: None  # This trial writes nothing.
        search = SearchEngine(store, embedder, SearchConfig(), LocalTurkishTranslator(settings.models_dir))
        for question, negative in questions():
            for selection in ("choose", "judge"):
                if (question.id, selection) in done:
                    continue
                free = available_memory_gb()
                if free is not None and free < STOP_GB:
                    print(f"Stopped with {free:.1f} GB free. Run it again to continue.")
                    return 3
                client.english = translations.get(question.id)
                engine = QAEngine(store, search, replace(config, selection=selection), client)
                started = time.perf_counter()
                result = engine.ask(question.tr, [question.repo])
                accepted = () if negative else question.accepted
                row = {
                    "id": question.id,
                    "selection": selection,
                    "negative": negative,
                    "status": result.status,
                    "outcome": outcome(result, question, negative),
                    "gold_offered": any(touches(p, s) for p in result.passages for s in accepted),
                    "chosen": [f"{c.path}:{c.start_line}-{c.end_line}" for c in result.citations],
                    "review": [f"{p.path}:{p.start_line}-{p.end_line}" for p in result.review_candidates],
                    "judgements": result.judgements,
                    "offered": [f"{p.path}:{p.start_line}-{p.end_line}" for p in result.passages],
                    "note": result.note,
                    "requests": result.sources_sent if selection == "judge" else int(bool(result.sources_sent)),
                    "input_tokens": result.input_tokens,
                    "seconds": round(time.perf_counter() - started, 2),
                    "free_memory_gb": round(available_memory_gb() or 0, 2),
                }
                rows.append(row)
                arguments.output.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                print(f"{question.id:<32} {selection:<7} {row['outcome']:<8} offered gold={row['gold_offered']!s:<5} {row['seconds']:>5}s {row['judgements']}", flush=True)

    for selection in ("choose", "judge"):
        group = [r for r in rows if r["selection"] == selection]
        answerable = [r for r in group if not r["negative"]]
        offered = [r for r in answerable if r["gold_offered"]]
        count = lambda items, value: sum(r["outcome"] == value for r in items)  # noqa: E731
        print(
            f"{selection}: answerable {len(answerable)}: correct {count(answerable, 'correct')}, wrong {count(answerable, 'wrong')}, none {count(answerable, 'none')}, invalid {count(answerable, 'invalid')}"
            f" | right lines offered {len(offered)}: correct {count(offered, 'correct')}, wrong {count(offered, 'wrong')}"
            f" | unanswerable given a source {sum(r['outcome'] == 'wrong' for r in group if r['negative'])} of {sum(r['negative'] for r in group)}"
            f" | mean {sum(r['seconds'] for r in group) / max(1, len(group)):.1f} s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
