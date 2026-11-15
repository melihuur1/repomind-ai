"""How far the places an answer needs get before any model, for every question of a set. No LLM.

    python eval/tools/stage_trace.py --questions eval/questions_v6.yaml \
        --translations eval/results/v6-translations.json [--database-url-file F] [--output rows.json]

Each question goes through the real `ask`: search, the refusal check, passage selection, completion
and the prompt. A stand-in replaces the model. It gives the stored wording when asked to translate
and chooses nothing when asked to choose, so nothing is loaded and nothing is random. The trace
then says, for each place, the first stage it did not pass: not in the index, not retrieved, not
made a passage, refused before the model, or sent to the model. What the model would do with what
it is sent is not measured here.

The question file is in the form of eval/questions_v6.yaml. Those questions have been read, so
what this prints about them is development data and proves nothing independent.
It never writes to the index.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8")

from repomind.config import Settings  # noqa: E402
from repomind.embeddings.provider import FastEmbedProvider  # noqa: E402
from repomind.qa import trace as stages  # noqa: E402
from repomind.qa.engine import QAEngine  # noqa: E402
from repomind.qa.llm import LLMConfig, LLMResponse  # noqa: E402
from repomind.qa.prompt import TRANSLATION_PROMPT  # noqa: E402
from repomind.search.engine import SearchConfig, SearchEngine  # noqa: E402
from repomind.search.translation import LocalTurkishTranslator  # noqa: E402
from repomind.store.database import Store  # noqa: E402

SENT = "sent to the model"
ORDER = (stages.NOT_INDEXED, stages.NOT_RETRIEVED, stages.NOT_A_PASSAGE, stages.NOT_ASKED, stages.NOT_SENT, SENT)


class StandIn:
    """The stored wording for a translation, and "nothing chosen" for a choice."""

    def __init__(self) -> None:
        self.english: str | None = None

    def complete(self, system, user, max_output_tokens):
        if system == TRANSLATION_PROMPT:
            return LLMResponse(json.dumps({"english": self.english}) if self.english else "", None, None)
        return LLMResponse('{"chosen": []}', None, None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--translations", type=Path, help="a JSON file with {\"translations\": {id: english}}")
    parser.add_argument("--database-url-file", type=Path, help="a file holding the URL of the index to read, instead of the main one")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    data = yaml.safe_load(arguments.questions.read_text(encoding="utf-8"))
    stored = json.loads(arguments.translations.read_text(encoding="utf-8"))["translations"] if arguments.translations else {}
    settings = Settings.load()
    url = arguments.database_url_file.read_text(encoding="utf-8").strip() if arguments.database_url_file else settings.database_url
    client = StandIn()
    config = LLMConfig("http://127.0.0.1:1/v1", "stand-in")
    rows = []
    with Store(url, apply_schema=False) as store:
        store.record_llm_usage = lambda *a, **k: None
        snapshots = {s.repo: s for s in store.active_snapshots()}
        search = SearchEngine(store, FastEmbedProvider(settings.embedding_model, settings.models_dir), SearchConfig(include_tests=None),
                              LocalTurkishTranslator(settings.models_dir))
        for question in data["questions"]:
            client.english = stored.get(question["id"])
            result = QAEngine(store, search, config, client).ask(question["question"], [question["repo"]])
            snapshot = snapshots[question["repo"]]
            places = [[stages.Lines(a["path"], a["start_line"], a["end_line"]) for a in place["accepted"]] for place in question["places"]]
            traced = stages.trace(result, places, lambda path: store.file_chunks(snapshot.id, path))
            got = [SENT if item.stage == stages.NOT_CHOSEN else item.stage for item in traced]
            first_lost = min(got, key=ORDER.index)
            rows.append({
                "id": question["id"], "language": question["language"], "kind": question["kind"], "places": got, "question_stage": first_lost,
                "ranks": [item.retrieval_rank for item in traced], "stopped_by": result.stopped_by, "translated_query": result.translated_query,
                "passages": [f"{p.path}:{p.start_line}-{p.end_line}" for p in result.passages],
                "passage_lines": sum(p.end_line - p.start_line + 1 for p in result.passages), "prompt_tokens": result.input_tokens if result.sent else None,
                "warnings": [w for w in result.warnings if "Not found" in w],
            })
        negatives = []
        for question in data.get("unanswerable", []):
            client.english = stored.get(question["id"])
            result = QAEngine(store, search, config, client).ask(question["question"], [question["repo"]])
            negatives.append({"id": question["id"], "language": question["language"], "kind": question["kind"], "reaches_the_model": bool(result.sent), "stopped_by": result.stopped_by})

    for language, label in (("tr", "Turkish"), ("en", "English")):
        mine = [r for r in rows if r["language"] == language]
        counts = Counter(r["question_stage"] for r in mine)
        print(f"--- {label}: {len(mine)} answerable questions, by the first stage a needed place did not pass")
        for stage in ORDER:
            if counts[stage]:
                names = ", ".join(r["id"].removeprefix("v6-") for r in mine if r["question_stage"] == stage)
                print(f"   {counts[stage]:>2}  {stage}" + ("" if stage == SENT else f": {names}"))
        stopped = Counter(r["stopped_by"] for r in mine if r["question_stage"] == stages.NOT_ASKED)
        for reason, count in stopped.items():
            print(f"         refused because {reason}: {count}")
        mine_negative = [n for n in negatives if n["language"] == language]
        if mine_negative:
            reaching = [n["id"].removeprefix("v6-") for n in mine_negative if n["reaches_the_model"]]
            print(f"       unanswerable: {len(mine_negative)}, reaching the model {len(reaching)}: {', '.join(reaching)}")
    print(f"every needed place sent to the model: {sum(r['question_stage'] == SENT for r in rows)} of {len(rows)} answerable; "
          f"unanswerable reaching the model: {sum(n['reaches_the_model'] for n in negatives)} of {len(negatives)}")
    asked = [r["prompt_tokens"] for r in rows if r["prompt_tokens"]]
    if asked:
        print(f"prompts for answerable questions: about {round(sum(asked) / len(asked))} tokens on average, {max(asked)} at most; "
              f"{round(sum(r['passage_lines'] for r in rows) / len(rows), 1)} lines of code per question")
    if arguments.output:
        arguments.output.write_text(json.dumps({"questions": rows, "unanswerable": negatives}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
