"""Does giving the model the glossary's terms improve its translation? See eval/TRIALS.md.

    python eval/tools/translation_trial.py check
    python eval/tools/translation_trial.py limited --output results.json
    python eval/tools/translation_trial.py all --output results.json
    python eval/tools/translation_trial.py report results.json [--grades grades.json --key key.json]
    python eval/tools/translation_trial.py wordings results.json --arm hints --output wordings.json
    python eval/tools/translation_trial.py blind results.json --pack pack.json --key key.json
    python eval/tools/translation_trial.py compare funnel_a.json funnel_b.json

`wordings` writes what `retrieval_funnel.py --translations` reads, to measure search with no model.
`compare` shows what changed between two of its result files, question by question.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from evidence_trial import MODEL, all_questions, stored_translations  # noqa: E402

OUTPUT_TOKENS = 80
HINT = (
    "\nThese words occur in this repository's code, and some Turkish words in the question may mean one of them: {terms}."
    " Prefer one of these words when it fits the question's meaning. Do not add a word the question does not ask about."
)

# What the right meaning needs, and the wrong words seen so far. Fixed before the trial.
KNOWN_ERRORS = {
    "sn-offline": (("offline",), ()),
    "sn-version-restore": (("note",), ()),
    "sn-presence": (("note",), ()),
    "sn-session-refresh": (("expir",), ()),
    "sg-invariants": (("invariant",), ()),
    "sn-redis-outage": (("collaborat",), ()),
    "v2-sg-reservation-defaults": (("default", "expir"), ()),
    "v2-sg-add-stock-limits": (("quantit|amount",), ("logging in", "login", "depoya")),
    "v2-sn-one-owner": (("note",), ()),
    "v2-sn-collab-instances": (("collaborat",), ()),
    "v2-sn-note-search": (("note",), ()),
    "v2-sn-health-redis": ((r"down|\boff\b|unavailable|closed|stopped|not running",), ()),
    "v2-sn-title-merge": (("title",), ("reason",)),
    "v2-sg-auth-error-format": ((r"not logged|hasn't logged|has not logged|unauthenticated|not authenticated|without logging|anonymous|not signed",), ()),
    "v3-sg-instance-header": (("instance|copy|replica",), ("version",)),
    "v4-sg-consistency-endpoint": (("stock|inventory",), ("package",)),
    "v4-sn-refresh-token-storage": (("refresh",), ()),
    "v4-sn-guard-order": (("order|sequence",), ("queue",)),
    "v4-sn-revoked-close-code": (("code",), ("snippet",)),
    "v4-sn-ticket-not-access-token": (("collaborat|shared edit|co-edit",), ()),
}
REAL_NAMES = {
    "sg-idempotency": "Idempotency-Key",
    "sg-jwt": "JWT",
    "sn-csrf": "CSRF",
    "sn-refresh-reuse": "Refresh token",
    "sg-db-connection-error": "PostgreSQL",
}
NAME_PATTERNS = {
    "config key": re.compile(r"^\s*([a-z][a-z0-9]*(?:-[a-z0-9]+)+):", re.M),
    "environment variable": re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b"),
    "HTTP constant": re.compile(r"\b(HttpStatus\.[A-Za-z_]+)\b"),
    "header": re.compile(r"[\"'`]([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)[\"'`]"),
    "SQL name": re.compile(r"\"?([a-z][a-z0-9_]*_(?:check|key|fkey|pkey|idx))\"?"),
}
NAMES_PER_KIND = 2
NAME_FRAME = "{} değeri nerede ayarlanıyor ve varsayılan değeri ne?"


def indexed_names(store) -> list[tuple[str, str, str]]:
    """Up to 20 names from the index, by a fixed random choice, so that none is picked by hand."""
    shuffler = random.Random(20261107)
    names = []
    for snapshot in store.active_snapshots():
        chunks = store.load_chunks([snapshot.id], False)
        for kind, pattern in NAME_PATTERNS.items():
            found = sorted({name for chunk in chunks for name in pattern.findall(chunk.content)})
            shuffler.shuffle(found)
            names.extend((snapshot.repo, kind, name) for name in found[:NAMES_PER_KIND])
    return names


def build_prompt(question: str, terms: list[str]):
    from repomind.qa.prompt import PreparedPrompt, estimate_tokens, TRANSLATION_PROMPT

    system = TRANSLATION_PROMPT + (HINT.format(terms=", ".join(terms)) if terms else "")
    return PreparedPrompt(system=system, user=question, estimated_input_tokens=estimate_tokens(system) + estimate_tokens(question) + 12, context_chars=0)


def translate(qa, question: str, terms: list[str]) -> dict:
    from repomind.qa.engine import AskResult
    from repomind.search.translation import keep_technical_terms

    result = AskResult(status="trial", question=question)
    started = time.perf_counter()
    text = qa._call(build_prompt(question, terms), OUTPUT_TOKENS, result)
    seconds = time.perf_counter() - started
    try:
        raw = json.loads(text)["english"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raw = None
    usable = isinstance(raw, str) and raw.strip()
    return {
        "reply": text,
        "raw": raw.strip() if usable else None,
        "final": keep_technical_terms(question, raw.strip()) if usable else None,
        "seconds": round(seconds, 2),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }


def plan(store, questions: dict, step: str) -> list[dict]:
    if step == "all":
        return [
            {"kind": "question" if hasattr(q, "accepted") else "negative", "id": q.id, "repo": q.repo, "text": q.tr, "arms": ("original", "hints")}
            for q in questions.values()
        ]
    items = [{"kind": "error", "id": i, "repo": questions[i].repo, "text": questions[i].tr, "arms": ("original", "hints")} for i in KNOWN_ERRORS]
    items += [{"kind": "real name", "id": i, "repo": questions[i].repo, "text": questions[i].tr, "name": name, "arms": ("original", "hints")} for i, name in REAL_NAMES.items()]
    for repo, kind, name in indexed_names(store):
        items.append({"kind": f"name: {kind}", "id": f"{repo}:{name}", "repo": repo, "text": NAME_FRAME.format(name), "name": name, "arms": ("original", "hints")})
    return items


def run(step: str, output: Path, min_free_gb: float) -> int:
    from repomind.cli import _embedder, _store
    from repomind.config import Settings
    from repomind.evaluation.ask_runner import Checkpoint
    from repomind.evaluation.memory import available_memory_gb
    from repomind.qa.engine import QAEngine
    from repomind.qa.llm import OpenAICompatibleClient, load_llm_config
    from repomind.search.engine import SearchConfig, SearchEngine
    from repomind.search.glossary import repository_terms
    from repomind.search.tokenizer import tokenize

    if output.exists():
        print(f"{output} already exists, and is not overwritten.")
        return 2
    settings = Settings.load()
    config = load_llm_config()
    if config is None or config.model != MODEL:
        print(f"This trial is for {MODEL}, and the configured model is {config.model if config else None}.")
        return 2
    questions = all_questions()
    checkpoint = Checkpoint(output.with_name(output.name + ".checkpoint.jsonl"), {"model": config.model, "trial": "translation", "step": step, "hint": HINT})
    finished = checkpoint.load()
    status = "complete"
    with _store(settings) as store:
        search = SearchEngine(store, _embedder(settings), SearchConfig(), None)
        qa = QAEngine(store, search, config, OpenAICompatibleClient(config))
        items = plan(store, questions, step)
        for item in items:
            vocabulary = search.vocabulary(item["text"], None, [item["repo"]])
            terms = repository_terms(item["text"], vocabulary)
            for arm in item["arms"]:
                if (arm, item["id"], "tr") in finished:
                    continue
                free = available_memory_gb()
                if free is not None and free < min_free_gb:
                    status = f"incomplete — stopped at {free:.1f} GB free"
                    break
                reply = translate(qa, item["text"], terms if arm == "hints" else [])
                final = reply["final"] or ""
                content = set(tokenize(final))
                question = questions.get(item["id"])
                anchors = set()
                for span in getattr(question, "accepted", ()):
                    anchors |= set(tokenize(span.anchor))
                row = {
                    "question": item["id"],
                    "language": "tr",
                    "kind": item["kind"],
                    "arm": arm,
                    "text": item["text"],
                    "name": item.get("name"),
                    "hints": terms if arm == "hints" else [],
                    **reply,
                    "terms": len(content),
                    "terms_in_repository": sum(term in vocabulary for term in content),
                    "terms_not_in_repository": sorted(term for term in content if term not in vocabulary),
                    "shared_with_gold_anchor": sorted(content & anchors),
                    "free_memory_gb": None if free is None else round(free, 2),
                }
                checkpoint.add(arm, row)
                print(f"{arm:<9}{item['id']:<46}{reply['seconds']:>5} s  {final}", flush=True)
            if status != "complete":
                break
    rows = [json.loads(line)["row"] for line in checkpoint.path.read_text(encoding="utf-8").splitlines()[1:]]
    output.write_text(json.dumps({"model": config.model, "step": step, "status": status, "rows": rows}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(status)
    return 0 if status == "complete" else 3


def passes(identifier: str, text: str | None) -> bool:
    if not text:
        return False
    must, must_not = KNOWN_ERRORS[identifier]
    return all(re.search(p, text, re.I) for p in must) and not any(re.search(p, text, re.I) for p in must_not)


def report_all(rows: list[dict], grades_path: Path | None, key_path: Path | None) -> None:
    from repomind.search.tokenizer import tokenize
    from repomind.search.translation import _TECHNICAL_TERM

    questions = all_questions()
    for arm in ("original", "hints"):
        members = [row for row in rows if row["arm"] == arm]
        shares = []
        for row in members:
            english = set(tokenize(questions[row["question"]].en))
            shares.append(len(set(tokenize(row["final"] or "")) & english) / max(len(english), 1))
        names = lost = 0
        lost_names = []
        for row in members:
            for name in dict.fromkeys(_TECHNICAL_TERM.findall(row["text"])):
                name = name.strip("`")
                names += 1
                if not (row["raw"] and name.casefold() in row["raw"].casefold()):
                    lost += 1
                    lost_names.append(f"{row['question']}:{name}")
        not_questions = [f"{row['question']}: {row['raw']}" for row in members if row["raw"] and not row["raw"].rstrip().endswith("?")]
        print(f"\n{arm}: {len(members)} translations, mean share of the developer's English terms {sum(shares) / len(shares):.3f}")
        print(f"  technical names lost in the reply: {lost} of {names} {lost_names}")
        print(f"  replies that are not a question: {len(not_questions)}")
        for item in not_questions:
            print(f"    {item}")
    by = {(row["question"], row["arm"]): row for row in rows}
    differ = [i for (i, arm) in by if arm == "original" and (i, "hints") in by and by[(i, "original")]["final"] != by[(i, "hints")]["final"]]
    print(f"\nthe two prompts wrote different wordings for {len(differ)} of {sum(arm == 'original' for _, arm in by)} questions")
    if grades_path and key_path:
        grades = json.loads(grades_path.read_text(encoding="utf-8"))
        key = json.loads(key_path.read_text(encoding="utf-8"))
        outcome: dict[str, list[str]] = {"both good": [], "both bad": [], "only the glossary prompt good": [], "only the current prompt good": []}
        for label, grade in grades.items():
            arms = {key[label]["A"]: grade["A"], key[label]["B"]: grade["B"]}
            if arms["original"] == arms["hints"]:
                outcome["both good" if arms["original"] == "good" else "both bad"].append(key[label]["question"])
            else:
                outcome["only the glossary prompt good" if arms["hints"] == "good" else "only the current prompt good"].append(key[label]["question"])
        for name, members in outcome.items():
            print(f"  {name}: {len(members)} {members}")


def blind(results: Path, pack: Path, key_path: Path) -> int:
    data = json.loads(results.read_text(encoding="utf-8"))
    questions = all_questions()
    by = {(row["question"], row["arm"]): row for row in data["rows"]}
    differ = [i for (i, arm) in by if arm == "original" and (i, "hints") in by and by[(i, "original")]["final"] != by[(i, "hints")]["final"]]
    shuffler = random.Random(20261107)
    shuffler.shuffle(differ)
    entries, key = [], {}
    for number, identifier in enumerate(differ, start=1):
        label = f"T{number:03d}"
        first, second = shuffler.sample(("original", "hints"), 2)
        key[label] = {"question": identifier, "A": first, "B": second}
        entries.append({
            "label": label,
            "turkish": questions[identifier].tr,
            "developers_english": questions[identifier].en,
            "A": by[(identifier, first)]["final"],
            "B": by[(identifier, second)]["final"],
        })
    pack.write_text(json.dumps(entries, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    key_path.write_text(json.dumps(key, indent=1) + "\n", encoding="utf-8")
    print(f"{len(entries)} pairs to grade")
    return 0


def compare(first_path: Path, second_path: Path) -> int:
    first = json.loads(first_path.read_text(encoding="utf-8"))
    second = json.loads(second_path.read_text(encoding="utf-8"))
    a, b = first["questions"], second["questions"]
    print(f"{first_path.name} -> {second_path.name}")
    answerable = [i for i, row in a.items() if not row.get("negative")]
    negative = [i for i, row in a.items() if row.get("negative")]
    for stage in ("candidates", "passages", "anchor", "evidence", "refused"):
        won = [i for i in answerable if b[i][stage] and not a[i][stage]]
        lost = [i for i in answerable if a[i][stage] and not b[i][stage]]
        print(f"  {stage:<11}{sum(a[i][stage] for i in answerable):>4} -> {sum(b[i][stage] for i in answerable):<4} +{len(won)} {won}\n{'':<15}-{len(lost)} {lost}")
    won = [i for i in negative if b[i]["refused"] and not a[i]["refused"]]
    lost = [i for i in negative if a[i]["refused"] and not b[i]["refused"]]
    print(f"  unanswerable refused before the model {sum(a[i]['refused'] for i in negative)} -> {sum(b[i]['refused'] for i in negative)} of {len(negative)}; newly refused {won}; no longer refused {lost}")
    for name in sorted({row["set"] for row in a.values()}):
        members = [i for i in answerable if a[i]["set"] == name]
        print(f"  {name:<8} passages {sum(a[i]['passages'] for i in members)} -> {sum(b[i]['passages'] for i in members)}, evidence {sum(a[i]['evidence'] for i in members)} -> {sum(b[i]['evidence'] for i in members)} of {len(members)}")
    config = [i for i in answerable if a[i]["config"]]
    print(f"  SQL or YAML, {len(config)} questions: passages {sum(a[i]['passages'] for i in config)} -> {sum(b[i]['passages'] for i in config)}, evidence {sum(a[i]['evidence'] for i in config)} -> {sum(b[i]['evidence'] for i in config)}")
    print(f"  {first['summary']}\n  {second['summary']}")
    return 0


def report(results: Path, grades_path: Path | None = None, key_path: Path | None = None) -> int:
    data = json.loads(results.read_text(encoding="utf-8"))
    rows = data["rows"]
    print(f"model {data['model']}, step {data['step']}, {data['status']}, {len(rows)} translations")
    by = {(row["question"], row["arm"]): row for row in rows}
    stored = stored_translations()
    if data["step"] == "all":
        report_all(rows, grades_path, key_path)
        same = sum(by[(i, "original")]["final"] == stored.get(i) for i, arm in by if arm == "original")
        print(f"\nthis run's current prompt matches the stored translation for {same} of {sum(arm == 'original' for _, arm in by)} questions")
    errors = [i for i in KNOWN_ERRORS if (i, "original") in by and (i, "hints") in by]
    if errors:
        print("\nknown errors")
        fixed, broken, both, neither = [], [], [], []
        for identifier in errors:
            original, hinted = by[(identifier, "original")], by[(identifier, "hints")]
            a, b = passes(identifier, original["final"]), passes(identifier, hinted["final"])
            (both if a and b else fixed if b else broken if a else neither).append(identifier)
            same = "same as stored" if original["final"] == stored.get(identifier) else "differs from stored"
            print(f"  {identifier:<30} {'ok ' if a else 'bad'} -> {'ok ' if b else 'bad'}  hints {hinted['hints']}\n     tr       {original['text']}\n     original {original['final']} ({same})\n     hints    {hinted['final']}")
        print(f"\nfixed {len(fixed)} {fixed}\nbroken {len(broken)} {broken}\nright in both {len(both)} {both}\nwrong in both {len(neither)} {neither}")
        for arm in ("original", "hints"):
            members = [by[(i, arm)] for i in errors]
            print(f"  {arm:<9} terms in repository {sum(r['terms_in_repository'] for r in members)} of {sum(r['terms'] for r in members)}; shared with gold anchors {sum(len(r['shared_with_gold_anchor']) for r in members)}")
        print("\ntechnical names, in the model's own reply before anything is added back")
    named = [row for row in rows if row["name"]]
    for arm in ("original", "hints"):
        members = [row for row in named if row["arm"] == arm]
        if members:
            lost = [row["name"] for row in members if not (row["raw"] and row["name"].casefold() in row["raw"].casefold())]
            exact = [row["name"] for row in members if row["raw"] and row["name"] in row["raw"]]
            final = [row["name"] for row in members if not (row["final"] and row["name"].casefold() in row["final"].casefold())]
            print(f"  {arm:<9} {len(members)} names: exact {len(exact)}, lost in the reply {len(lost)} {lost}, still lost after adding back {len(final)} {final}")
    for arm in ("original", "hints"):
        members = [row for row in rows if row["arm"] == arm]
        if members:
            seconds = sorted(row["seconds"] for row in members)
            print(f"  {arm:<9} seconds per translation: median {seconds[len(seconds) // 2]}, max {seconds[-1]}; tokens in {sum(r['input_tokens'] for r in members)}, out {sum(r['output_tokens'] for r in members)}; unusable replies {sum(r['final'] is None for r in members)}; lowest free memory {min(r['free_memory_gb'] for r in members)} GB")
    return 0


def wordings(results: Path, arm: str, output: Path) -> int:
    data = json.loads(results.read_text(encoding="utf-8"))
    chosen = {row["question"]: row["final"] for row in data["rows"] if row["arm"] == arm and row["final"] and ":" not in row["question"] and row["kind"] in ("error", "real name", "question", "negative")}
    output.write_text(json.dumps(chosen, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(chosen)} wordings written")
    return 0


def check() -> int:
    from repomind.cli import _embedder, _store
    from repomind.config import Settings
    from repomind.search.engine import SearchConfig, SearchEngine
    from repomind.search.glossary import repository_terms

    settings = Settings.load()
    questions, stored = all_questions(), stored_translations()
    with _store(settings) as store:
        search = SearchEngine(store, _embedder(settings), SearchConfig(), None)
        for item in plan(store, questions, "limited"):
            terms = repository_terms(item["text"], search.vocabulary(item["text"], None, [item["repo"]]))
            note = ""
            if item["kind"] == "error":
                note = f"stored passes its check: {passes(item['id'], stored[item['id']])}"
            print(f"{item['kind']:<30}{item['id']:<46}{terms} {note}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    for step in ("limited", "all"):
        command = commands.add_parser(step)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--min-free-memory-gb", type=float, default=4.0)
    command = commands.add_parser("report")
    command.add_argument("results", type=Path)
    command.add_argument("--grades", type=Path)
    command.add_argument("--key", type=Path)
    command = commands.add_parser("wordings")
    command.add_argument("results", type=Path)
    command.add_argument("--arm", default="hints")
    command.add_argument("--output", type=Path, required=True)
    command = commands.add_parser("blind")
    command.add_argument("results", type=Path)
    command.add_argument("--pack", type=Path, required=True)
    command.add_argument("--key", type=Path, required=True)
    command = commands.add_parser("compare")
    command.add_argument("first", type=Path)
    command.add_argument("second", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "check":
        return check()
    if arguments.command in ("limited", "all"):
        return run(arguments.command, arguments.output, arguments.min_free_memory_gb)
    if arguments.command == "report":
        return report(arguments.results, arguments.grades, arguments.key)
    if arguments.command == "blind":
        return blind(arguments.results, arguments.pack, arguments.key)
    if arguments.command == "compare":
        return compare(arguments.first, arguments.second)
    return wordings(arguments.results, arguments.arm, arguments.output)


if __name__ == "__main__":
    sys.exit(main())
