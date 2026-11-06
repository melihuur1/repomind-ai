"""Can the lines that answer a question be found without an LLM?

Takes the same 8 sources that `ask` reads, cuts them into 3-line windows, and ranks the windows
with the local embedding model and shared terms. A question counts as found when a gold anchor,
a text verified to be in the gold code, appears in a top window. Compared with the quotes that
the models chose. Both question sets have been read before, so this guides design only.
"""

import json
import re
import statistics
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from repomind.cli import _embedder, _store  # noqa: E402
from repomind.config import Settings  # noqa: E402
from repomind.evaluation.dataset import load_dataset, load_holdout_unanswerable  # noqa: E402
from repomind.search.engine import SearchConfig, SearchEngine  # noqa: E402
from repomind.search.hybrid import reciprocal_rank_fusion  # noqa: E402
from repomind.search.tokenizer import tokenize  # noqa: E402
from repomind.search.translation import LocalTurkishTranslator  # noqa: E402

RESULTS = ROOT / "eval" / "results"
SOURCES = 8
WINDOW = 3
STEP = 2


def squeeze(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def windows(content: str) -> list[str]:
    lines = content.split("\n")
    pieces = ("\n".join(lines[i : i + WINDOW]) for i in range(0, max(len(lines) - WINDOW + 1, 1), STEP))
    return [p for p in pieces if re.search(r"[^\W\d_]{3}", p)]


class Ranker:
    def __init__(self, embedder) -> None:
        self.embedder = embedder
        self.vectors: dict[str, np.ndarray] = {}

    def vector_of(self, texts: list[str]) -> np.ndarray:
        missing = [t for t in dict.fromkeys(texts) if t not in self.vectors]
        for start in range(0, len(missing), 64):
            part = missing[start : start + 64]
            for text, vector in zip(part, self.embedder.embed_passages(part)):
                self.vectors[text] = np.asarray(vector) / np.linalg.norm(vector)
        return np.stack([self.vectors[t] for t in texts])

    def rank(self, pieces: list[str], queries: list[str]) -> tuple[list[str], float, float]:
        """Returns the ordered windows, the best cosine, and the best share of question terms in one window."""
        matrix = self.vector_of(pieces)
        cosines = np.zeros(len(pieces))
        for query in queries:
            vector = np.asarray(self.embedder.embed_query(query))
            cosines = np.maximum(cosines, matrix @ (vector / np.linalg.norm(vector)))
        terms = {t for query in queries for t in tokenize(query)}
        tokens = [set(tokenize(p)) for p in pieces]
        frequency = {t: sum(t in s for s in tokens) for t in terms}
        rarity = {t: np.log(1 + len(pieces) / (1 + frequency[t])) for t in terms}
        lexical = [sum(rarity[t] for t in terms & s) for s in tokens]
        fused = reciprocal_rank_fusion(
            {
                "cosine": sorted(range(len(pieces)), key=lambda i: -cosines[i]),
                "terms": sorted(range(len(pieces)), key=lambda i: -lexical[i]),
            },
            k=60,
        )
        # Only the English wording: Turkish words of the question would never occur in the code.
        wanted = set(tokenize(queries[-1]))
        coverage = max((len(wanted & s) / max(len(wanted), 1) for s in tokens), default=0.0)
        return [pieces[item.key] for item in fused], float(cosines.max()) if len(pieces) else 0.0, coverage


def probe(engine: SearchEngine, ranker: Ranker, question, text: str, translated: str | None) -> dict:
    hits = engine.search(text, "hybrid", [question.repo], SOURCES, translated=translated).hits
    pieces = list(dict.fromkeys(p for hit in hits for p in windows(hit.chunk.content)))
    used = engine.search(text, "hybrid", [question.repo], 1, translated=translated).translated_query
    ordered, cosine, coverage = ranker.rank(pieces, [q for q in (text, used) if q])
    result = {"cosine": cosine, "coverage": coverage}
    anchors = [squeeze(span.anchor) for span in getattr(question, "gold", ())]
    if anchors:
        everything = squeeze("\n".join(hit.chunk.content for hit in hits))
        result["in_sources"] = any(a in everything for a in anchors)
        for k in (1, 3, 5):
            top = squeeze("\n".join(ordered[:k]))
            result[f"top{k}"] = any(a in top for a in anchors)
    return result


def report(name: str, rows: dict[str, dict], negatives: list[dict], quotes: dict[str, list[str]] | None, questions) -> None:
    n = len(rows)
    counts = {key: sum(r[key] for r in rows.values()) for key in ("in_sources", "top1", "top3", "top5")}
    line = f"{name}: {n} questions. Anchor in the 8 sources {counts['in_sources']}, top window {counts['top1']}, top 3 {counts['top3']}, top 5 {counts['top5']}"
    if quotes is not None:
        anchors = {q.id: [squeeze(s.anchor) for s in q.gold] for q in questions}
        chosen = sum(any(a in squeeze(q) for a in anchors[i] for q in quotes.get(i, [])) for i in rows)
        line += f" | in a quote the model chose: {chosen}"
    print(line)
    print("  not in the 8 sources:", ", ".join(i for i, r in rows.items() if not r["in_sources"]) or "none")
    print("  in the sources, missed by the top 3:", ", ".join(i for i, r in rows.items() if r["in_sources"] and not r["top3"]) or "none")
    if quotes is not None and name.startswith("Second"):
        grades = {
            g["question"]: g["grade"]
            for g in json.loads((RESULTS / "v2-grades-3b-tr.json").read_text(encoding="utf-8"))
            if g["run"] == "v2-ask-phase2-3b.json"
        }
        for grade in ("wrong", "partial"):
            mine = [i for i in rows if grades.get(i) == grade]
            print(f"  3B answers graded {grade}: {len(mine)}, with the anchor in the top 3 windows: {sum(rows[i]['top3'] for i in mine)}")
    positive = [r["coverage"] for r in rows.values()]
    negative = [r["coverage"] for r in negatives]
    print(f"  best window's share of question terms: answerable median {statistics.median(positive):.2f}, negative median {statistics.median(negative):.2f}")
    for threshold in (0.34, 0.5, 0.67):
        print(f"    refuse below {threshold}: answerable kept {sum(c >= threshold for c in positive)}/{len(positive)}, negatives refused {sum(c < threshold for c in negative)}/{len(negative)}")
    positive_cos = [r["cosine"] for r in rows.values()]
    negative_cos = [r["cosine"] for r in negatives]
    pairs = sum((p > m) + 0.5 * (p == m) for p in positive_cos for m in negative_cos) / max(len(positive_cos) * len(negative_cos), 1)
    print(f"  best cosine: answerable median {statistics.median(positive_cos):.3f}, negative median {statistics.median(negative_cos):.3f}, AUC {pairs:.2f}")


settings = Settings.load()
with _store(settings) as store:
    embedder = _embedder(settings)
    engine = SearchEngine(store, embedder, SearchConfig(), LocalTurkishTranslator(settings.models_dir))
    ranker = Ranker(embedder)

    second = load_dataset(ROOT / "eval" / "questions_v2.yaml")
    stored = json.loads((RESULTS / "v2-ask-phase2-3b.json").read_text(encoding="utf-8"))
    translated = {r["question"]: r["translated_query"] for r in stored["answerable"] + stored["unanswerable"]}
    quotes_3b = {
        r["question"]: [x["quote"] for x in r["readings"] if x["outcome"] == "supported"] for r in stored["answerable"]
    }
    first = load_dataset(ROOT / "eval" / "questions.yaml")
    first_negatives = (*first.unanswerable, *load_holdout_unanswerable())
    dev = json.loads((RESULTS / "dev-phase2-7b-tr.json").read_text(encoding="utf-8"))
    quotes_7b = {r["id"]: [x["quote"] for x in r["readings"] if x["outcome"] == "supported"] for r in dev if r["answerable"]}

    for language in ("tr", "en"):
        use = (lambda q: translated.get(q.id)) if language == "tr" else (lambda q: None)
        rows = {q.id: probe(engine, ranker, q, q.text(language), use(q)) for q in second.questions}
        negatives = [probe(engine, ranker, q, q.text(language), use(q)) for q in second.unanswerable]
        report(f"Second set, {language}", rows, negatives, quotes_3b if language == "tr" else None, second.questions)
    for language in ("tr", "en"):
        rows = {q.id: probe(engine, ranker, q, q.text(language), None) for q in first.questions}
        negatives = [probe(engine, ranker, q, q.text(language), None) for q in first_negatives]
        report(f"First set, {language}", rows, negatives, quotes_7b if language == "tr" else None, first.questions)
    print("windows embedded:", len(ranker.vectors))
