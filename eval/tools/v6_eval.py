"""The one comparison of v2 with v1 on the sixth question set. Rules: eval/V2_EVALUATION_PLAN.md.

    python eval/tools/v6_eval.py check                                   the question file against Git, no model
    python eval/tools/v6_eval.py index     --version v1                  that version's own indexer, into its own database
    python eval/tools/v6_eval.py ask       --version v1 --part dry       every prompt it would send, with no model
    python eval/tools/v6_eval.py translate                               one stored wording per Turkish question
    python eval/tools/v6_eval.py ask       --version v1 --part A --run 1
    python eval/tools/v6_eval.py ask       --version v2 --part B
    python eval/tools/v6_eval.py blind                                   sources outside the accepted lists, with no version shown
    python eval/tools/v6_eval.py report                                  the counts and the conditions

Each version runs from its own folder under .data/v6-eval/code, taken from Git with `git archive`,
against its own database. `index` and `ask` start a second process whose `repomind` package is
that folder's, and it refuses to go on if it imported any other.

Three things here are the same for both versions, and come from the v2 folder whatever is being run:

* the credential check. Every passage about to be sent to the model is checked with v2's
  `find_credential`. If one is flagged, the request is not sent, and the question is recorded as
  blocked. v1 has no such rule for code of its own, and nothing is shown to a model for the sake
  of a comparison;
* the memory gate, `check_before_loading`: 12 GB of free physical memory and 12 GB of free commit
  space, read twice, before the model is loaded;
* the stored English wording of each Turkish question, in part A.

Nothing is written to the main index, to the repositories asked about, or to .env.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parents[2]
QUESTIONS = PROJECT / "eval" / "questions_v6.yaml"
RESULTS = PROJECT / "eval" / "results"
WORK = PROJECT / ".data" / "v6-eval"
COMMITS = {"v1": "4e4a71f2", "v2": "v2-candidate"}
DATABASES = {"v1": "repomind_rt_eval_v1", "v2": "repomind_rt_eval_v2"}
MODEL = "qwen2.5:7b"
INDEXED = ("*.java", "*.ts", "*.tsx", "*.js", "*.sql", "*.yml", "*.yaml")
STOP_GB = 4.0
WAIT_FOR_MEMORY_SECONDS = 20 * 60
MODEL_SECONDS_LIMIT = 60 * 60


# ----------------------------------------------------------------------------- shared helpers


def load_questions() -> dict:
    return yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))


def all_questions(data: dict) -> list[dict]:
    return [{**q, "answerable": True} for q in data["questions"]] + [{**q, "answerable": False} for q in data["unanswerable"]]


def code_folder(version: str) -> Path:
    return WORK / "code" / version


def url_file(version: str) -> Path:
    return WORK / f"{DATABASES[version]}.url"


def record_path(version: str, part: str, run: int | None) -> Path:
    if part == "dry":
        return WORK / f"v6-{version}-dry.json"
    return RESULTS / (f"v6-{version}-part-a-run{run}.json" if part == "A" else f"v6-{version}-part-b.json")


def span(path: str, start: int, end: int) -> dict:
    return {"path": path, "start_line": start, "end_line": end}


def overlaps(first: dict, second: dict) -> bool:
    return first["path"] == second["path"] and first["start_line"] <= second["end_line"] and second["start_line"] <= first["end_line"]


def key_of(question_id: str, source: dict) -> str:
    return f"{question_id}|{source['path']}|{source['start_line']}-{source['end_line']}"


def git_show(folder: str, commit: str, path: str) -> list[str]:
    shown = subprocess.run(["git", "--no-optional-locks", "-C", folder, "show", f"{commit}:{path}"], capture_output=True, check=True)
    return shown.stdout.decode("utf-8", "replace").split("\n")


def load_by_path(path: Path, name: str):
    """A module of the v2 folder under another name, so that it can run beside v1's own package."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def project_settings() -> dict[str, str]:
    """What the project's .env sets for RepoMind, for the processes of both versions. Never printed."""
    sys.path.insert(0, str(PROJECT))
    from repomind.config import _read_dotenv

    return {key: value for key, value in _read_dotenv(PROJECT / ".env").items() if key.startswith("REPOMIND_")}


def repository_paths() -> dict[str, str]:
    settings = {**project_settings(), **{k: v for k, v in os.environ.items() if k.startswith("REPOMIND_")}}
    return {"stockguard": settings["REPOMIND_REPO_STOCKGUARD"], "syncnotes": settings["REPOMIND_REPO_SYNCNOTES"]}


def environment(version: str) -> dict[str, str]:
    """The same settings for both versions, except the code folder and the database."""
    settings = project_settings()
    return {
        **os.environ,
        **settings,
        "REPOMIND_DATABASE_URL": url_file(version).read_text(encoding="utf-8").strip(),
        "REPOMIND_MODELS_DIR": settings.get("REPOMIND_MODELS_DIR", str(PROJECT / ".models")),
        "REPOMIND_LLM_MODEL": MODEL,
        "REPOMIND_LLM_SELECTION": "choose",
        "REPOMIND_LLM_LINKED_EVIDENCE": "0",
        "PYTHONPATH": str(code_folder(version)),
        "PYTHONIOENCODING": "utf-8",
        "GIT_OPTIONAL_LOCKS": "0",
        "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
    }


# -------------------------------------------------------------------------------------- check


def check(_arguments) -> int:
    """The question file against the plan and against Git at the pinned commits. No model, no index."""
    data = load_questions()
    paths = repository_paths()
    problems: list[str] = []
    questions = all_questions(data)
    ids = [q["id"] for q in questions]
    problems += [f"{i} is used twice" for i in set(ids) if ids.count(i) > 1]

    def count(**wanted) -> int:
        return sum(all(q.get(k, False) == v for k, v in wanted.items()) for q in questions)

    expected = [
        (count(answerable=True), 32, "answerable"), (count(answerable=False), 16, "unanswerable"),
        (count(language="tr"), 30, "Turkish"), (count(language="en"), 18, "English"),
        (count(answerable=True, language="tr"), 20, "answerable Turkish"), (count(answerable=False, language="tr"), 10, "unanswerable Turkish"),
        (count(answerable=True, kind="one"), 24, "one place"), (count(answerable=True, kind="two"), 8, "two places"),
        (count(targets_completion=True), 4, "constant-value"), (count(answerable=False, kind="absent"), 8, "absent"), (count(answerable=False, kind="adjacent"), 8, "adjacent"),
        (count(repo="stockguard", answerable=True), 16, "StockGuard answerable"), (count(repo="stockguard", answerable=False), 8, "StockGuard unanswerable"),
    ]
    problems += [f"{name}: {found}, and the plan says {wanted}" for found, wanted, name in expected if found != wanted]

    earlier: dict[str, list[dict]] = {"stockguard": [], "syncnotes": []}

    def walk(node, repo=None):
        if isinstance(node, dict):
            repo = node.get("repo", repo)
            if "path" in node and "start_line" in node and repo:
                earlier[repo].append(span(node["path"], node["start_line"], node.get("end_line", node["start_line"])))
            for value in node.values():
                walk(value, repo)
        elif isinstance(node, list):
            for item in node:
                walk(item, repo)

    for other in sorted((PROJECT / "eval").glob("*.yaml")):
        if other.name != QUESTIONS.name:
            walk(yaml.safe_load(other.read_text(encoding="utf-8")))

    reused = 0
    for question in questions:
        folder, commit = paths[question["repo"]], data["repositories"][question["repo"]]["commit"]
        places = question.get("places", [])
        if question["answerable"] and len(places) != (1 if question["kind"] == "one" else 2):
            problems.append(f"{question['id']}: {len(places)} places for kind {question['kind']}")
        for place in places:
            for number, accepted in enumerate(place["accepted"]):
                lines = git_show(folder, commit, accepted["path"])
                if accepted["anchor"] not in "\n".join(lines[accepted["start_line"] - 1 : accepted["end_line"]]):
                    problems.append(f"{question['id']}: the anchor is not in {accepted['path']}:{accepted['start_line']}-{accepted['end_line']}")
                if any(overlaps(accepted, old) for old in earlier[question["repo"]]):
                    reused += 1
                    if number == 0:
                        problems.append(f"{question['id']}: its first place for '{place['name']}' overlaps a span an earlier set accepted")
        for term in question.get("absent_terms", []):
            found = subprocess.run(["git", "--no-optional-locks", "-C", folder, "grep", "-i", "-l", term, commit, "--", *INDEXED], capture_output=True)
            if found.stdout.strip():
                problems.append(f"{question['id']}: '{term}' occurs in {found.stdout.decode().splitlines()[0].split(':', 1)[1]}")
    print(f"{len(questions)} questions, {sum(len(p['accepted']) for q in questions for p in q.get('places', []))} accepted places, "
          f"{reused} of them alternatives that overlap an earlier set's span")
    print("\n".join(f"PROBLEM  {p}" for p in problems) or "The question file matches the plan and the pinned commits.")
    return 1 if problems else 0


# ----------------------------------------------------------------- starting a version's process


def index(arguments) -> int:
    version = arguments.version
    paths = repository_paths()
    data = load_questions()
    for name, folder in paths.items():
        done = subprocess.run(
            [sys.executable, "-m", "repomind", "index", "--path", folder, "--name", name, "--ref", data["repositories"][name]["commit"]],
            cwd=code_folder(version), env=environment(version), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        shown = [line for line in done.stdout.splitlines() if line.strip() and "embedding" not in line.split(":")[0]]
        print(f"[{version}] " + "\n      ".join(shown[:12]))
        if done.returncode:
            print(done.stderr[-800:])
            return done.returncode
    return 0


def start(arguments) -> int:
    version = getattr(arguments, "version", "v2")
    command = [sys.executable, str(Path(__file__).resolve()), "worker", "--version", version, "--part", arguments.part]
    if getattr(arguments, "run", None):
        command += ["--run", str(arguments.run)]
    if getattr(arguments, "only", None):
        command += ["--only", arguments.only]
    return subprocess.run(command, cwd=code_folder(version), env=environment(version)).returncode


# -------------------------------------------------------------------------------- the worker


class Blocked(Exception):
    """A passage about to be sent holds what v2's rules call a credential. Nothing was sent."""


class Resources:
    """Samples GPU memory in use and free memory, once a second, while one question runs."""

    def __init__(self, memory) -> None:
        self.memory, self.gpu, self.physical, self.commit = memory, [], [], []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _sample(self) -> None:
        try:
            reply = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
            self.gpu.append(int(reply.stdout.split()[0]))
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            pass
        reading = self.memory.read_memory()
        if reading.physical_gb is not None:
            self.physical.append(reading.physical_gb)
        if reading.commit_gb is not None:
            self.commit.append(reading.commit_gb)

    def _run(self) -> None:
        while not self._stop.wait(1.0):
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


def model_server(base_url: str) -> str:
    return base_url[: -len("/v1")] if base_url.endswith("/v1") else base_url


def loaded_models(server: str) -> list[dict]:
    with urllib.request.urlopen(f"{server}/api/ps", timeout=10) as reply:
        return json.load(reply).get("models", [])


def unload(server: str) -> None:
    body = json.dumps({"model": MODEL, "keep_alive": 0}).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(f"{server}/api/generate", data=body, headers={"Content-Type": "application/json"}), timeout=60):
        pass


def worker(arguments) -> int:
    version, part = arguments.version, arguments.part
    code = code_folder(version).resolve()
    import repomind

    if Path(repomind.__file__).resolve().parent.parent != code:
        print(f"Not started: repomind was imported from {repomind.__file__}, not from {code}.")
        return 2
    guard_folder = code_folder("v2")
    find_credential = load_by_path(guard_folder / "repomind" / "ingest" / "filters.py", "v6_guard_filters").find_credential
    memory = load_by_path(guard_folder / "repomind" / "evaluation" / "memory.py", "v6_guard_memory")

    from repomind.config import Settings
    from repomind.embeddings.provider import FastEmbedProvider
    from repomind.qa import engine as engine_module
    from repomind.qa.engine import QAEngine
    from repomind.qa.llm import LLMResponse, OpenAICompatibleClient, load_llm_config
    from repomind.qa.prompt import TRANSLATION_PROMPT
    from repomind.search.engine import SearchConfig, SearchEngine
    from repomind.search.translation import LocalTurkishTranslator
    from repomind.store.database import Store

    data = load_questions()
    questions = all_questions(data)
    if part in ("B", "translate"):
        questions = [q for q in questions if q["language"] == "tr"]
    if arguments.only:
        questions = [q for q in questions if q["id"] in arguments.only.split(",")]
    config = load_llm_config()
    if config is None or config.model != MODEL or getattr(config, "selection", "choose") != "choose" or getattr(config, "linked_evidence", False):
        print("Not started: the model, the way of choosing or linked evidence is not what the plan fixes.")
        return 2
    server = model_server(config.base_url)
    uses_model = part != "dry"
    translations_file = RESULTS / "v6-translations.json"
    stored = json.loads(translations_file.read_text(encoding="utf-8"))["translations"] if translations_file.exists() else {}
    if part == "A" and any(q["language"] == "tr" and q["id"] not in stored for q in questions):
        print("Not started: part A needs the stored wording of every Turkish question. Run translate first.")
        return 2

    output = translations_file if part == "translate" else record_path(version, part, arguments.run)
    record = {
        "version": version, "part": part, "run": arguments.run, "model": MODEL if uses_model else None,
        "code": str(code), "code_commit": (code / "COMMIT").read_text(encoding="utf-8").strip(),
        "pipeline_version": repomind.PIPELINE_VERSION, "database": DATABASES[version],
        "settings": {"selection": "choose", "linked_evidence": False, "max_input_tokens": config.max_input_tokens, "max_output_tokens": config.max_output_tokens},
        "memory_at_start": None, "placement": None, "rows": [], "translations": {},
    }
    if output.exists():
        saved = json.loads(output.read_text(encoding="utf-8"))
        if (saved["version"], saved["part"], saved.get("run"), saved["code_commit"]) != (version, part, arguments.run, record["code_commit"]):
            print(f"{output} holds another run, and is not overwritten.")
            return 2
        record = saved
    done = {row["id"] for row in record["rows"]} | set(record["translations"])
    left = [q for q in questions if q["id"] not in done]
    if not left:
        print(f"{output.name}: every question is already answered.")
        return 0

    if uses_model:
        waited = 0
        while True:
            gate = memory.check_before_loading()
            if not gate.refusal:
                break
            if waited >= WAIT_FOR_MEMORY_SECONDS:
                print(f"Not started: {gate.refusal}. Waited {waited // 60} minutes. Nothing was loaded.")
                return 3
            print(f"Waiting for memory: {gate.describe()}", flush=True)
            time.sleep(30)
            waited += 30
        others = [m["name"] for m in loaded_models(server) if m["name"] != MODEL]
        if others:
            print(f"Not started: {', '.join(others)} is in the model server's memory. Two models are never loaded at once.")
            return 3
        record["memory_at_start"] = record["memory_at_start"] or gate.describe()
        print(f"[{version} {part} {arguments.run or ''}] free memory before the model loads: {gate.describe()}", flush=True)

    class Harness:
        def __init__(self, inner) -> None:
            self.inner = inner
            self.begin(None)

        def begin(self, english) -> None:
            self.english, self.offered, self.blocked, self.prompts = english, [], None, []
            self.requests = self.translation_requests = self.input_tokens = self.output_tokens = 0
            self.model_translation = None

        def _real(self, system, user, max_output_tokens):
            response = self.inner.complete(system, user, max_output_tokens)
            self.input_tokens += response.input_tokens or 0
            self.output_tokens += response.output_tokens or 0
            return response

        def complete(self, system, user, max_output_tokens):
            if system == TRANSLATION_PROMPT:
                if part in ("B", "translate"):
                    self.translation_requests += 1
                    response = self._real(system, user, max_output_tokens)
                    self.model_translation = response.text
                    return response
                # The stored wording in part A. With none, as in a dry run before it exists, no model translation at all.
                return LLMResponse(json.dumps({"english": self.english}) if self.english else "", 0, 0)
            flagged = [f"{p.path}:{p.start_line}-{p.end_line}" for p in self.offered if find_credential(p.text, p.chunk.language)]
            self.prompts.append({"system": system, "user": user})
            if flagged:
                self.blocked = flagged
                raise Blocked()
            self.requests += 1
            if part == "dry":
                return LLMResponse('{"chosen": []}', 0, 0)
            return self._real(system, user, max_output_tokens)

    harness = Harness(OpenAICompatibleClient(config) if uses_model else None)
    build_selection_prompt = engine_module.build_selection_prompt

    def remembering_what_is_offered(*positional, **named):
        harness.offered = next((list(v) for v in (*positional, *named.values()) if isinstance(v, (list, tuple)) and v and hasattr(v[0], "chunk")), [])
        return build_selection_prompt(*positional, **named)

    engine_module.build_selection_prompt = remembering_what_is_offered
    settings = Settings.load()
    embedder = FastEmbedProvider(settings.embedding_model, settings.models_dir)
    prompts_folder = WORK / "prompts" / f"{version}-{part}-{arguments.run or 0}"
    prompts_folder.mkdir(parents=True, exist_ok=True)
    git_files: dict[tuple, list[str]] = {}
    spent = sum(row["seconds"] for row in record["rows"] if row.get("reached_model"))
    code_now = 0
    try:
        with Store(settings.database_url) as store:
            store.record_llm_usage = lambda *a, **k: None  # Nothing is written beside the index this version made.
            snapshots = {s.repo: s for s in store.active_snapshots()}
            search = SearchEngine(store, embedder, SearchConfig(include_tests=None), LocalTurkishTranslator(settings.models_dir))
            for question in left:
                free = memory.available_memory_gb()
                if uses_model and free is not None and free < STOP_GB:
                    print(f"Stopped with {free:.1f} GB of memory free. What is answered so far is saved.")
                    code_now = 3
                    break
                if spent > MODEL_SECONDS_LIMIT:
                    print("Stopped: more than 60 minutes of model time.")
                    code_now = 3
                    break
                snapshot = snapshots[question["repo"]]
                pinned = data["repositories"][question["repo"]]["commit"]
                harness.begin(stored.get(question["id"]) if part in ("A", "dry") else None)
                engine_module.new_nonce = lambda identifier=question["id"]: hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:16]
                engine = QAEngine(store, search, config, harness)
                if part == "translate":
                    from repomind.qa.engine import AskResult

                    engine._translate_with_the_model(question["question"], AskResult(status="", question=question["question"]))
                    try:
                        english = json.loads(harness.model_translation or "").get("english")
                    except (ValueError, AttributeError):
                        english = None
                    record["translations"][question["id"]] = english if isinstance(english, str) and english.strip() else None
                    output.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                    print(f"{question['id']:<32} {'translated' if record['translations'][question['id']] else 'NO USABLE TRANSLATION'}", flush=True)
                    continue

                error = None
                result = None
                with Resources(memory) as resources:
                    started = time.perf_counter()
                    try:
                        result = engine.ask(question["question"], [question["repo"]])
                    except Blocked:
                        error = "blocked"
                    seconds = round(time.perf_counter() - started, 2)

                def of(passage) -> dict:
                    return span(passage.path, passage.start_line, passage.end_line)

                def matches_git(passage) -> bool:
                    key = (question["repo"], passage.path)
                    if key not in git_files:
                        git_files[key] = git_show(snapshot.source_path, pinned, passage.path)
                    return passage.chunk.commit_sha == pinned and "\n".join(git_files[key][passage.start_line - 1 : passage.end_line]) == passage.text

                shown = list(result.passages) if result else list(harness.offered)
                row = {
                    "id": question["id"], "repo": question["repo"], "language": question["language"], "answerable": question["answerable"],
                    "status": error or result.status,
                    "reached_model": harness.requests > 0,
                    "blocked": harness.blocked,
                    "answers": [of(item.passage) for item in result.evidence] if result else [],
                    "review": [of(p) for p in result.review_candidates] if result else [],
                    "offered": [of(p) for p in shown],
                    "sent": [of(p) for p in harness.offered] if harness.requests else [],
                    "returned_sources": len(shown),
                    "returned_sources_matching_git": sum(matches_git(p) for p in shown),
                    "sent_holding_a_credential": 0 if harness.requests else None,
                    "translated_query": getattr(result, "translated_query", None),
                    "model_translation": harness.model_translation,
                    "note": getattr(result, "note", "") or "",
                    "warnings": list(getattr(result, "warnings", [])),
                    "completions": [[c.name, c.used_in, bool(c.declaration), c.reason] for c in getattr(result, "completions", [])],
                    "prompt_sha256": [hashlib.sha256(f"{p['system']}\0{p['user']}".encode("utf-8")).hexdigest() for p in harness.prompts],
                    "requests": harness.requests, "translation_requests": harness.translation_requests,
                    "input_tokens": harness.input_tokens, "output_tokens": harness.output_tokens,
                    "seconds": seconds, **resources.summary(),
                }
                (prompts_folder / f"{question['id']}.json").write_text(json.dumps(harness.prompts, ensure_ascii=False), encoding="utf-8")
                if uses_model and record["placement"] is None and harness.requests:
                    placed = next((m for m in loaded_models(server) if m["name"] == MODEL), None)
                    if placed:
                        record["placement"] = {"size_mib": round(placed["size"] / 2**20), "in_gpu_mib": round(placed.get("size_vram", 0) / 2**20)}
                record["rows"].append(row)
                output.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                spent += seconds if row["reached_model"] else 0
                print(f"{question['id']:<32} {row['status']:<18} answers={len(row['answers'])} model={row['reached_model']!s:<5} {seconds:>6}s", flush=True)
    finally:
        if uses_model:
            unload(server)
            print(f"Unloaded {MODEL}. In the model server's memory now: {', '.join(m['name'] for m in loaded_models(server)) or 'nothing'}.", flush=True)
    return code_now


# ------------------------------------------------------------------------------------ grading


def adjudications() -> dict:
    path = RESULTS / "v6-adjudications.json"
    return json.loads(path.read_text(encoding="utf-8"))["decisions"] if path.exists() else {}


def judge(question: dict, row: dict, decided: dict) -> dict:
    """The outcome of one answer, by the table in the plan."""
    places = question.get("places", [])
    covered, wrong, undecided = set(), [], []
    for source in row["answers"]:
        hit = [place["name"] for place in places if any(overlaps(source, accepted) for accepted in place["accepted"])]
        decision = decided.get(key_of(question["id"], source))
        if hit:
            covered.update(hit)
        elif decision and decision["right"]:
            covered.update(decision.get("places") or [place["name"] for place in places][:1])
        else:
            wrong.append(source)
            if decision is None and question["answerable"]:  # Any source for an unanswerable question is wrong. Nothing is left to decide.
                undecided.append(source)
    offered = {place["name"] for place in places if any(overlaps(source, accepted) for source in row["offered"] for accepted in place["accepted"])}
    sent = {place["name"] for place in places if any(overlaps(source, accepted) for source in row.get("sent", []) for accepted in place["accepted"])}
    if row["status"] == "blocked":
        outcome = "blocked"
    elif row["status"] == "invalid_response":
        outcome = "invalid"
    elif not question["answerable"]:
        outcome = "wrongly accepted" if row["answers"] else "refused"
    elif not row["answers"]:
        outcome = "no answer"
    elif wrong:
        outcome = "mixed" if covered else "wrong"
    else:
        outcome = "correct" if len(covered) == len(places) else "partial"
    why = None
    if outcome in ("no answer", "refused"):
        why = "before the model" if not row["reached_model"] else ("turned down by the evidence rule" if row["review"] else "the model chose nothing")
    return {
        "outcome": outcome, "why_no_answer": why, "undecided": undecided,
        "right_lines_offered": bool(places) and len(offered) == len(places), "some_right_lines_offered": bool(offered),
        "right_lines_sent_to_the_model": bool(places) and len(sent) == len(places),
    }


def runs() -> dict[str, dict]:
    found = {}
    for version in ("v1", "v2"):
        for name, part, run in ((f"{version} A1", "A", 1), (f"{version} A2", "A", 2), (f"{version} B", "B", None)):
            path = record_path(version, part, run)
            if path.exists():
                found[name] = json.loads(path.read_text(encoding="utf-8"))
    return found


def blind(_arguments) -> int:
    """Every answer source outside the accepted lists, once, in a shuffled order, with no version or run."""
    data = load_questions()
    by_id = {q["id"]: q for q in all_questions(data)}
    paths = repository_paths()
    decided = adjudications()
    wanted: dict[str, tuple[dict, dict]] = {}
    for record in runs().values():
        for row in record["rows"]:
            question = by_id[row["id"]]
            if question["answerable"]:
                for source in judge(question, row, {})["undecided"]:
                    wanted[key_of(question["id"], source)] = (question, source)
    items = sorted(wanted.items())
    random.Random(6).shuffle(items)
    pack = []
    for key, (question, source) in items:
        commit = data["repositories"][question["repo"]]["commit"]
        lines = git_show(paths[question["repo"]], commit, source["path"])[source["start_line"] - 1 : source["end_line"]]
        pack.append({"key": key, "question": question["question"], "expected": question["expected"], "places": [p["name"] for p in question["places"]],
                     "source": f"{source['path']}:{source['start_line']}-{source['end_line']}", "lines": lines, "already_decided": key in decided})
    out = WORK / "v6-blind-pack.json"
    out.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(pack)} sources outside the accepted lists, {sum(not p['already_decided'] for p in pack)} not yet decided: {out}")
    return 0


def summarise(record: dict, by_id: dict, decided: dict) -> dict:
    rows = [{**row, **judge(by_id[row["id"]], row, decided), "question": by_id[row["id"]]} for row in record["rows"]]

    def counts(group: list[dict]) -> dict:
        answerable = [r for r in group if r["answerable"]]
        negatives = [r for r in group if not r["answerable"]]
        tally = lambda items, *names: sum(r["outcome"] in names for r in items)  # noqa: E731
        offered = [r for r in answerable if r["right_lines_offered"]]
        return {
            "answerable": len(answerable), "correct": tally(answerable, "correct"), "partial": tally(answerable, "partial"),
            "mixed": tally(answerable, "mixed"), "wrong": tally(answerable, "wrong"), "wrong_or_mixed": tally(answerable, "wrong", "mixed"),
            "no_answer": tally(answerable, "no answer"), "invalid": tally(group, "invalid"), "blocked": tally(group, "blocked"),
            "no_answer_before_the_model": sum(r["why_no_answer"] == "before the model" for r in answerable),
            "no_answer_model_chose_nothing": sum(r["why_no_answer"] == "the model chose nothing" for r in answerable),
            "no_answer_evidence_rule": sum(r["why_no_answer"] == "turned down by the evidence rule" for r in answerable),
            "right_lines_offered": len(offered), "some_right_lines_offered": sum(r["some_right_lines_offered"] for r in answerable),
            "right_lines_sent_to_the_model": sum(r["right_lines_sent_to_the_model"] for r in answerable),
            "no_answer_with_right_lines_offered": tally(offered, "no answer", "invalid"),
            "unanswerable": len(negatives), "refused": tally(negatives, "refused"), "wrongly_accepted": tally(negatives, "wrongly accepted"),
            "unanswerable_reaching_the_model": sum(r["reached_model"] for r in negatives),
        }

    asked = [r for r in rows if r["reached_model"]]
    return {
        "all": counts(rows),
        "without_the_four_constant_questions": counts([r for r in rows if not r["question"].get("targets_completion")]),
        "only_the_four_constant_questions": counts([r for r in rows if r["question"].get("targets_completion")]),
        "turkish": counts([r for r in rows if r["language"] == "tr"]), "english": counts([r for r in rows if r["language"] == "en"]),
        "stockguard": counts([r for r in rows if r["repo"] == "stockguard"]), "syncnotes": counts([r for r in rows if r["repo"] == "syncnotes"]),
        "one_place": counts([r for r in rows if r["question"].get("kind") == "one"]), "two_places": counts([r for r in rows if r["question"].get("kind") == "two"]),
        "unanswerable_absent": counts([r for r in rows if r["question"].get("kind") == "absent"]),
        "unanswerable_adjacent": counts([r for r in rows if r["question"].get("kind") == "adjacent"]),
        "sources_returned": sum(r["returned_sources"] for r in rows), "sources_matching_git": sum(r["returned_sources_matching_git"] for r in rows),
        "questions_reaching_the_model": len(asked),
        "seconds_median_when_the_model_was_asked": round(statistics.median(r["seconds"] for r in asked), 2) if asked else None,
        "seconds_max": max((r["seconds"] for r in rows), default=None), "model_seconds_total": round(sum(r["seconds"] for r in asked), 1),
        "input_tokens_mean": round(statistics.mean(r["input_tokens"] for r in asked)) if asked else None,
        "output_tokens_mean": round(statistics.mean(r["output_tokens"] for r in asked)) if asked else None,
        "input_tokens_total": sum(r["input_tokens"] for r in rows), "output_tokens_total": sum(r["output_tokens"] for r in rows),
        "peak_gpu_mib": max((r["peak_gpu_mib"] for r in rows if r.get("peak_gpu_mib") is not None), default=None),
        "min_free_physical_gb": min((r["min_free_physical_gb"] for r in rows if r.get("min_free_physical_gb") is not None), default=None),
        "min_free_commit_gb": min((r["min_free_commit_gb"] for r in rows if r.get("min_free_commit_gb") is not None), default=None),
        "outcomes": {r["id"]: r["outcome"] for r in rows},
        "undecided_sources": sum(len(r["undecided"]) for r in rows),
    }


def report(arguments) -> int:
    data = load_questions()
    by_id = {q["id"]: q for q in all_questions(data)}
    decided = adjudications()
    records = runs()
    summaries = {name: summarise(record, by_id, decided) for name, record in records.items()}
    out = {"questions": len(by_id), "adjudicated_sources": len(decided), "runs": summaries, "memory_at_start": {n: r["memory_at_start"] for n, r in records.items()},
           "placement": {n: r["placement"] for n, r in records.items()}, "code": {n: [r["code_commit"], r["pipeline_version"]] for n, r in records.items()}}

    part_a = {v: [summaries[f"{v} A{n}"] for n in (1, 2) if f"{v} A{n}" in summaries] for v in ("v1", "v2")}
    if all(len(part_a[v]) == 2 for v in part_a):
        total = lambda version, group, key: sum(run[group][key] for run in part_a[version])  # noqa: E731
        each = lambda version, group, key: [run[group][key] for run in part_a[version]]  # noqa: E731
        medians = {v: statistics.median(m for m in (run["seconds_median_when_the_model_was_asked"] for run in part_a[v]) if m is not None) for v in part_a}
        b = {v: summaries.get(f"{v} B") for v in ("v1", "v2")}
        v2_runs = [records[n] for n in records if n.startswith("v2")]
        safety = {
            "1 wrong acceptance: v2 not above v1": [total("v2", "all", "wrongly_accepted"), total("v1", "all", "wrongly_accepted"), total("v2", "all", "wrongly_accepted") <= total("v1", "all", "wrongly_accepted")],
            "2 wrong or mixed sources: v2 not above v1": [total("v2", "all", "wrong_or_mixed"), total("v1", "all", "wrong_or_mixed"), total("v2", "all", "wrong_or_mixed") <= total("v1", "all", "wrong_or_mixed")],
            "3 every source v2 returned matches Git": [sum(s["sources_matching_git"] for n, s in summaries.items() if n.startswith("v2")), sum(s["sources_returned"] for n, s in summaries.items() if n.startswith("v2")),
                                                       all(s["sources_matching_git"] == s["sources_returned"] for n, s in summaries.items() if n.startswith("v2"))],
            "4 no v2 prompt held a credential": [sum(r["sent_holding_a_credential"] or 0 for rec in v2_runs for r in rec["rows"]), sum(bool(r["blocked"]) for rec in v2_runs for r in rec["rows"]),
                                                 all(not r["blocked"] and not r["sent_holding_a_credential"] for rec in v2_runs for r in rec["rows"])],
            "5 invalid replies: v2 not above v1 + 1": [total("v2", "all", "invalid"), total("v1", "all", "invalid"), total("v2", "all", "invalid") <= total("v1", "all", "invalid") + 1],
            "6 unanswered with the right lines offered: v2 not above v1 + 2": [total("v2", "all", "no_answer_with_right_lines_offered"), total("v1", "all", "no_answer_with_right_lines_offered"),
                                                                             total("v2", "all", "no_answer_with_right_lines_offered") <= total("v1", "all", "no_answer_with_right_lines_offered") + 2],
            "7 part B: v2 wrongly accepts no more, and wrong or mixed not above v1 + 1": (
                [[b["v2"]["all"]["wrongly_accepted"], b["v2"]["all"]["wrong_or_mixed"]], [b["v1"]["all"]["wrongly_accepted"], b["v1"]["all"]["wrong_or_mixed"]],
                 b["v2"]["all"]["wrongly_accepted"] <= b["v1"]["all"]["wrongly_accepted"] and b["v2"]["all"]["wrong_or_mixed"] <= b["v1"]["all"]["wrong_or_mixed"] + 1]
                if b["v1"] and b["v2"] else [None, None, None]),
            "8 GPU memory under 4096 MiB and median time not above 1.25 x v1": [[max(s["peak_gpu_mib"] or 0 for s in summaries.values()), medians["v2"]], [4096, round(1.25 * medians["v1"], 2)],
                                                                             max(s["peak_gpu_mib"] or 0 for s in summaries.values()) < 4096 and medians["v2"] <= 1.25 * medians["v1"]],
        }
        accuracy = {
            "1 correct: v2 at least v1 + 6, of 64": [total("v2", "all", "correct"), total("v1", "all", "correct"), total("v2", "all", "correct") >= total("v1", "all", "correct") + 6],
            "2 v2's lower run not below v1's higher run": [each("v2", "all", "correct"), each("v1", "all", "correct"), min(each("v2", "all", "correct")) >= max(each("v1", "all", "correct"))],
            "3 without the four constant questions: v2 not below v1, of 56": [total("v2", "without_the_four_constant_questions", "correct"), total("v1", "without_the_four_constant_questions", "correct"),
                                                                             total("v2", "without_the_four_constant_questions", "correct") >= total("v1", "without_the_four_constant_questions", "correct")],
            "4 Turkish: v2 not below v1, of 40": [total("v2", "turkish", "correct"), total("v1", "turkish", "correct"), total("v2", "turkish", "correct") >= total("v1", "turkish", "correct")],
        }
        safe = all(v[2] for v in safety.values())
        better = all(v[2] for v in accuracy.values())
        not_worse = total("v2", "all", "correct") >= total("v1", "all", "correct") - 2
        out["conditions"] = {
            "safety (v2, v1, holds)": safety, "accuracy (v2, v1, holds)": accuracy,
            "decision": ("not released: a safety condition fails" if not safe else "can be released, and may be called more accurate on this set" if better
                         else "can be released for what it fixes, with no claim of better answers" if not_worse else "not released: more than 2 correct answers below v1"),
        }
        out["runs_that_differ"] = {
            version: [{"id": i, "run 1": part_a[version][0]["outcomes"][i], "run 2": part_a[version][1]["outcomes"][i]}
                      for i in part_a[version][0]["outcomes"] if part_a[version][0]["outcomes"][i] != part_a[version][1]["outcomes"].get(i)]
            for version in part_a
        }
    for name in ("v1", "v2"):
        if f"{name} B" in summaries:
            record = records[f"{name} B"]
            stored = json.loads((RESULTS / "v6-translations.json").read_text(encoding="utf-8"))["translations"]
            a_runs = [summaries[f"{name} A{n}"]["outcomes"] for n in (1, 2) if f"{name} A{n}" in summaries]
            out.setdefault("part_b", {})[name] = {
                "turkish_questions": len(record["rows"]),
                "translation_differs_from_the_stored_one": sum((r["translated_query"] or "").strip() != (stored.get(r["id"]) or "").strip() for r in record["rows"]),
                "outcome_differs_from_both_part_a_runs": [r["id"] for r in record["rows"] if a_runs and all(summaries[f"{name} B"]["outcomes"][r["id"]] != a[r["id"]] for a in a_runs)],
            }
    text = json.dumps(out, ensure_ascii=False, indent=1)
    target = arguments.output or (RESULTS / "v6-report.json")
    target.write_text(text + "\n", encoding="utf-8")
    brief = {name: {k: s["all"][k] for k in ("correct", "partial", "wrong_or_mixed", "no_answer", "invalid", "blocked", "wrongly_accepted")} for name, s in summaries.items()}
    print(json.dumps({"runs": brief, "conditions": out.get("conditions"), "undecided_sources": {n: s["undecided_sources"] for n, s in summaries.items()}}, ensure_ascii=False, indent=1))
    print(f"written to {target}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("index").add_argument("--version", choices=["v1", "v2"], required=True)
    asking = commands.add_parser("ask")
    asking.add_argument("--version", choices=["v1", "v2"], required=True)
    asking.add_argument("--part", choices=["dry", "A", "B"], required=True)
    asking.add_argument("--run", type=int, choices=[1, 2])
    asking.add_argument("--only")
    commands.add_parser("translate")
    inner = commands.add_parser("worker")
    inner.add_argument("--version", choices=["v1", "v2"], required=True)
    inner.add_argument("--part", choices=["dry", "A", "B", "translate"], required=True)
    inner.add_argument("--run", type=int)
    inner.add_argument("--only")
    commands.add_parser("blind")
    commands.add_parser("report").add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "ask" and (arguments.part == "A") != bool(arguments.run):
        parser.error("part A needs --run 1 or 2, and the other parts take none")
    if arguments.command == "translate":
        arguments.part, arguments.version, arguments.run, arguments.only = "translate", "v2", None, None
    handlers = {"check": check, "index": index, "ask": start, "translate": start, "worker": worker, "blind": blind, "report": report}
    return handlers[arguments.command](arguments)


if __name__ == "__main__":
    raise SystemExit(main())
