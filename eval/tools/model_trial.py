"""Two local models choosing among the same passages: one run per model, then a comparison.

    python eval/tools/model_trial.py run --model qwen2.5:7b --output a.json
    python eval/tools/model_trial.py run --model qwen2.5-coder:7b --output b.json
    python eval/tools/model_trial.py compare a.json b.json

The rules, and what would count as better, are in eval/MODEL_TRIAL.md, written before any run.

Only the model differs. Both get the same 32 development questions, the same index, the same
stored 7B translations, so the same search and the same passages, and the same `choose` prompt
with the same limits and temperature 0. The random nonce in the prompt is fixed per question, so
the two models receive byte-identical prompts, and `compare` refuses to compare if any differ.

A run starts only when two readings a second apart both show 12 GB of free physical memory and
12 GB of free commit space, and when no other model is in the model server's memory. It stops
between questions below 4 GB, saves every question as it finishes, and unloads its model at the end.
Development data: every question here has been read. Nothing is written to the index.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import selection_trial as shared  # noqa: E402  (its questions, translations and resource sampling)

from repomind.config import Settings  # noqa: E402
from repomind.embeddings.provider import FastEmbedProvider  # noqa: E402
from repomind.evaluation.memory import available_memory_gb, check_before_loading  # noqa: E402
from repomind.evaluation.metrics import touches  # noqa: E402
from repomind.qa import engine as engine_module  # noqa: E402
from repomind.qa.engine import QAEngine  # noqa: E402
from repomind.qa.llm import OpenAICompatibleClient, load_llm_config  # noqa: E402
from repomind.search.engine import SearchConfig, SearchEngine  # noqa: E402
from repomind.search.translation import LocalTurkishTranslator  # noqa: E402
from repomind.store.database import Store  # noqa: E402

STOP_GB = 4.0


def fixed_nonce(identifier: str) -> str:
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:16]


class Recording(shared.StoredTranslation):
    """Also keeps a hash of the prompt each model is sent, so the two runs can be shown to match."""

    def reset(self) -> None:
        super().reset()
        self.prompts: list[str] = []

    def complete(self, system, user, max_output_tokens):
        if system != shared.TRANSLATION_PROMPT:
            self.prompts.append(hashlib.sha256(f"{system}\0{user}\0{max_output_tokens}".encode("utf-8")).hexdigest())
        return super().complete(system, user, max_output_tokens)


def _server(config) -> str:
    return config.base_url[: -len("/v1")] if config.base_url.endswith("/v1") else config.base_url


def loaded_models(server: str) -> list[dict]:
    with urllib.request.urlopen(f"{server}/api/ps", timeout=10) as reply:
        return json.load(reply).get("models", [])


def unload(server: str, model: str) -> None:
    body = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
    request = urllib.request.Request(f"{server}/api/generate", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60):
        pass


def run(model: str, output: Path, only: set[str] | None = None) -> int:
    check = check_before_loading()
    print(f"Free memory before the model loads: {check.describe()}", flush=True)
    if check.refusal:
        print(f"Not started: {check.refusal}. Nothing was loaded.")
        return 3
    base = load_llm_config()
    if base is None:
        print("No model is configured.")
        return 2
    config = replace(base, model=model, selection="choose")
    server = _server(config)
    others = [m["name"] for m in loaded_models(server) if m["name"] != model]
    if others:
        print(f"Not started: {', '.join(others)} is still in memory. Two models are never loaded at once.")
        return 3

    done = {}
    if output.exists():
        saved = json.loads(output.read_text(encoding="utf-8"))
        if saved["model"] != model:
            print(f"{output} holds a run of {saved['model']}, not {model}.")
            return 2
        done = {row["id"]: row for row in saved["rows"]}
    record = {"model": model, "settings": {"max_output_tokens": config.max_output_tokens, "max_input_tokens": config.max_input_tokens, "temperature": 0, "selection": "choose"},
              "memory_at_start": check.describe(), "placement": None, "rows": list(done.values())}
    settings = Settings.load()
    embedder = FastEmbedProvider(settings.embedding_model, settings.models_dir)
    translations = shared.stored_translations()
    client = Recording(OpenAICompatibleClient(config))
    try:
        with Store(settings.database_url, apply_schema=False) as store:
            store.record_llm_usage = lambda *args, **kwargs: None  # This trial writes nothing.
            search = SearchEngine(store, embedder, SearchConfig(), LocalTurkishTranslator(settings.models_dir))
            for question, negative in shared.questions():
                if question.id in done or (only and question.id not in only):
                    continue
                free = available_memory_gb()
                if free is not None and free < STOP_GB:
                    print(f"Stopped with {free:.1f} GB free. Run it again to continue.")
                    return 3
                client.english = translations.get(question.id)
                client.reset()
                engine_module.new_nonce = lambda identifier=question.id: fixed_nonce(identifier)
                qa = QAEngine(store, search, config, client)
                with shared.Resources() as resources:
                    started = time.perf_counter()
                    result = qa.ask(question.tr, [question.repo])
                    seconds = round(time.perf_counter() - started, 2)
                if record["placement"] is None and client.requests:
                    placed = next((m for m in loaded_models(server) if m["name"] == model), None)
                    if placed:
                        record["placement"] = {"size_mib": round(placed["size"] / 2**20), "in_gpu_mib": round(placed.get("size_vram", 0) / 2**20)}
                accepted = () if negative else question.accepted
                row = {
                    "id": question.id,
                    "negative": negative,
                    "status": result.status,
                    "outcome": shared.outcome(result, question, negative),
                    "gold_offered": any(touches(p, s) for p in result.passages for s in accepted),
                    "chosen": [f"{c.path}:{c.start_line}-{c.end_line}" for c in result.citations],
                    "review": [f"{p.path}:{p.start_line}-{p.end_line}" for p in result.review_candidates],
                    "offered": [f"{p.path}:{p.start_line}-{p.end_line}" for p in result.passages],
                    "note": result.note,
                    "prompts": client.prompts,
                    "requests": client.requests,
                    "input_tokens": client.input_tokens,
                    "output_tokens": client.output_tokens,
                    "seconds": seconds,
                    **resources.summary(),
                }
                record["rows"].append(row)
                output.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                print(f"{question.id:<32} {row['outcome']:<8} offered gold={row['gold_offered']!s:<5} {seconds:>6}s", flush=True)
    finally:
        unload(server, model)
        left = [m["name"] for m in loaded_models(server)]
        print(f"Unloaded {model}. In memory now: {', '.join(left) or 'nothing'}.")
    return 0


def summary(rows: list[dict]) -> dict:
    counted = [r for r in rows if r["id"] != shared.NEGATIVE_STOCK.id]
    answerable = [r for r in counted if not r["negative"]]
    negatives = [r for r in counted if r["negative"]]
    offered = [r for r in answerable if r["gold_offered"]]
    seconds = [r["seconds"] for r in counted]
    asked = [r for r in counted if r["requests"]]
    count = lambda items, value: sum(r["outcome"] == value for r in items)  # noqa: E731
    return {
        "answerable": len(answerable),
        "correct": count(answerable, "correct"),
        "wrong": count(answerable, "wrong"),
        "no_answer": count(answerable, "none"),
        "right_lines_offered": len(offered),
        "wrong_with_right_lines_offered": count(offered, "wrong"),
        "refused_with_right_lines_offered": count(offered, "none") + count(offered, "invalid"),
        "unanswerable": len(negatives),
        "unanswerable_given_a_source": count(negatives, "wrong"),
        "wrong_in_all": count(answerable, "wrong") + count(negatives, "wrong"),
        "invalid": count(counted, "invalid"),
        "failed_turkish_questions": sorted(r["id"] for r in answerable if r["outcome"] != "correct") + sorted(r["id"] for r in negatives if r["outcome"] == "wrong"),
        "seconds_median": round(statistics.median(seconds), 1) if seconds else None,
        "seconds_max": max(seconds, default=None),
        "seconds_median_when_the_model_was_asked": round(statistics.median(r["seconds"] for r in asked), 1) if asked else None,
        "input_tokens_mean": round(statistics.mean(r["input_tokens"] for r in asked)) if asked else None,
        "output_tokens_mean": round(statistics.mean(r["output_tokens"] for r in asked)) if asked else None,
        "peak_gpu_mib": max((r["peak_gpu_mib"] for r in counted if r.get("peak_gpu_mib") is not None), default=None),
        "min_free_physical_gb": min((r["min_free_physical_gb"] for r in counted if r.get("min_free_physical_gb") is not None), default=None),
        "min_free_commit_gb": min((r["min_free_commit_gb"] for r in counted if r.get("min_free_commit_gb") is not None), default=None),
    }


def compare(first: dict, second: dict) -> dict:
    """The figures eval/MODEL_TRIAL.md decides on. Refuses unless both runs saw the same prompts and passages."""
    a = {r["id"]: r for r in first["rows"]}
    b = {r["id"]: r for r in second["rows"]}
    if set(a) != set(b):
        raise SystemExit(f"The runs cover different questions: {sorted(set(a) ^ set(b))}")
    different = [i for i in a if a[i]["prompts"] != b[i]["prompts"] or a[i]["offered"] != b[i]["offered"]]
    if different:
        raise SystemExit(f"The models were not shown the same prompts for: {different}")
    report = {
        "questions": len(a),
        "identical_prompts": sum(bool(a[i]["prompts"]) for i in a),
        first["model"]: {"summary": summary(first["rows"]), "placement": first["placement"], "memory_at_start": first["memory_at_start"]},
        second["model"]: {"summary": summary(second["rows"]), "placement": second["placement"], "memory_at_start": second["memory_at_start"]},
        "differ": [{"id": i, "negative": a[i]["negative"], "gold_offered": a[i]["gold_offered"], first["model"]: a[i]["outcome"], second["model"]: b[i]["outcome"],
                    "chosen": {first["model"]: a[i]["chosen"], second["model"]: b[i]["chosen"]}}
                   for i in a if a[i]["outcome"] != b[i]["outcome"] or a[i]["chosen"] != b[i]["chosen"]],
        "negative_stock": {run["model"]: shared.negative_stock_facts(next(r for r in run["rows"] if r["id"] == shared.NEGATIVE_STOCK.id)) for run in (first, second)},
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("run")
    one.add_argument("--model", required=True)
    one.add_argument("--output", type=Path, required=True)
    one.add_argument("--only", help="comma-separated question ids, for a small check of a few questions")
    both = commands.add_parser("compare")
    both.add_argument("first", type=Path)
    both.add_argument("second", type=Path)
    both.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "run":
        return run(arguments.model, arguments.output, set(arguments.only.split(",")) if arguments.only else None)
    report = compare(*(json.loads(path.read_text(encoding="utf-8")) for path in (arguments.first, arguments.second)))
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if arguments.output:
        arguments.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
