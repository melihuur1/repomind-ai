"""How often does the no-LLM passage selection reach the lines that answer a question?

Takes the same 8 sources that `ask` reads and runs `select_passages` on them. A question counts
as found when a gold anchor, a text verified to be in the gold code, is inside a selected passage.
Only the two earlier question sets are used, because the pilot set must stay unseen until its run.
Both have been read before, so this guides design only.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from repomind.cli import _embedder, _store  # noqa: E402
from repomind.config import Settings  # noqa: E402
from repomind.evaluation.dataset import load_dataset, load_holdout_unanswerable  # noqa: E402
from repomind.qa import passages as passages_module  # noqa: E402
from repomind.qa.passages import select_passages  # noqa: E402
from repomind.search.engine import SearchConfig, SearchEngine  # noqa: E402
from repomind.search.translation import LocalTurkishTranslator  # noqa: E402

RESULTS = ROOT / "eval" / "results"
SOURCES = 8


def squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


class CachingEmbedder:
    """The questions share most of their chunks, so this avoids embedding the same lines again."""

    def __init__(self, embedder) -> None:
        self.embedder = embedder
        self.cache: dict[str, object] = {}

    def embed_passages(self, texts):
        missing = [t for t in dict.fromkeys(texts) if t not in self.cache]
        if missing:
            self.cache.update(zip(missing, self.embedder.embed_passages(missing)))
        return [self.cache[t] for t in texts]

    def embed_query(self, text):
        return self.embedder.embed_query(text)


def probe(engine, embedder, question, text, translated):
    found = engine.search(text, "hybrid", [question.repo], SOURCES, translated=translated)
    queries = [q for q in (text, found.translated_query) if q]
    passages = select_passages([hit.chunk for hit in found.hits], queries, embedder, limit=5)
    anchors = [squeeze(span.anchor) for span in getattr(question, "gold", ())]
    row = {"passages": len(passages), "lines": sum(p.end_line - p.start_line + 1 for p in passages)}
    if anchors:
        row["in_sources"] = any(a in squeeze("\n".join(hit.chunk.content for hit in found.hits)) for a in anchors)
        for k in (1, 3, 5):
            row[f"top{k}"] = any(a in squeeze("\n".join(p.text for p in passages[:k])) for a in anchors)
    return row


def report(name, rows, negatives):
    n = len(rows)
    print(
        f"{name}: {n} questions. In the 8 sources {sum(r['in_sources'] for r in rows.values())}, "
        f"top passage {sum(r['top1'] for r in rows.values())}, top 3 {sum(r['top3'] for r in rows.values())}, "
        f"top 5 {sum(r['top5'] for r in rows.values())}; mean lines shown {sum(r['lines'] for r in rows.values()) / n:.1f}; "
        f"questions with no passage: answerable {sum(r['passages'] == 0 for r in rows.values())}, "
        f"negative {sum(r['passages'] == 0 for r in negatives)}/{len(negatives)}"
    )


settings = Settings.load()
with _store(settings) as store:
    embedder = CachingEmbedder(_embedder(settings))
    engine = SearchEngine(store, embedder.embedder, SearchConfig(), LocalTurkishTranslator(settings.models_dir))
    second = load_dataset(ROOT / "eval" / "questions_v2.yaml")
    stored = json.loads((RESULTS / "v2-ask-phase2-3b.json").read_text(encoding="utf-8"))
    translated = {r["question"]: r["translated_query"] for r in stored["answerable"] + stored["unanswerable"]}
    first = load_dataset(ROOT / "eval" / "questions.yaml")
    first_negatives = (*first.unanswerable, *load_holdout_unanswerable())
    for longest in map(int, sys.argv[1:]) if len(sys.argv) > 1 else [passages_module.MAX_PASSAGE_LINES]:
        passages_module.MAX_PASSAGE_LINES = longest
        print(f"--- passages of at most {longest} lines")
        for language in ("tr", "en"):
            use = (lambda q: translated.get(q.id)) if language == "tr" else (lambda q: None)
            rows = {q.id: probe(engine, embedder, q, q.text(language), use(q)) for q in second.questions}
            negatives = [probe(engine, embedder, q, q.text(language), use(q)) for q in second.unanswerable]
            report(f"Second set, {language}", rows, negatives)
        for language in ("tr", "en"):
            rows = {q.id: probe(engine, embedder, q, q.text(language), None) for q in first.questions}
            negatives = [probe(engine, embedder, q, q.text(language), None) for q in first_negatives]
            report(f"First set, {language}", rows, negatives)
