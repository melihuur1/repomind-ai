"""Compares evidence rules on the same replies of the model. See eval/TRIALS.md.

The model sees the same passages under every rule, so each question needs one request, and each
rule is then worked out from the reply. Stored translations are used, so no translation runs.

    python eval/tools/evidence_trial.py check
    python eval/tools/evidence_trial.py run --output results.json
    python eval/tools/evidence_trial.py blind results.json --pack pack.json --key key.json
    python eval/tools/evidence_trial.py report results.json [--grades grades.json]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8")

RESULTS = PROJECT / "eval" / "results"
MODEL = "qwen2.5:7b"
GLOSSARY_SEATS = 5
RULES = ("strict", "flexible", "either", "model")
GROUPS = {
    "A": ("sg-oversell", "sn-offline", "sn-ticket", "sn-version-restore", "sn-presence", "v2-sg-negative-stock-db", "v2-sg-reservation-defaults", "v2-sg-add-stock-limits", "v2-sn-collab-instances", "v2-sn-note-search", "v4-sg-username-format", "v4-sn-postgres-port"),
    "B": ("v2-sg-neg-transfer", "v2-sn-neg-tags", "v2-sn-neg-comments", "v3-sg-neg-low-stock-alert", "v4-sg-neg-reduce-quantity"),
    "C": ("sn-redis-outage", "sn-session-refresh"),
    "D": ("v4-sn-neg-view-log", "v3-sn-neg-sunset-theme"),
}
NEGATIVE_GROUPS = {"B", "D"}


def stored_translations() -> dict[str, str]:
    known = json.loads((RESULTS / "dev-phase3-translations.json").read_text(encoding="utf-8"))
    fourth = json.loads((RESULTS / "v4-improved-7b-tr.json").read_text(encoding="utf-8"))
    known.update({row["question"]: row["translated_query"] for row in fourth["answerable"] + fourth["unanswerable"]})
    return known


def all_questions() -> dict:
    from repomind.evaluation.dataset import V2_DATASET_PATH, V3_DATASET_PATH, V4_DATASET_PATH, load_dataset, load_holdout_unanswerable

    first, second, third, fourth = load_dataset(), load_dataset(V2_DATASET_PATH), load_dataset(V3_DATASET_PATH), load_dataset(V4_DATASET_PATH)
    items = [*load_holdout_unanswerable()]
    for dataset in (first, second, third, fourth):
        items.extend((*dataset.questions, *dataset.unanswerable))
    return {item.id: item for item in items}


def concepts(question: str, wording: str, vocabulary) -> list[set[str]]:
    """One set per Turkish word's senses, or per other term of the translation, so that a word counts once."""
    from repomind.search import glossary
    from repomind.search.tokenizer import _WORD, fold, tokenize

    words = [fold(word) for word in _WORD.findall(question)]
    joined = " ".join(words)
    senses = [english for phrase, english in glossary._PHRASES.items() if phrase in joined]
    for word in words:
        if word in glossary._WHOLE_WORDS:
            senses.append(glossary._WHOLE_WORDS[word])
        elif stem := next((s for s in glossary._LONGEST_FIRST if word.startswith(s)), None):
            senses.append(glossary._STEMS[stem])
    groups: list[set[str]] = []
    for english in senses:
        group = {t for term in english.split() if all(x in vocabulary for x in tokenize(term)) for t in tokenize(term)}
        if group:
            groups.append(group)
    for term in set(tokenize(wording)):
        if not any(term in group for group in groups):
            groups.append({term})
    merged: list[set[str]] = []
    for group in groups:
        for other in [m for m in merged if m & group]:
            group = group | other
            merged.remove(other)
        merged.append(group)
    return merged


def prepare(question, english, search, embedder, chunk_limit):
    from repomind.qa.passages import select_passages
    from repomind.qa.sufficiency import check_sufficiency

    second = search.local_translation(question.tr, [])
    found = search.search(question.tr, "hybrid", [question.repo], chunk_limit, translated=english, more_wordings=[second] if second else (), glossary_seats=GLOSSARY_SEATS)
    vocabulary = search.vocabulary(question.tr, english, [question.repo])
    gate = check_sufficiency(question.tr, english, vocabulary).insufficient
    queries = [text for text in (question.tr, english, second) if text]
    passages = select_passages([hit.chunk for hit in found.hits], queries, embedder, more_terms=found.glossary_terms)
    return passages, gate, second, vocabulary


def describe(passage, question, english, second, groups, offered: bool) -> dict:
    from repomind.evaluation.metrics import touches
    from repomind.qa.passages import MIN_SHARED_TERMS, is_evidence
    from repomind.search.tokenizer import tokenize

    tokens = set(tokenize(passage.text))
    either = set(tokenize(english)) | (set(tokenize(second)) if second else set())
    accepted = getattr(question, "accepted", ())
    return {
        "id": passage.id,
        "path": passage.path,
        "start_line": passage.start_line,
        "end_line": passage.end_line,
        "text": passage.text,
        "offered": offered,
        "shared_with_translation": sorted(tokens & set(tokenize(english))),
        "strict": is_evidence(passage, english),
        "flexible": sum(bool(group & tokens) for group in groups) >= min(MIN_SHARED_TERMS, len(groups)),
        "either": len(either & tokens) >= min(MIN_SHARED_TERMS, len(either)),
        "gold": any(touches(passage, span) for span in accepted),
    }


def check() -> int:
    from repomind.cli import _embedder, _store
    from repomind.config import Settings
    from repomind.search.engine import SearchConfig, SearchEngine
    from repomind.search.translation import LocalTurkishTranslator

    settings = Settings.load()
    questions, translations = all_questions(), stored_translations()
    with _store(settings) as store:
        embedder = _embedder(settings)
        search = SearchEngine(store, embedder, SearchConfig(), LocalTurkishTranslator(settings.models_dir))
        for group, ids in GROUPS.items():
            for identifier in ids:
                question, english = questions[identifier], translations[identifier]
                passages, gate, second, vocabulary = prepare(question, english, search, embedder, 12)
                groups = concepts(question.tr, english, vocabulary)
                rows = [describe(p, question, english, second, groups, True) for p in passages]
                print(f"{group} {identifier:<30} gate {gate!s:<6} passages {len(rows)} gold {sum(r['gold'] for r in rows)} strict {sum(r['strict'] for r in rows)} flexible {sum(r['flexible'] for r in rows)} either {sum(r['either'] for r in rows)}")
    return 0


def run(output: Path, min_free_gb: float) -> int:
    from repomind.cli import _embedder, _store
    from repomind.config import Settings
    from repomind.evaluation.ask_runner import Checkpoint
    from repomind.evaluation.memory import available_memory_gb, check_before_loading
    from repomind.qa.engine import AskResult, QAEngine
    from repomind.qa.llm import OpenAICompatibleClient, load_llm_config
    from repomind.qa.prompt import build_selection_prompt, fit_passages, new_nonce
    from repomind.qa.verification import verify_selection
    from repomind.search.engine import SearchConfig, SearchEngine
    from repomind.search.translation import LocalTurkishTranslator

    if output.exists():
        print(f"{output} already exists, and is not overwritten.")
        return 2
    check = check_before_loading()
    print(f"Free memory before the model loads: {check.describe()}", flush=True)
    if check.refusal:
        print(f"Not started: {check.refusal}. Nothing was loaded.")
        return 3
    settings = Settings.load()
    config = load_llm_config()
    if config is None or config.model != MODEL:
        print(f"This trial is for {MODEL}, and the configured model is {config.model if config else None}.")
        return 2
    questions, translations = all_questions(), stored_translations()
    checkpoint = Checkpoint(output.with_name(output.name + ".checkpoint.jsonl"), {"model": config.model, "trial": "evidence", "groups": GROUPS})
    finished = checkpoint.load()
    status = "complete"
    with _store(settings) as store:
        embedder = _embedder(settings)
        search = SearchEngine(store, embedder, SearchConfig(), LocalTurkishTranslator(settings.models_dir))
        qa = QAEngine(store, search, config, OpenAICompatibleClient(config))
        for group, ids in GROUPS.items():
            for identifier in ids:
                if (group, identifier, "tr") in finished:
                    continue
                free = available_memory_gb()
                if free is not None and free < min_free_gb:
                    status = f"incomplete — stopped at {free:.1f} GB free"
                    break
                question, english = questions[identifier], translations[identifier]
                passages, gate, second, vocabulary = prepare(question, english, search, embedder, config.max_context_chunks)
                groups = concepts(question.tr, english, vocabulary)
                result = AskResult(status="trial", question=question.tr)
                verified = qa._verified_passages(passages, result)
                nonce = new_nonce()
                offered, _ = fit_passages(question.tr, verified, config.max_input_tokens, nonce, english)
                started = time.perf_counter()
                reply = qa._call(build_selection_prompt(question.tr, offered, nonce, english), config.max_output_tokens, result)
                seconds = time.perf_counter() - started
                selection = verify_selection(reply, offered, question.tr)
                shown = {passage.id for passage in offered}
                row = {
                    "question": identifier,
                    "language": "tr",
                    "group": group,
                    "repo": question.repo,
                    "tr": question.tr,
                    "en": question.en,
                    "expected": getattr(question, "expected", ""),
                    "translated_query": english,
                    "vocabulary_gate": gate,
                    "passages": [describe(p, question, english, second, groups, p.id in shown) for p in verified],
                    "outcome": selection.outcome,
                    "chosen": [passage.id for passage in selection.chosen],
                    "note": selection.note,
                    "note_problem": selection.note_problem,
                    "reason": selection.reason,
                    "lines_failing_git": result.rejected,
                    "seconds": round(seconds, 2),
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "free_memory_gb": None if free is None else round(free, 2),
                }
                checkpoint.add(group, row)
                print(f"{group} {identifier:<30} {selection.outcome:<8} {row['chosen']} {seconds:.1f} s, {free:.1f} GB free", flush=True)
            if status != "complete":
                break
    rows = [json.loads(line)["row"] for line in checkpoint.path.read_text(encoding="utf-8").splitlines()[1:]]
    output.write_text(json.dumps({"model": config.model, "status": status, "rows": rows}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(status)
    return 0 if status == "complete" else 3


def blind(results: Path, pack: Path, key: Path) -> int:
    data = json.loads(results.read_text(encoding="utf-8"))
    chosen = [row for row in data["rows"] if row["outcome"] == "chosen"]
    random.Random(20261107).shuffle(chosen)
    entries, mapping = [], {}
    for number, row in enumerate(chosen, start=1):
        label = f"B{number:02d}"
        by_id = {p["id"]: p for p in row["passages"]}
        mapping[label] = row["question"]
        entries.append({
            "label": label,
            "question": row["tr"],
            "question_en": row["en"],
            "expected": (
                "UNANSWERABLE: the repository does not have this feature."
                if row["group"] in NEGATIVE_GROUPS
                else row["expected"] or "No note for this question: judge by the question and the code."
            ),
            "lines": [f"{by_id[i]['path']}:{by_id[i]['start_line']}-{by_id[i]['end_line']}\n{by_id[i]['text']}" for i in row["chosen"]],
            "models_reading": row["note"],
        })
    pack.write_text(json.dumps(entries, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    key.write_text(json.dumps(mapping, indent=1) + "\n", encoding="utf-8")
    print(f"{len(entries)} choices to grade")
    return 0


def decide(row: dict, rule: str) -> tuple[str, list[dict]]:
    """How a rule ends this question: refused_before, declined, dropped, invalid, or accepted, with the lines kept."""
    by_id = {p["id"]: p for p in row["passages"]}
    if rule != "model" and not any(p[rule] for p in row["passages"]):
        return "refused_before", []
    if row["outcome"] == "invalid":
        return "invalid", []
    if row["outcome"] == "none":
        return "declined", []
    chosen = [by_id[i] for i in row["chosen"]]
    kept = chosen if rule == "model" else [p for p in chosen if p[rule]]
    return ("accepted", kept) if kept else ("dropped", [])


def report(results: Path, grades_path: Path | None) -> int:
    data = json.loads(results.read_text(encoding="utf-8"))
    rows = data["rows"]
    grades = json.loads(grades_path.read_text(encoding="utf-8")) if grades_path else {}
    print(f"model {data['model']}, {data['status']}, {len(rows)} questions")
    print(f"\n{'':<10}{'correct source':>16}{'wrong source':>14}{'no answer':>11}{'right lines dropped':>21}{'unanswerable accepted':>23}{'refused before model':>22}{'model asked':>13}")
    for rule in RULES:
        counts: Counter = Counter()
        for row in rows:
            ending, kept = decide(row, rule)
            model_chose_gold = row["outcome"] == "chosen" and any(p["gold"] for p in row["passages"] if p["id"] in row["chosen"])
            counts["refused_before"] += ending == "refused_before"
            counts["asked"] += ending != "refused_before"
            if row["group"] in NEGATIVE_GROUPS:
                counts["false_accept"] += ending == "accepted"
            elif ending == "accepted":
                counts["correct" if any(p["gold"] for p in kept) else "wrong"] += 1
            else:
                counts["no_answer"] += 1
                counts["dropped_gold"] += model_chose_gold
        print(f"{rule:<10}{counts['correct']:>16}{counts['wrong']:>14}{counts['no_answer']:>11}{counts['dropped_gold']:>21}{counts['false_accept']:>23}{counts['refused_before']:>22}{counts['asked']:>13}")

    print("\nthe model itself, before any rule")
    for group in GROUPS:
        members = [row for row in rows if row["group"] == group]
        print(f"  {group}: {len(members)} questions, " + ", ".join(f"{k} {v}" for k, v in Counter(row["outcome"] for row in members).items()) + f"; chose right lines {sum(any(p['gold'] for p in row['passages'] if p['id'] in row['chosen']) for row in members)}")

    print("\nquestion by question")
    for row in rows:
        by_id = {p["id"]: p for p in row["passages"]}
        chosen = [by_id[i] for i in row["chosen"]]
        endings = " ".join(f"{rule}={decide(row, rule)[0]}" for rule in ("strict", "flexible"))
        lines = "; ".join(f"{p['path'].split('/')[-1]}:{p['start_line']}-{p['end_line']}{' GOLD' if p['gold'] else ''} shares {len(p['shared_with_translation'])}" for p in chosen)
        print(f"  {row['group']} {row['question']:<30} {row['outcome']:<7} {endings:<48} grade {grades.get(row['question'], '-'):<10} {lines}")

    if grades:
        print("\ngraded lines: do they really answer?")
        for rule in RULES:
            counts = Counter()
            for row in rows:
                ending, _ = decide(row, rule)
                if ending == "accepted":
                    counts[("negative " if row["group"] in NEGATIVE_GROUPS else "answerable ") + grades.get(row["question"], "ungraded")] += 1
            print(f"  {rule:<10}" + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        few = [row["question"] for row in rows if row["outcome"] == "chosen" and grades.get(row["question"]) in ("correct", "partial") and all(len(p["shared_with_translation"]) < 2 for p in row["passages"] if p["id"] in row["chosen"])]
        print(f"  chosen lines sharing fewer than 2 terms, that still answer: {few}")

    seconds = sorted(row["seconds"] for row in rows)
    print(f"\nseconds per model request: median {seconds[len(seconds) // 2]}, max {seconds[-1]}; tokens in {sum(r['input_tokens'] for r in rows)}, out {sum(r['output_tokens'] for r in rows)}; lowest free memory {min(r['free_memory_gb'] for r in rows)} GB")
    print(f"lines offered that failed the Git check: {sum(len(r['lines_failing_git']) for r in rows)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    command = commands.add_parser("run")
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--min-free-memory-gb", type=float, default=4.0)
    command = commands.add_parser("blind")
    command.add_argument("results", type=Path)
    command.add_argument("--pack", type=Path, required=True)
    command.add_argument("--key", type=Path, required=True)
    command = commands.add_parser("report")
    command.add_argument("results", type=Path)
    command.add_argument("--grades", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "check":
        return check()
    if arguments.command == "run":
        return run(arguments.output, arguments.min_free_memory_gb)
    if arguments.command == "blind":
        return blind(arguments.results, arguments.pack, arguments.key)
    return report(arguments.results, arguments.grades)


if __name__ == "__main__":
    sys.exit(main())
