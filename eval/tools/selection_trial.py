"""Choosing among all passages at once against judging each one, with the real 7B model, on the same passages.

    python eval/tools/selection_trial.py --output rows.json

The rules, and what decides between the two, are in eval/SELECTION_TRIAL.md, written before this ran.

Development data only: every question here has been read, and several were picked because they
failed. Stored 7B translations stand in for translation, so both ways see exactly the same passages,
and only the choice differs. Nothing is written to the index or to the usage table.

It starts only when two readings a second apart both show 12 GB of free physical memory and 12 GB
of free commit space, stops between questions below 4 GB, and saves each question as it finishes.
Running it again on the same file continues, and checks memory again first.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8")

from dataclasses import replace  # noqa: E402

from repomind.config import Settings  # noqa: E402
from repomind.embeddings.provider import FastEmbedProvider  # noqa: E402
from repomind.evaluation.dataset import V2_DATASET_PATH, V4_DATASET_PATH, V5_DATASET_PATH, GoldSpan, Question, load_dataset  # noqa: E402
from repomind.evaluation.memory import available_memory_gb, check_before_loading, read_memory  # noqa: E402
from repomind.evaluation.metrics import touches  # noqa: E402
from repomind.qa.engine import QAEngine  # noqa: E402
from repomind.qa.llm import LLMResponse, OpenAICompatibleClient, load_llm_config  # noqa: E402
from repomind.qa.prompt import TRANSLATION_PROMPT  # noqa: E402
from repomind.search.engine import SearchConfig, SearchEngine  # noqa: E402
from repomind.search.translation import LocalTurkishTranslator  # noqa: E402
from repomind.store.database import Store  # noqa: E402

STOP_GB = 4.0
RESULTS = PROJECT / "eval" / "results"
SELECTIONS = ("choose", "judge")

# Earlier questions whose right lines were offered and whose choice went wrong or empty, and the
# unanswerable questions that whole units newly let through. Picked before this tool was run.
EXTRA_ANSWERABLE = ["sg-db-connection-error", "sg-oversell", "v2-sg-error-status-map", "v2-sn-redis-durability"]
EXTRA_UNANSWERABLE = ["v2-sg-neg-transfer", "v4-sg-neg-wishlist", "v4-sg-neg-reduce-quantity"]
# The question that started this work. Its accepted lines are the ones that prevent negative stock.
# It is reported on its own and left out of the totals, so that one example cannot move them.
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
    """The real model for choosing, the stored wording for translating, and a count of what the model was sent."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.english: str | None = None
        self.reset()

    def reset(self) -> None:
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def complete(self, system, user, max_output_tokens):
        if system == TRANSLATION_PROMPT:
            return LLMResponse(json.dumps({"english": self.english}), 0, 0)
        response = self.inner.complete(system, user, max_output_tokens)
        self.requests += 1
        self.input_tokens += response.input_tokens or 0
        self.output_tokens += response.output_tokens or 0
        return response


def _gpu_memory_mib() -> int | None:
    try:
        reply = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        return int(reply.stdout.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


class Resources:
    """Samples GPU memory in use and free memory, once a second, while one question runs."""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self.gpu: list[int] = []
        self.physical: list[float] = []
        self.commit: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _sample(self) -> None:
        gpu, memory = _gpu_memory_mib(), read_memory()
        if gpu is not None:
            self.gpu.append(gpu)
        if memory.physical_gb is not None:
            self.physical.append(memory.physical_gb)
        if memory.commit_gb is not None:
            self.commit.append(memory.commit_gb)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._sample()

    def __enter__(self) -> "Resources":
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join()
        self._sample()

    def summary(self) -> dict:
        return {
            "peak_gpu_mib": max(self.gpu, default=None),
            "min_free_physical_gb": round(min(self.physical), 2) if self.physical else None,
            "min_free_commit_gb": round(min(self.commit), 2) if self.commit else None,
        }


def questions() -> list[tuple[Question, bool]]:
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


def _span(text: str) -> GoldSpan:
    path, lines = text.rsplit(":", 1)
    start, end = lines.split("-")
    return GoldSpan(path, int(start), int(end), "")


def negative_stock_facts(row: dict) -> dict:
    """The three things asked about the question that started this work."""
    constraint, guard = NEGATIVE_STOCK.gold
    return {
        "chose_simulation_service": any(c.split(":")[0].endswith("SimulationService.java") for c in row["chosen"]),
        "chose_check_constraint": any(touches(_span(c), constraint) for c in row["chosen"]),
        "offered_check_constraint": any(touches(_span(p), constraint) for p in row["offered"]),
        "offered_repository_guard": any(touches(_span(p), guard) for p in row["offered"]),
        "chose_repository_guard": any(touches(_span(c), guard) for c in row["chosen"]),
    }


def summarise(rows: list[dict]) -> dict:
    """The figures eval/SELECTION_TRIAL.md decides on, for each way, over the questions both ways finished."""
    by_key = {(r["id"], r["selection"]): r for r in rows}
    both = sorted({r["id"] for r in rows if all((r["id"], s) in by_key for s in SELECTIONS)})
    counted = [i for i in both if i != NEGATIVE_STOCK.id]
    report: dict = {"questions_compared": len(counted), "passages_differ": [i for i in both if by_key[(i, "choose")]["offered"] != by_key[(i, "judge")]["offered"]]}
    for selection in SELECTIONS:
        group = [by_key[(i, selection)] for i in counted]
        answerable = [r for r in group if not r["negative"]]
        negatives = [r for r in group if r["negative"]]
        offered = [r for r in answerable if r["gold_offered"]]
        seconds = [r["seconds"] for r in group]
        count = lambda items, value: sum(r["outcome"] == value for r in items)  # noqa: E731
        report[selection] = {
            "answerable": len(answerable),
            "correct": count(answerable, "correct"),
            "wrong": count(answerable, "wrong"),
            "no_answer": count(answerable, "none"),
            "invalid": count(answerable, "invalid") + count(negatives, "invalid"),
            "right_lines_offered": len(offered),
            "refused_with_right_lines_offered": count(offered, "none") + count(offered, "invalid"),
            "unanswerable": len(negatives),
            "unanswerable_given_a_source": count(negatives, "wrong"),
            "wrong_in_all": count(answerable, "wrong") + count(negatives, "wrong"),
            "seconds_mean": round(statistics.mean(seconds), 1) if seconds else None,
            "seconds_median": round(statistics.median(seconds), 1) if seconds else None,
            "seconds_max": max(seconds, default=None),
            "requests_mean": round(statistics.mean(r["requests"] for r in group), 2) if group else None,
            "input_tokens_mean": round(statistics.mean(r["input_tokens"] for r in group)) if group else None,
            "output_tokens_mean": round(statistics.mean(r["output_tokens"] for r in group)) if group else None,
            "peak_gpu_mib": max((r["peak_gpu_mib"] for r in group if r.get("peak_gpu_mib") is not None), default=None),
            "min_free_physical_gb": min((r["min_free_physical_gb"] for r in group if r.get("min_free_physical_gb") is not None), default=None),
            "min_free_commit_gb": min((r["min_free_commit_gb"] for r in group if r.get("min_free_commit_gb") is not None), default=None),
        }
    report["negative_stock"] = {s: negative_stock_facts(by_key[(NEGATIVE_STOCK.id, s)]) for s in SELECTIONS if (NEGATIVE_STOCK.id, s) in by_key}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    check = check_before_loading()
    print(f"Free memory before the model loads: {check.describe()}", flush=True)
    if check.refusal:
        print(f"Not started: {check.refusal}. Nothing was loaded.")
        return 3

    done = {}
    if arguments.output.exists():
        done = {(r["id"], r["selection"]): r for r in json.loads(arguments.output.read_text(encoding="utf-8"))["rows"]}
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
            for selection in SELECTIONS:
                if (question.id, selection) in done:
                    continue
                free = available_memory_gb()
                if free is not None and free < STOP_GB:
                    print(f"Stopped with {free:.1f} GB free. Run it again to continue.")
                    return 3
                client.english = translations.get(question.id)
                client.reset()
                engine = QAEngine(store, search, replace(config, selection=selection), client)
                with Resources() as resources:
                    started = time.perf_counter()
                    result = engine.ask(question.tr, [question.repo])
                    seconds = round(time.perf_counter() - started, 2)
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
                    "requests": client.requests,
                    "input_tokens": client.input_tokens,
                    "output_tokens": client.output_tokens,
                    "seconds": seconds,
                    **resources.summary(),
                }
                rows.append(row)
                arguments.output.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                print(f"{question.id:<32} {selection:<7} {row['outcome']:<8} offered gold={row['gold_offered']!s:<5} {row['seconds']:>5}s {row['judgements']}", flush=True)

    report = summarise(rows)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
