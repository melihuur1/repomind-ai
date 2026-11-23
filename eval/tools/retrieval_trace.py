"""Where the right code goes in search, list by list, for a few questions. No LLM.

    python eval/tools/retrieval_trace.py v5-sg-root-redirect v5-sn-migrate-first ...

For each question it searches the way `ask` does, with the stored 7B translation and the local
translator's wording, and again with the set's own English wording in place of the 7B one. That
separates what translation loses from what the words of the question never share with the code.

For every chunk holding accepted lines it prints its rank in the keyword list, in each semantic
list, after fusion, and among the passages, with the full depth of each list rather than the 50
the search keeps, and which question terms the chunk does and does not contain.

Development data: these questions have been read. Nothing is written to the index.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from accuracy_funnel import CHUNKS, SEATS, question_sets, stored_translations  # noqa: E402

from repomind.config import Settings  # noqa: E402
from repomind.embeddings.provider import FastEmbedProvider  # noqa: E402
from repomind.evaluation.metrics import overlaps, touches  # noqa: E402
from repomind.qa.passages import select_passages  # noqa: E402
from repomind.search.engine import SearchConfig, SearchEngine  # noqa: E402
from repomind.search.hybrid import reciprocal_rank_fusion  # noqa: E402
from repomind.search.tokenizer import tokenize  # noqa: E402
from repomind.search.translation import LocalTurkishTranslator  # noqa: E402
from repomind.store.database import Store  # noqa: E402


def rank_of(ids: list[int], wanted: int) -> int | None:
    return ids.index(wanted) + 1 if wanted in ids else None


def trace(engine: SearchEngine, embedder, question, asked: str, english: str | None, second: str | None) -> dict:
    ids = engine._snapshot_ids([question.repo])
    include_tests = engine._tests_included(asked, english)
    all_chunks = engine.store.load_chunks(ids, True)
    gold = [c for c in all_chunks if any(overlaps(c, s) for s in question.accepted)]
    wordings = list(dict.fromkeys(w for w in (english, second) if w))
    everything = len(all_chunks)

    keyword = engine._keyword_hits(asked, wordings, ids, include_tests, everything)
    semantic = {"semantic (asked)": engine._semantic_hits(asked, ids, include_tests, everything)}
    for number, wording in enumerate(wordings, start=1):
        semantic[f"semantic ({'translation' if number == 1 else 'second wording'})"] = engine._semantic_hits(wording, ids, include_tests, everything)
    lists = {"keyword": keyword, **semantic}
    depth = engine.config.candidates
    fused = reciprocal_rank_fusion({name: [h.chunk.id for h in hits[:depth]] for name, hits in lists.items()}, k=engine.config.rrf_k)
    fused_ids = [item.key for item in fused]

    found = engine.search(asked, "hybrid", [question.repo], CHUNKS, translated=english, more_wordings=[second] if second else (), glossary_seats=SEATS)
    candidate_ids = [hit.chunk.id for hit in found.hits]
    queries = [text for text in (asked, found.translated_query, second) if text]
    passages = select_passages([h.chunk for h in found.hits], queries, embedder, more_terms=found.glossary_terms)

    terms = list(dict.fromkeys(tokenize(" ".join([*wordings, asked]))))
    keyword_by_id = {h.chunk.id: h for h in keyword}
    rows = []
    for chunk in gold:
        body = set(tokenize(" ".join([chunk.content, chunk.symbol or "", *chunk.symbols, Path(chunk.path).stem])))
        hit = keyword_by_id.get(chunk.id)
        rows.append({
            "chunk": f"{chunk.path}:{chunk.start_line}-{chunk.end_line}",
            "kind": chunk.kind,
            "symbol": chunk.symbol,
            "lines": chunk.end_line - chunk.start_line + 1,
            "is_test": chunk.is_test,
            "keyword_rank": rank_of([h.chunk.id for h in keyword], chunk.id),
            "keyword_matched": hit.explanation["matched_terms"] if hit else {},
            **{f"{name}_rank": rank_of([h.chunk.id for h in hits], chunk.id) for name, hits in semantic.items()},
            **{f"{name}_similarity": next((h.score for h in hits if h.chunk.id == chunk.id), None) for name, hits in semantic.items()},
            "fused_rank": rank_of(fused_ids, chunk.id),
            "candidate_rank": rank_of(candidate_ids, chunk.id),
            "question_terms_in_chunk": [t for t in terms if t in body],
            "question_terms_missing": [t for t in terms if t not in body],
        })
    return {
        "asked": asked,
        "translation": english,
        "second_wording": second,
        "chunks_searched": everything,
        "glossary_terms": found.glossary_terms,
        "top_semantic_similarity": {name: round(hits[0].score, 4) if hits else None for name, hits in semantic.items()},
        "candidates": [f"{h.chunk.path}:{h.chunk.start_line}-{h.chunk.end_line}" for h in found.hits],
        "passages": [f"{p.path}:{p.start_line}-{p.end_line}" for p in passages],
        "right_lines_in_passages": any(touches(p, s) for p in passages for s in question.accepted),
        "gold_chunks": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="+")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.output and arguments.output.exists():
        parser.error(f"{arguments.output} already exists, and is not overwritten.")
    questions = {q.id: q for answerable, _negatives in question_sets().values() for q in answerable}
    translations = stored_translations()
    settings = Settings.load()
    embedder = FastEmbedProvider(settings.embedding_model, settings.models_dir)
    report = {}
    with Store(settings.database_url, apply_schema=False) as store:
        engine = SearchEngine(store, embedder, SearchConfig(), LocalTurkishTranslator(settings.models_dir))
        for identifier in arguments.ids:
            question = questions[identifier]
            second = engine.local_translation(question.tr, [])
            report[identifier] = {
                "as_asked": trace(engine, embedder, question, question.tr, translations.get(identifier), second),
                "with_the_set_english": trace(engine, embedder, question, question.tr, question.en, second),
                "english_question": trace(engine, embedder, question, question.en, None, None),
            }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if arguments.output:
        arguments.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
