"""How far the right code gets, with the flow and rules `ask` uses, and no LLM.

Stored 7B translations stand in for the model's, so nothing is loaded and the run takes minutes.
These questions have all been read, so this is development data and proves nothing independent.

    python eval/tools/retrieval_funnel.py <code folder> <7b|local|en> [--output rows.json]

The code folder is this project, or a frozen copy of an earlier commit, which needs its own
`eval/*.yaml` beside it. Arguments that a version does not have yet are simply not passed.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
import inspect
import json
import os
import re
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("code", type=Path)
parser.add_argument("mode", choices=("7b", "local", "en"))
parser.add_argument("--output", type=Path)
arguments = parser.parse_args()

sys.path.insert(0, str(arguments.code))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("REPOMIND_MODELS_DIR", str(PROJECT / ".models"))

import repomind  # noqa: E402
from repomind.cli import _embedder, _store  # noqa: E402
from repomind.config import Settings  # noqa: E402
from repomind.evaluation.dataset import (  # noqa: E402
    V2_DATASET_PATH,
    V3_DATASET_PATH,
    V4_DATASET_PATH,
    load_dataset,
    load_holdout_unanswerable,
)
from repomind.evaluation.metrics import overlaps, touches  # noqa: E402
from repomind.qa.passages import is_evidence, select_passages  # noqa: E402
from repomind.qa.sufficiency import check_sufficiency  # noqa: E402
from repomind.search.engine import SearchConfig, SearchEngine  # noqa: E402
from repomind.search.translation import LocalTurkishTranslator  # noqa: E402

assert Path(repomind.__file__).parent.parent.resolve() == arguments.code.resolve(), repomind.__file__

CHUNKS = 12
SEATS = 5
RESULTS = PROJECT / "eval" / "results"


def stored_translations() -> dict[str, str]:
    known = json.loads((RESULTS / "dev-phase3-translations.json").read_text(encoding="utf-8"))
    fourth = json.loads((RESULTS / "v4-improved-7b-tr.json").read_text(encoding="utf-8"))
    known.update({row["question"]: row["translated_query"] for row in fourth["answerable"] + fourth["unanswerable"]})
    return known


def squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


class _Counters(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD), ("PageFaultCount", w.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t), ("d", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def peak_memory_mb() -> int | None:
    if sys.platform != "win32":
        return None
    counters = _Counters()
    counters.cb = ctypes.sizeof(counters)
    psapi, kernel32 = ctypes.WinDLL("psapi"), ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = w.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.POINTER(_Counters), w.DWORD]
    psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    return round(counters.PeakWorkingSetSize / 1024**2)


translations = stored_translations()
settings = Settings.load()
with _store(settings) as store:
    embedder = _embedder(settings)
    engine = SearchEngine(store, embedder, SearchConfig(), LocalTurkishTranslator(settings.models_dir))
    has_seats = "glossary_seats" in inspect.signature(engine.search).parameters
    has_terms = "more_terms" in inspect.signature(select_passages).parameters
    first, second_set, third, fourth = (load_dataset(), load_dataset(V2_DATASET_PATH), load_dataset(V3_DATASET_PATH), load_dataset(V4_DATASET_PATH))
    sets = {
        "first": (first.questions, (*first.unanswerable, *load_holdout_unanswerable())),
        "second": (second_set.questions, second_set.unanswerable),
        "third": (third.questions, third.unanswerable),
        "fourth": (fourth.questions, fourth.unanswerable),
    }
    engine.search("warm up", "hybrid", ["stockguard"], 1)

    def run(question):
        if arguments.mode == "7b":
            query, english, second = question.tr, translations[question.id], engine.local_translation(question.tr, [])
        elif arguments.mode == "local":
            query, english, second = question.tr, None, None
        else:
            query, english, second = question.en, None, None
        found = engine.search(
            query, "hybrid", [question.repo], CHUNKS, translated=english, more_wordings=[second] if second else (),
            **({"glossary_seats": SEATS} if has_seats else {}),
        )
        wording = found.translated_query or query
        gate = check_sufficiency(query, found.translated_query, engine.vocabulary(query, found.translated_query, [question.repo])).insufficient
        queries = [text for text in (query, found.translated_query, second) if text]
        chunks = [hit.chunk for hit in found.hits]
        more = {"more_terms": found.glossary_terms} if has_terms else {}
        passages = [] if gate else select_passages(chunks, queries, embedder, **more)
        return chunks, passages, [p for p in passages if is_evidence(p, wording)], gate

    rows: dict[str, dict] = {}
    timings: list[float] = []
    lines: list[int] = []
    sizes: list[int] = []
    print(f"{'set':<8}{'n':>4}{'cand':>6}{'pass':>6}{'anch':>6}{'evid':>6}{'refused':>9}{'neg refused':>13}")
    for name, (questions, negatives) in sets.items():
        counts = dict.fromkeys(("candidates", "passages", "anchor", "evidence", "refused"), 0)
        for question in questions:
            started = time.perf_counter()
            chunks, passages, good, gate = run(question)
            timings.append(time.perf_counter() - started)
            text = squeeze("\n".join(p.text for p in passages))
            row = {
                "set": name,
                "repo": question.repo,
                "candidates": any(overlaps(c, s) for c in chunks for s in question.accepted),
                "passages": any(touches(p, s) for p in passages for s in question.accepted),
                "anchor": any(squeeze(s.anchor) in text for s in question.accepted),
                "evidence": any(touches(p, s) for p in good for s in question.accepted),
                "refused": bool(gate or not good),
                "config": all(s.path.endswith((".yml", ".yaml", ".sql")) for s in question.accepted),
            }
            rows[question.id] = row
            for key in counts:
                counts[key] += row[key]
            lines.append(sum(p.end_line - p.start_line + 1 for p in passages))
            sizes.append(len(chunks))
        refused = 0
        for question in negatives:
            started = time.perf_counter()
            _, _, good, gate = run(question)
            timings.append(time.perf_counter() - started)
            refused += bool(gate or not good)
            rows[question.id] = {"set": name, "repo": question.repo, "negative": True, "refused": bool(gate or not good)}
        print(f"{name:<8}{len(questions):>4}{counts['candidates']:>6}{counts['passages']:>6}{counts['anchor']:>6}{counts['evidence']:>6}{counts['refused']:>9}{refused:>10}/{len(negatives)}", flush=True)

    answerable = [row for row in rows.values() if not row.get("negative")]
    negative = [row for row in rows.values() if row.get("negative")]

    def total(group: list[dict], key: str) -> int:
        return sum(bool(row[key]) for row in group)

    def line(label: str, group: list[dict]) -> str:
        return f"{label:<8}{len(group):>4}{total(group, 'candidates'):>6}{total(group, 'passages'):>6}{total(group, 'anchor'):>6}{total(group, 'evidence'):>6}"

    print(f"{line('all', answerable)}{total(answerable, 'refused'):>9}{total(negative, 'refused'):>10}/{len(negative)}")
    print(line("config", [row for row in answerable if row["config"]]))
    print(line("code", [row for row in answerable if not row["config"]]))
    timings.sort()
    summary = {
        "median_seconds": round(timings[len(timings) // 2], 2),
        "mean_seconds": round(sum(timings) / len(timings), 2),
        "p95_seconds": round(timings[int(len(timings) * 0.95)], 2),
        "mean_chunks": round(sum(sizes) / len(sizes), 1),
        "mean_lines_shown": round(sum(lines) / len(lines), 1),
        "peak_memory_mb": peak_memory_mb(),
    }
    print(summary)
    if arguments.output:
        arguments.output.write_text(json.dumps({"summary": summary, "questions": rows}, indent=1) + "\n", encoding="utf-8")
