"""Before sending code to a remote model: would any prompt carry what the index holds as a credential?

Runs questions through the real engine with a stand-in for the model, which keeps every prompt and chooses
nothing. No model is asked and nothing leaves the machine. The database session is read-only, so the index
cannot be changed, and the engine's usage record is left out. Prints counts, paths and rule names, never a
line of code.

    python eval/tools/prompt_check.py --database repomind --repo stockguard --repo syncnotes
    python eval/tools/prompt_check.py --database repomind_openai_trial --repo trial --questions my-questions.yaml

Each prompt is checked twice: for every line the index holds that the credential rules flag, and with the
rules of each passage's own language. Lines that only another language's rules flag are listed apart, to
be looked at: they are usually a name such as `secret: config.jwtSecret`, and not a value.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from repomind.config import Settings  # noqa: E402
from repomind.embeddings.provider import FastEmbedProvider  # noqa: E402
from repomind.ingest.filters import LANGUAGE_BY_EXTENSION, find_credential  # noqa: E402
from repomind.qa.engine import QAEngine  # noqa: E402
from repomind.qa.llm import LLMConfig, LLMResponse  # noqa: E402
from repomind.search.engine import SearchEngine  # noqa: E402
from repomind.search.translation import LocalTurkishTranslator  # noqa: E402
from repomind.store.database import Store  # noqa: E402

# Questions that ask for secrets outright, for any repository.
ASKING_FOR_SECRETS = [
    "What is the JWT secret?",
    "Which password does the database use?",
    "What password do the test users log in with?",
    "Where is the API token configured?",
    "What is the database connection URL?",
    "Which credentials do the tests use?",
    "Where are secrets read from the environment?",
    "Which API key does the application use?",
    "Veritabanı şifresi nedir?",
    "Test kullanıcılarının parolası ne?",
]
_PASSAGE = re.compile(r'<passage-\w+ id="[^"]*" path="([^"]+)" lines="(\d+)-(\d+)"[^>]*>\n(.*?)\n</passage-\w+>', re.S)


class _NoUsageRecord:
    """The store, without the one write the engine makes."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def record_llm_usage(self, *args, **kwargs) -> None:
        return None


class _Recording:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, max_output_tokens: int) -> LLMResponse:
        self.prompts.append(user)
        # No translation, so the local translator is used, and no choice, so nothing is accepted.
        return LLMResponse('{"chosen": []}' if "<passage" in user else "{}", 1, 1)


def _questions(path: str | None, repos: list[str]) -> list[tuple[str, str]]:
    asked = [(repo, question) for repo in repos for question in ASKING_FOR_SECRETS]
    if path:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        entries = [entry for value in (data.values() if isinstance(data, dict) else [data]) if isinstance(value, list) for entry in value]
        asked += [(entry["repo"], entry["question"]) for entry in entries if isinstance(entry, dict) and entry.get("repo") in repos]
    return asked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default="repomind", help="a database in RepoMind's container")
    parser.add_argument("--repo", action="append", required=True)
    parser.add_argument("--questions", help="a question file with repo and question fields, added to the ones that ask for secrets")
    args = parser.parse_args()

    settings = Settings.load()
    store = Store(settings.database_url.rsplit("/", 1)[0] + "/" + args.database, apply_schema=False)
    store.connection.execute("SET default_transaction_read_only = on")
    stored: dict[str, set[str]] = {}
    for snapshot in store.active_snapshots(args.repo):
        for chunk in store.load_chunks([snapshot.id]):
            for line in chunk.content.splitlines():
                if len(line.strip()) > 8 and find_credential(line, chunk.language):
                    stored.setdefault(snapshot.repo, set()).add(line.strip())
        print(f"{snapshot.repo} @ {snapshot.commit_sha[:12]}: index version {snapshot.pipeline_version}, "
              f"{len(stored.get(snapshot.repo, ()))} stored lines flagged as credentials")

    embedder = FastEmbedProvider(settings.embedding_model, settings.models_dir)
    translator = LocalTurkishTranslator(settings.models_dir)
    safe = _NoUsageRecord(store)
    config = LLMConfig("http://127.0.0.1:1/v1", "stand-in")
    questions = _questions(args.questions, args.repo)
    prompts = carrying = 0
    by_own_rules: list[str] = []
    to_look_at: list[str] = []
    for number, (repo, question) in enumerate(questions, 1):
        client = _Recording()
        QAEngine(safe, SearchEngine(safe, embedder, translator=translator), config, client).ask(question, [repo])
        for prompt in client.prompts:
            prompts += 1
            if any(line in prompt for line in stored.get(repo, ())):
                carrying += 1
            for path, start, end, text in _PASSAGE.findall(prompt):
                language = LANGUAGE_BY_EXTENSION.get(Path(path).suffix.lower())
                place = f"question {number}, {repo}: {path}:{start}-{end}"
                if find_credential(text, language):
                    by_own_rules.append(f"{place}, rule {find_credential(text, language)!r}")
                elif find_credential(text, None):
                    to_look_at.append(f"{place}, matched only by another language's rule {find_credential(text, None)!r}")
    store.close()

    print(f"questions: {len(questions)}, prompts: {prompts}")
    print(f"prompts carrying a stored credential line: {carrying}")
    print(f"passages flagged by their own language's rules: {len(by_own_rules)}")
    for line in by_own_rules:
        print(f"  {line}")
    print(f"passages to look at: {len(to_look_at)}")
    for line in to_look_at:
        print(f"  {line}")
    return 1 if carrying or by_own_rules else 0


if __name__ == "__main__":
    raise SystemExit(main())
