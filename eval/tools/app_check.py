"""A small check of the real `ask` flow through the API with one chosen model, and of its Turkish translations.

    python eval/tools/app_check.py ask --model qwen2.5-coder:7b --output check.json
    python eval/tools/app_check.py retrieval check.json

`ask` starts the RepoMind server for this run only, with REPOMIND_LLM_MODEL set in its own
environment, so .env and the default model stay as they are. It asks the questions fixed in
eval/app_check.yaml through POST /api/ask, as the web application does, so the model translates
the Turkish ones itself. It checks every returned line against Git at its commit and builds the
GitHub link the way the web application does. Then, with the same model still loaded, it
translates the 32 development questions of the earlier trials with the engine's own prompt and
parsing, for comparison with the stored qwen2.5:7b translations, and unloads the model.

`retrieval` needs no model: it searches those 32 questions once with each translation and reports
where the right code gets to, so a change in translation can be seen to matter or not.

`ask` starts only when two readings a second apart both show 12 GB of free physical memory and
12 GB of free commit space, and when no other model is in the model server's memory.
Development data: the 32 questions have been read. The six in the YAML file are new.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import selection_trial as shared  # noqa: E402  (its questions, translations and resource sampling)

from repomind.config import Settings, get_setting  # noqa: E402
from repomind.evaluation.dataset import GoldSpan  # noqa: E402
from repomind.evaluation.memory import check_before_loading  # noqa: E402
from repomind.evaluation.metrics import overlaps, touches  # noqa: E402
from repomind.qa.engine import AskResult, QAEngine  # noqa: E402
from repomind.qa.llm import OpenAICompatibleClient, load_llm_config  # noqa: E402
from repomind.store.database import Store  # noqa: E402

QUESTIONS = PROJECT / "eval" / "app_check.yaml"
PORT = 7329
BASE = f"http://127.0.0.1:{PORT}"
PATH_ENV = {"stockguard": "REPOMIND_REPO_STOCKGUARD", "syncnotes": "REPOMIND_REPO_SYNCNOTES"}


def git_lines(repo: str, commit: str, path: str) -> list[str]:
    source = get_setting(PATH_ENV[repo])
    shown = subprocess.run(["git", "--no-optional-locks", "-C", source, "show", f"{commit}:{path}"], capture_output=True, check=True)
    return shown.stdout.decode("utf-8").split("\n")


def load_questions() -> dict:
    return yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))


def problems_with_the_questions(data: dict) -> list[str]:
    """Every accepted span holds its anchor at the pinned commit, and no absent term appears in the repository."""
    problems = []
    for question in (*data["questions"], *data["apart"]):
        commit = data["repositories"][question["repo"]]["commit"]
        for span in question.get("accepted", []):
            text = "\n".join(git_lines(question["repo"], commit, span["path"])[span["start_line"] - 1 : span["end_line"]])
            if span["anchor"] not in text:
                problems.append(f"{question['id']}: '{span['anchor']}' is not in {span['path']}:{span['start_line']}-{span['end_line']}")
        for term in question.get("absent", []):
            found = subprocess.run(["git", "--no-optional-locks", "-C", get_setting(PATH_ENV[question["repo"]]), "grep", "-i", "-l", term, commit, "--"],
                                   capture_output=True).stdout.decode("utf-8").strip()
            if found:
                problems.append(f"{question['id']}: '{term}' appears in {found.splitlines()[0]}")
    return problems


def request(method: str, url: str, body: dict | None = None, timeout: float = 30):
    data = None if body is None else json.dumps(body).encode("utf-8")
    sent = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(sent, timeout=timeout) as reply:
        return json.load(reply)


def model_server(config) -> str:
    return config.base_url[: -len("/v1")] if config.base_url.endswith("/v1") else config.base_url


def loaded_models(server: str) -> list[dict]:
    return request("GET", f"{server}/api/ps").get("models", [])


def unload(server: str, model: str) -> None:
    request("POST", f"{server}/api/generate", {"model": model, "keep_alive": 0}, timeout=60)


def start_server(model: str, log: Path, linked_evidence: bool = False) -> subprocess.Popen:
    # Both settings are given, so a record never depends on what the machine's .env happens to say.
    environment = {**os.environ, "REPOMIND_LLM_MODEL": model, "REPOMIND_LLM_SELECTION": "choose",
                   "REPOMIND_LLM_LINKED_EVIDENCE": "1" if linked_evidence else "0", "HF_HUB_DISABLE_SYMLINKS_WARNING": "1"}
    process = subprocess.Popen([sys.executable, "-m", "repomind", "serve", "--port", str(PORT)], cwd=PROJECT, env=environment,
                               stdout=log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 240
    health = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"The server stopped at start. See {log}.")
        try:
            health = request("GET", f"{BASE}/api/health", timeout=5)
            # Only with this model ready, so that no answer quietly comes from search alone.
            if health["database"] and health["ai"]["state"] == "ready" and health["ai"].get("model") == model:
                return process
        except (urllib.error.URLError, OSError, KeyError):
            pass
        time.sleep(2)
    process.terminate()
    raise SystemExit(f"The server was not ready with {model} within four minutes. Last health: {health}")


def outcome(response: dict, accepted: list[GoldSpan]) -> str:
    if response["verdict"] == "invalid_response":
        return "invalid"
    answers = [s for s in response["sources"] if s["role"] == "answer"]
    if not answers:
        return "none"
    if not accepted:
        return "wrong"
    cited = [GoldSpan(s["path"], s["start_line"], s["end_line"], "") for s in answers]
    return "correct" if any(touches(c, s) for c in cited for s in accepted) else "wrong"


def check_links(response: dict, repo: str, pinned: str, origin: str | None) -> list[dict]:
    """Each returned line against Git at its commit, and the GitHub link the web application would build."""
    checked = []
    for source in response["sources"]:
        lines = git_lines(repo, source["commit"], source["path"])
        same = "\n".join(lines[source["start_line"] - 1 : source["end_line"]]) == source["text"]
        path = "/".join(urllib.parse.quote(part) for part in source["path"].split("/"))
        anchor = f"#L{source['start_line']}" if source["start_line"] == source["end_line"] else f"#L{source['start_line']}-L{source['end_line']}"
        checked.append({
            "role": source["role"],
            "where": f"{source['path']}:{source['start_line']}-{source['end_line']}",
            "commit_is_the_indexed_one": source["commit"] == pinned,
            "exists_in_git": source["exists_in_git"],
            "text_matches_git": same,
            "github_url": f"{origin}/blob/{source['commit']}/{path}{anchor}" if origin else None,
        })
    return checked


def github_answers(urls: set[str]) -> dict[str, int | str]:
    """Whether each file page exists on GitHub at that commit. The line anchor is not sent."""
    status = {}
    for url in sorted(urls):
        page = url.split("#", 1)[0]
        try:
            sent = urllib.request.Request(page, method="HEAD", headers={"User-Agent": "repomind-link-check"})
            with urllib.request.urlopen(sent, timeout=20) as reply:
                status[page] = reply.status
        except urllib.error.HTTPError as error:
            status[page] = error.code
        except (urllib.error.URLError, OSError) as error:
            status[page] = f"unreachable: {type(error).__name__}"
    return status


def ask(model: str, output: Path, translations: bool = True, linked_evidence: bool = False) -> int:
    if output.exists():
        print(f"{output} already exists, and is not overwritten.")
        return 2
    data = load_questions()
    problems = problems_with_the_questions(data)
    if problems:
        print("The questions do not match the code:\n  " + "\n  ".join(problems))
        return 2
    check = check_before_loading()
    print(f"Free memory before the model loads: {check.describe()}", flush=True)
    if check.refusal:
        print(f"Not started: {check.refusal}. Nothing was loaded.")
        return 3
    config = replace(load_llm_config(), model=model, selection="choose")
    server = model_server(config)
    others = [m["name"] for m in loaded_models(server) if m["name"] != model]
    if others:
        print(f"Not started: {', '.join(others)} is still in memory. Two models are never loaded at once.")
        return 3

    record = {"model": model, "linked_evidence": linked_evidence, "memory_at_start": check.describe(), "placement": None, "answers": [], "translations": {}}
    log = output.with_name(output.name + ".server.log")
    process = start_server(model, log, linked_evidence)
    try:
        repositories = {r["name"]: r for r in request("GET", f"{BASE}/api/repositories")}
        for question in (*data["questions"], *data["apart"]):
            pinned = data["repositories"][question["repo"]]["commit"]
            accepted = [GoldSpan(s["path"], s["start_line"], s["end_line"], s["anchor"]) for s in question.get("accepted", [])]
            with shared.Resources() as resources:
                started = time.perf_counter()
                response = request("POST", f"{BASE}/api/ask", {"repo": question["repo"], "question": question["question"]}, timeout=900)
                seconds = round(time.perf_counter() - started, 2)
            if response["verdict"] == "ai_unavailable" or (response["model"] and response["model"] != model):
                raise SystemExit(f"{question['id']} was not answered by {model}: {response['verdict']}, {response['model']}, {response['ai']}")
            if record["placement"] is None:
                placed = next((m for m in loaded_models(server) if m["name"] == model), None)
                if placed:
                    record["placement"] = {"size_mib": round(placed["size"] / 2**20), "in_gpu_mib": round(placed.get("size_vram", 0) / 2**20)}
            offered = [GoldSpan(s["path"], s["start_line"], s["end_line"], "") for s in response["sources"]]
            row = {
                "id": question["id"],
                "language": question["language"],
                "apart": question in data["apart"],
                "answerable": bool(accepted),
                "verdict": response["verdict"],
                "ai_used": response["ai_used"],
                "model": response["model"],
                "translated_query": response["translated_query"],
                "outcome": outcome(response, accepted),
                "right_lines_offered": any(touches(o, s) for o in offered for s in accepted),
                "sources": check_links(response, question["repo"], pinned, repositories[question["repo"]].get("origin_url")),
                "index_commit_is_pinned": repositories[question["repo"]]["commit"] == pinned,
                "model_reading": response["model_reading"],
                "warnings": response["warnings"],
                "input_tokens": response["input_tokens"],
                "output_tokens": response["output_tokens"],
                "seconds": seconds,
                **resources.summary(),
            }
            record["answers"].append(row)
            output.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            print(f"{row['id']:<24} {row['language']} {row['outcome']:<8} {seconds:>6}s  translated: {row['translated_query']}", flush=True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()

    # The same model, still loaded, translating the earlier trial's Turkish questions with the engine's own code.
    settings = Settings.load()
    with Store(settings.database_url, apply_schema=False) as store:
        store.record_llm_usage = lambda *args, **kwargs: None  # This part writes nothing.
        engine = QAEngine(store, None, config, OpenAICompatibleClient(config))
        stored = shared.stored_translations()
        try:
            for question, _negative in shared.questions() if translations else ():
                started = time.perf_counter()
                translated = engine._translate_with_the_model(question.tr, AskResult(status="trial", question=question.tr))
                record["translations"][question.id] = {"asked": question.tr, "model": translated, "qwen2.5:7b_stored": stored.get(question.id),
                                                       "seconds": round(time.perf_counter() - started, 2)}
        finally:
            unload(server, model)
    record["github"] = github_answers({s["github_url"] for row in record["answers"] for s in row["sources"] if s["github_url"]})
    output.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    time.sleep(3)
    print(f"Unloaded {model}. In memory now: {', '.join(m['name'] for m in loaded_models(server)) or 'nothing'}.")
    return 0


def retrieval(path: Path) -> int:
    """Where the right code gets to, once with the chosen model's translations and once with the stored qwen2.5:7b ones. No model."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from repomind.embeddings.provider import FastEmbedProvider
    from repomind.qa.passages import is_evidence, select_passages
    from repomind.qa.sufficiency import check_sufficiency
    from repomind.search.engine import SearchConfig, SearchEngine
    from repomind.search.translation import LocalTurkishTranslator

    record = json.loads(path.read_text(encoding="utf-8"))
    settings = Settings.load()
    embedder = FastEmbedProvider(settings.embedding_model, settings.models_dir)
    report: dict = {"model": record["model"], "rows": []}
    with Store(settings.database_url, apply_schema=False) as store:
        search = SearchEngine(store, embedder, SearchConfig(), LocalTurkishTranslator(settings.models_dir))
        for question, negative in shared.questions():
            entry = record["translations"][question.id]
            second = search.local_translation(question.tr, [])
            row = {"id": question.id, "negative": negative}
            for name, english in (("stored", entry["qwen2.5:7b_stored"]), ("model", entry["model"])):
                found = search.search(question.tr, "hybrid", [question.repo], 12, translated=english or second, more_wordings=[second] if english and second else (), glossary_seats=5)
                gate = check_sufficiency(question.tr, found.translated_query, search.vocabulary(question.tr, found.translated_query, [question.repo])).insufficient
                queries = [t for t in (question.tr, found.translated_query, second) if t]
                passages = [] if gate else select_passages([h.chunk for h in found.hits], queries, embedder, more_terms=found.glossary_terms)
                good = [p for p in passages if is_evidence(p, found.translated_query or question.tr)]
                accepted = () if negative else question.accepted
                row[name] = {
                    "candidates": any(overlaps(h.chunk, s) for h in found.hits for s in accepted),
                    "passages": any(touches(p, s) for p in passages for s in accepted),
                    "evidence": any(touches(p, s) for p in good for s in accepted),
                    "reaches_model": not gate and bool(good),
                }
            report["rows"].append(row)
    for name in ("stored", "model"):
        answerable = [r[name] for r in report["rows"] if not r["negative"]]
        negatives = [r[name] for r in report["rows"] if r["negative"]]
        report[name] = {key: sum(r[key] for r in answerable) for key in ("candidates", "passages", "evidence")}
        report[name]["answerable_reaching_model"] = sum(r["reaches_model"] for r in answerable)
        report[name]["unanswerable_reaching_model"] = sum(r["reaches_model"] for r in negatives)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=1))
    changed = [r["id"] for r in report["rows"] if r["stored"] != r["model"]]
    print(f"questions where the translation changed where the right code got to: {changed or 'none'}")
    path.with_name(path.stem + "-retrieval.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("ask")
    one.add_argument("--model", required=True)
    one.add_argument("--output", type=Path, required=True)
    one.add_argument("--no-translations", action="store_true", help="ask the questions only, without translating the development questions")
    one.add_argument("--linked-evidence", action="store_true", help="experimental: start the application with linked evidence on. It is off without this.")
    two = commands.add_parser("retrieval")
    two.add_argument("record", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "ask":
        return ask(arguments.model, arguments.output, translations=not arguments.no_translations, linked_evidence=arguments.linked_evidence)
    return retrieval(arguments.record)


if __name__ == "__main__":
    raise SystemExit(main())
