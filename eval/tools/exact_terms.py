"""Known-item test, generated from the index, so that no question is hand-picked. No LLM.

For each exact technical term, asks for it bare and inside a Turkish sentence, and checks whether
search and the passages return a line that holds it. It shows that a term can be found when someone
names it. It says nothing about questions that describe a term without naming it.

    python eval/tools/exact_terms.py <code folder> [--output table.json]
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("code", type=Path)
parser.add_argument("--output", type=Path)
arguments = parser.parse_args()

sys.path.insert(0, str(arguments.code))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("REPOMIND_MODELS_DIR", str(PROJECT / ".models"))

import repomind  # noqa: E402
from repomind.cli import _embedder, _store  # noqa: E402
from repomind.config import Settings  # noqa: E402
from repomind.qa.passages import select_passages  # noqa: E402
from repomind.search.engine import SearchConfig, SearchEngine  # noqa: E402
from repomind.search.translation import LocalTurkishTranslator  # noqa: E402

assert Path(repomind.__file__).parent.parent.resolve() == arguments.code.resolve(), repomind.__file__

SEATS = 5
TERMS_PER_KIND = 25
# A term in many chunks is not a known item: any hit would prove little.
MOST_CHUNKS_PER_TERM = 5
PATTERNS = {
    "config key with a hyphen": re.compile(r"^\s*([a-z][a-z0-9]*(?:-[a-z0-9]+)+):", re.M),
    "environment variable": re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b"),
    "HTTP status constant": re.compile(r"\b(HttpStatus\.[A-Za-z_]+)\b"),
    "header or hyphenated literal": re.compile(r"[\"'`]([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)[\"'`]"),
    "SQL constraint or index name": re.compile(r"\"?([a-z][a-z0-9_]*_(?:check|key|fkey|pkey|idx))\"?"),
}
FRAMES = {"bare": "{}", "turkish": "{} ne işe yarıyor?"}

settings = Settings.load()
with _store(settings) as store:
    embedder = _embedder(settings)
    engine = SearchEngine(store, embedder, SearchConfig(), LocalTurkishTranslator(settings.models_dir))
    has_seats = "glossary_seats" in inspect.signature(engine.search).parameters
    has_terms = "more_terms" in inspect.signature(select_passages).parameters
    shuffler = random.Random(20261106)
    items = []
    for snapshot in store.active_snapshots():
        chunks = store.load_chunks([snapshot.id], False)
        for kind, pattern in PATTERNS.items():
            where: Counter[str] = Counter()
            for chunk in chunks:
                where.update(set(pattern.findall(chunk.content)))
            terms = sorted(term for term, count in where.items() if count <= MOST_CHUNKS_PER_TERM)
            shuffler.shuffle(terms)
            items.extend((snapshot.repo, kind, term) for term in terms[:TERMS_PER_KIND])

    table: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for repo, kind, term in items:
        for frame_name, frame in FRAMES.items():
            query = frame.format(term)
            found = engine.search(query, "hybrid", [repo], 12, **({"glossary_seats": SEATS} if has_seats else {}))
            queries = [text for text in (query, found.translated_query) if text]
            more = {"more_terms": found.glossary_terms} if has_terms else {}
            passages = select_passages([hit.chunk for hit in found.hits], queries, embedder, **more)
            rank = next((i for i, hit in enumerate(found.hits, 1) if term in hit.chunk.content), None)
            row = table[(kind, frame_name)]
            row["n"] += 1
            row["top1"] += rank == 1
            row["top5"] += rank is not None and rank <= 5
            row["found"] += rank is not None
            row["shown"] += any(term in p.text for p in passages)

    print(f"{'kind':<32}{'query':<10}{'n':>4}{'top1':>6}{'top5':>6}{'found':>7}{'shown':>7}")
    all_rows: Counter = Counter()
    for (kind, frame), row in sorted(table.items()):
        all_rows.update(row)
        print(f"{kind:<32}{frame:<10}{row['n']:>4}{row['top1']:>6}{row['top5']:>6}{row['found']:>7}{row['shown']:>7}")
    print(f"{'all':<42}{all_rows['n']:>4}{all_rows['top1']:>6}{all_rows['top5']:>6}{all_rows['found']:>7}{all_rows['shown']:>7}")
    if arguments.output:
        rows = [{"kind": kind, "query": frame, **row} for (kind, frame), row in sorted(table.items())]
        arguments.output.write_text(json.dumps({"terms": len(items), "all": all_rows, "rows": rows}, indent=1) + "\n", encoding="utf-8")
