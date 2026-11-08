"""Where the right code is lost between search and the passages, for every development question. No LLM.

    python eval/tools/passage_trace.py [--output rows.json]

Two losses are kept apart:

* ``not a candidate``: the right chunk is not among the 12 chunks and glossary seats that search
  hands on. Its fused rank, without seats, says how far away it was.
* ``not chosen``: it is a candidate, and no passage touches its lines. Then the window touching
  them is followed through the window ranking, with why it got no passage: the five were taken,
  its file already had three, no window touching it shares a term with the question, or its
  chunk was taken with other lines.

The passage order is rebuilt from select_passages' own parts, and checked to give the same passages.
Development data: these questions have been read. Nothing is written to the index.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from accuracy_funnel import CHUNKS, SEATS, question_sets, stored_translations  # noqa: E402

from repomind.config import Settings  # noqa: E402
from repomind.embeddings.provider import FastEmbedProvider  # noqa: E402
from repomind.evaluation.metrics import overlaps, touches  # noqa: E402
from repomind.qa import passages as selection  # noqa: E402
from repomind.search.engine import SearchConfig, SearchEngine  # noqa: E402
from repomind.search.hybrid import reciprocal_rank_fusion  # noqa: E402
from repomind.search.tokenizer import tokenize  # noqa: E402
from repomind.search.translation import LocalTurkishTranslator  # noqa: E402
from repomind.store.database import Store  # noqa: E402


def window_order(chunks, queries, embedder, more_terms):
    """The same order select_passages builds, with the three lists that made it."""
    terms = {term for query in (*queries, *more_terms) for term in tokenize(query)}
    windows = [w for w in selection._windows(chunks) if terms & w.tokens]
    if not windows:
        return terms, [], [], {}
    lists = {
        "terms": selection._by_shared_terms(windows, terms),
        "meaning": selection._by_meaning(windows, queries, embedder),
    }
    lists["search"] = selection._by_search(windows, lists)
    ordered = [windows[item.key] for item in reciprocal_rank_fusion(lists, k=selection._WINDOW_RRF_K)]
    closest = windows[lists["meaning"][0]]
    ordered = [closest, *(w for w in ordered if w is not closest)]
    return terms, windows, ordered, lists


def _touches(window, span) -> bool:
    return window.chunk.path == span.path and window.start_line <= span.end_line and span.start_line <= window.end_line


def why_not_chosen(question, chunks, terms, windows, ordered, lists) -> dict:
    gold_chunks = [c for c in chunks if any(overlaps(c, s) for s in question.accepted)]
    all_windows = selection._windows(gold_chunks)
    touching_all = [w for w in all_windows if any(_touches(w, s) for s in question.accepted)]
    touching = [w for w in ordered if any(_touches(w, s) for s in question.accepted)]
    first = touching[0] if touching else None
    detail = {
        "gold_chunks": [f"{c.path}:{c.start_line}-{c.end_line} ({c.end_line - c.start_line + 1} lines, candidate {chunks.index(c) + 1})" for c in gold_chunks],
        "windows_in_ranking": len(ordered),
    }
    if first is None:
        detail["reason"] = "no window touching the right lines shares a term with the question" if touching_all else "no window touches the right lines"
        return detail
    position = ordered.index(first)
    index = windows.index(first)
    before = selection._merged(ordered[:position], terms, selection.MAX_PASSAGES)
    same_file = sum(p.path == first.chunk.path for p in before)
    same_chunk = [p for p in before if p.chunk.id == first.chunk.id]
    if len(before) >= selection.MAX_PASSAGES:
        reason = "the five passages were taken by windows ranked above it"
    elif same_file >= selection.MAX_PASSAGES_PER_FILE:
        reason = "its file already had three passages"
    elif same_chunk:
        reason = "its chunk was already taken, with other lines"
    else:
        reason = "taken windows grew over it, or it shares no term once trimmed"
    detail.update(
        reason=reason,
        window=f"{first.chunk.path}:{first.start_line}-{first.end_line}",
        window_position=position + 1,
        ranks={name: ranked.index(index) + 1 for name, ranked in lists.items()},
        passages_above=[f"{p.path.split('/')[-1]}:{p.start_line}-{p.end_line}" for p in before],
        unit=first.chunk.end_line - first.chunk.start_line + 1 <= selection.UNIT_LINES,
    )
    return detail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.output and arguments.output.exists():
        parser.error(f"{arguments.output} already exists, and is not overwritten.")
    translations = stored_translations()
    settings = Settings.load()
    embedder = FastEmbedProvider(settings.embedding_model, settings.models_dir)
    rows = []
    with Store(settings.database_url, apply_schema=False) as store:
        engine = SearchEngine(store, embedder, SearchConfig(), LocalTurkishTranslator(settings.models_dir))
        for language in ("tr", "en"):
            for set_name, (answerable, _negatives) in question_sets().items():
                for question in answerable:
                    if language == "tr":
                        asked, english, second = question.tr, translations.get(question.id), engine.local_translation(question.tr, [])
                        if english is None:
                            english, second = second, None
                    else:
                        asked, english, second = question.en, None, None
                    more = [second] if second else ()
                    found = engine.search(asked, "hybrid", [question.repo], CHUNKS, translated=english, more_wordings=more, glossary_seats=SEATS)
                    chunks = [h.chunk for h in found.hits]
                    queries = [t for t in (asked, found.translated_query, second) if t]
                    terms, windows, ordered, lists = window_order(chunks, queries, embedder, found.glossary_terms)
                    chosen = selection._merged(ordered, terms, selection.MAX_PASSAGES) if ordered else []
                    assert [(p.path, p.start_line, p.end_line) for p in chosen] == [
                        (p.path, p.start_line, p.end_line) for p in selection.select_passages(chunks, queries, embedder, more_terms=found.glossary_terms)
                    ], f"the rebuilt order differs for {question.id}"
                    row = {"language": language, "set": set_name, "id": question.id}
                    if any(touches(p, s) for p in chosen for s in question.accepted):
                        row["stage"] = "chosen"
                    elif not any(overlaps(c, s) for c in chunks for s in question.accepted):
                        wide = engine.search(asked, "hybrid", [question.repo], 50, translated=english, more_wordings=more)
                        ranks = [rank for rank, hit in enumerate(wide.hits, start=1) if any(overlaps(hit.chunk, s) for s in question.accepted)]
                        row.update(stage="not a candidate", fused_rank=ranks[0] if ranks else None)
                    else:
                        row.update(stage="not chosen", **why_not_chosen(question, chunks, terms, windows, ordered, lists))
                    rows.append(row)

    for language in ("tr", "en"):
        group = [r for r in rows if r["language"] == language]
        stages = Counter(r["stage"] for r in group)
        far = Counter(
            "13-16" if r["fused_rank"] and r["fused_rank"] <= 16 else "17-20" if r["fused_rank"] and r["fused_rank"] <= 20
            else "21-30" if r["fused_rank"] and r["fused_rank"] <= 30 else "31-50" if r["fused_rank"] else "beyond 50"
            for r in group if r["stage"] == "not a candidate"
        )
        reasons = Counter(r["reason"] for r in group if r["stage"] == "not chosen")
        print(f"\n{language}: {len(group)} answerable: {dict(stages)}")
        print(f"  not a candidate, by fused rank: {dict(sorted(far.items()))}")
        for reason, count in reasons.most_common():
            print(f"  not chosen, {count}: {reason}")
    if arguments.output:
        arguments.output.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
