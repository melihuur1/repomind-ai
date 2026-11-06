"""Command line interface. Every failure ends in a readable message, not a traceback."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from repomind.config import PROJECT_ROOT, Settings
from repomind.errors import RepoMindError
from repomind.search.engine import MODES, SearchConfig, SearchEngine

_CONTROL_CHARACTERS = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def safe_text(text: str) -> str:
    """Repository content and model output are untrusted: they must not be able to
    drive the terminal with escape sequences."""
    return _CONTROL_CHARACTERS.sub("", text)


def _emit(text: str = "") -> None:
    print(text, flush=True)


def _progress(done: int, total: int) -> None:
    print(f"\r  embedding {done}/{total}", end="" if done < total else "\n", file=sys.stderr, flush=True)


def _question_progress(done: int, total: int) -> None:
    print(f"\r  question {done}/{total}", end="" if done < total else "\n", file=sys.stderr, flush=True)


def _output_path(name: str) -> Path:
    output = Path(name)
    return output if output.is_absolute() else PROJECT_ROOT / output


def _check_can_write(output: Path, overwrite: bool) -> None:
    """A finished result is evidence, so replacing one has to be asked for. An unfinished one may be continued."""
    if overwrite or not output.exists():
        return
    try:
        complete = json.loads(output.read_text(encoding="utf-8")).get("run", {}).get("complete", True)
    except (OSError, ValueError, AttributeError):
        complete = True
    if complete:
        raise RepoMindError(
            f"{output} already holds a finished result, and it will not be replaced.",
            hint="Choose another --output, or pass --overwrite if replacing it is really intended.",
        )


def _write_json(output: Path, results: dict) -> None:
    """Goes through a temporary file, so that a crash cannot leave half a result under the real name."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, output)


def _store(settings: Settings):
    from repomind.store.database import Store

    return Store(settings.database_url)


def _embedder(settings: Settings, model: str | None = None):
    from repomind.embeddings.provider import FastEmbedProvider

    return FastEmbedProvider(model or settings.embedding_model, settings.models_dir)


def command_index(args: argparse.Namespace, settings: Settings) -> int:
    from repomind.indexing import index_repository

    embedder = None if args.no_embed else _embedder(settings, args.model)
    with _store(settings) as store:
        report = index_repository(store, args.path, embedder, args.name, args.ref, args.force, _progress)
    state = "already indexed, nothing re-parsed" if report.unchanged else "indexed"
    _emit(f"{report.repo} @ {report.commit[:12]}: {state} ({report.seconds:.1f}s)")
    _emit(f"  commit      {safe_text(report.subject)}")
    _emit(f"  files       {report.files_indexed} indexed of {report.files_in_commit} in the commit")
    _emit(f"  languages   {report.languages}")
    _emit(f"  chunks      {report.chunks}")
    _emit(f"  symbols     {report.symbols}")
    _emit(f"  imports     {report.imports}")
    _emit(f"  skipped     {report.skipped}")
    if report.skipped_for_secrets:
        _emit(f"  WARNING     skipped because they appear to contain secrets: {report.skipped_for_secrets}")
    if report.config_chunks_withheld:
        _emit(f"  withheld    configuration chunks holding a credential: {report.config_chunks_withheld}")
    if report.files_with_syntax_errors:
        _emit(f"  syntax      {len(report.files_with_syntax_errors)} file(s) with parse errors were kept: {report.files_with_syntax_errors}")
    if report.embedding:
        _emit(f"  embeddings  {report.embedding.computed} computed, {report.embedding.reused} reused ({report.embedding.model})")
    else:
        _emit("  embeddings  skipped (--no-embed); only keyword search is available")
    return 0


def command_embed(args: argparse.Namespace, settings: Settings) -> int:
    from repomind.indexing import embed_missing

    with _store(settings) as store:
        snapshots = store.active_snapshots(args.repo)
        if not snapshots:
            raise RepoMindError("Nothing to embed: no repository is indexed.")
        report = embed_missing(store, _embedder(settings, args.model), [s.id for s in snapshots], _progress)
    _emit(f"{report.model}: {report.computed} computed, {report.reused} reused")
    return 0


def command_repos(_args: argparse.Namespace, settings: Settings) -> int:
    with _store(settings) as store:
        snapshots = store.active_snapshots()
        if not snapshots:
            _emit("No repositories indexed.")
            return 0
        for snapshot in snapshots:
            counts = store.snapshot_counts(snapshot.id)
            embedded, total = store.count_embedded_chunks(settings.embedding_model, [snapshot.id])
            _emit(f"{snapshot.repo} @ {snapshot.commit_sha}")
            _emit(f"  commit     {safe_text(snapshot.commit_subject)} ({snapshot.committed_at:%Y-%m-%d})")
            _emit(f"  source     {snapshot.source_path}")
            _emit(f"  files      {counts['files']}   chunks {counts['chunks']} ({snapshot.stats['test_chunks']} in tests)")
            _emit(f"  symbols    {counts['symbols']}   imports {counts['imports']}")
            _emit(f"  languages  {snapshot.stats['languages']}")
            _emit(f"  embeddings {embedded}/{total} with {settings.embedding_model}")
    return 0


def _explanation(hit) -> str:
    details = hit.explanation
    parts = []
    if "ranks" in details:
        parts.append("ranks " + ", ".join(f"{name} #{rank}" for name, rank in details["ranks"].items()))
    if "cosine_similarity" in details:
        parts.append(f"cosine {details['cosine_similarity']}")
    if "matched_terms" in details:
        parts.append("terms " + ", ".join(sorted(details["matched_terms"], key=details["matched_terms"].get, reverse=True)))
    return "; ".join(parts)


def print_hits(hits, explain: bool = False, preview_lines: int = 3) -> None:
    for hit in hits:
        chunk = hit.chunk
        label = f"{chunk.kind} {chunk.symbol}" if chunk.symbol else chunk.kind
        part = f" part {chunk.part_index}/{chunk.part_total}" if chunk.part_total > 1 else ""
        test = " [test]" if chunk.is_test else ""
        _emit(f"{hit.rank:>2}. {chunk.path}:{chunk.start_line}-{chunk.end_line}  ({safe_text(label)}{part}){test}")
        _emit(f"    {chunk.repo}@{chunk.commit_sha[:12]}  score {hit.score:.4f}")
        if explain:
            _emit(f"    why: {safe_text(_explanation(hit))}")
        for line in chunk.content.split("\n")[:preview_lines]:
            _emit(f"    | {safe_text(line)[:110]}")


def _translator(settings: Settings, args: argparse.Namespace):
    if not args.translate:
        return None
    from repomind.search.translation import LocalTurkishTranslator

    return LocalTurkishTranslator(settings.models_dir)


def command_search(args: argparse.Namespace, settings: Settings) -> int:
    embedder = None if args.mode == "keyword" else _embedder(settings, args.model)
    with _store(settings) as store:
        engine = SearchEngine(store, embedder, SearchConfig(include_tests=args.include_tests), _translator(settings, args))
        result = engine.search(args.query, args.mode, args.repo, args.limit)
        for warning in result.warnings:
            _emit(f"warning: {warning}")
        if result.translated_query:
            _emit(f"translated query: {safe_text(result.translated_query)}")
        _emit(f"{len(result.hits)} result(s), method: {result.mode}")
        if not result.hits:
            _emit("No matching code found.")
        print_hits(result.hits, args.explain)
    return 0


def command_evaluate(args: argparse.Namespace, settings: Settings) -> int:
    from repomind.evaluation.dataset import load_dataset
    from repomind.evaluation.runner import format_summary, run_evaluation

    output = _output_path(args.output) if args.output else None
    if output:
        _check_can_write(output, args.overwrite)
    dataset = load_dataset(args.dataset)
    modes = tuple(args.modes)
    embedder = _embedder(settings, args.model) if set(modes) & {"semantic", "hybrid"} else None
    config = SearchConfig(include_tests=args.include_tests, rrf_k=args.rrf_k)
    with _store(settings) as store:
        engine = SearchEngine(store, embedder, config, _translator(settings, args))
        results = run_evaluation(engine, store, dataset, modes=modes, languages=tuple(args.languages))
    _emit(format_summary(results, args.split))
    comparisons = {k: v for k, v in results["comparisons"].items() if k.startswith(f"{args.split}/")}
    if comparisons:
        _emit("")
        _emit("Paired hybrid comparisons (95% bootstrap CI of the mean difference):")
        for name, value in comparisons.items():
            verdict = "difference" if value["significant"] else "no clear difference"
            _emit(f"  {name:<52} {value['mean_difference']:+.3f} [{value['ci95'][0]:+.3f}, {value['ci95'][1]:+.3f}]  {verdict}")
    if output:
        _write_json(output, results)
        _emit(f"\nFull results: {output}")
    invalid = sum(
        s["citations_checked"] - s["citations_valid"]
        for language in results["summaries"].get("all", {}).values()
        for s in language.values()
    )
    if invalid:
        _emit(f"\nERROR: invalid citations were returned ({invalid} counted across the summaries above).")
        return 1
    return 0


def _qa_engine(store, settings: Settings, args: argparse.Namespace):
    from repomind.qa.engine import QAEngine
    from repomind.qa.llm import OpenAICompatibleClient, load_llm_config

    config = None if args.no_llm else load_llm_config()
    client = OpenAICompatibleClient(config) if config else None
    search = SearchEngine(store, _embedder(settings, args.model), SearchConfig(), _translator(settings, args))
    return QAEngine(store, search, config, client)


_STATUS_MESSAGES = {
    "no_llm": (
        "This is not an AI answer: no LLM was used. These are the lines closest to the question, best first.\n"
        "They may or may not answer it, so read them. To add a model, set REPOMIND_LLM_BASE_URL and REPOMIND_LLM_MODEL (see README)."
    ),
    "no_results": "I do not know: no indexed code matches this question.",
    "insufficient": "I do not know: the indexed code does not give enough evidence to answer this.",
    "invalid_response": "I do not know: the model did not return a valid response, so none of it is shown.",
}


_REVIEW_HEADINGS = {
    "tr": (
        "İncelenebilecek kaynak adayı: model bu satırları seçti, ancak kanıt kuralı kabul etmedi.\n"
        "Satırlar bu commit'te gerçekten var, ama soruyu cevapladıkları doğrulanmadı. Bu bir cevap değildir."
    ),
    "en": (
        "Source to review: the model chose these lines, but the evidence rule did not accept them.\n"
        "They do exist at this commit, but nothing confirms that they answer the question. This is not an answer."
    ),
}


def _print_passage(passage) -> None:
    chunk = passage.chunk
    _emit(f"{chunk.path}:{passage.start_line}-{passage.end_line}  ({chunk.repo}@{chunk.commit_sha[:12]})")
    for line in passage.text.split("\n"):
        _emit(f"    | {safe_text(line)}")


def _print_review_candidates(result) -> None:
    from repomind.search.translation import looks_turkish

    if not result.review_candidates:
        return
    _emit("")
    _emit(_REVIEW_HEADINGS["tr" if looks_turkish(result.question) else "en"])
    for passage in result.review_candidates:
        _emit("")
        _print_passage(passage)


def command_ask(args: argparse.Namespace, settings: Settings) -> int:
    with _store(settings) as store:
        result = _qa_engine(store, settings, args).ask(args.question, args.repo)
    if result.translated_query:
        _emit(f"translated query: {safe_text(result.translated_query)}")
    for warning in result.warnings:
        _emit(f"warning: {safe_text(warning)}")
    if result.status == "answered":
        _emit("The model chose these lines as the answer:")
        for item in result.evidence:
            _emit("")
            _print_passage(item.passage)
        if result.note:
            _emit("")
            _emit(f"Model's reading, not verified: {safe_text(result.note)}")
        if result.note_problem:
            _emit("")
            _emit(f"The model's sentence was left out: {safe_text(result.note_problem)}")
        _emit("")
        _emit("Each line above is checked against Git. That they answer the question is the model's judgement, and a citation alone does not prove an answer.")
        _print_review_candidates(result)
    else:
        _emit(_STATUS_MESSAGES[result.status])
        _print_review_candidates(result)
        under_review = {passage.id for passage in result.review_candidates}
        closest = [passage for passage in result.passages if passage.id not in under_review]
        if closest:
            if result.status != "no_llm":
                _emit("")
                _emit("Closest lines found, which are not an answer:")
            for passage in closest:
                _emit("")
                _print_passage(passage)
        elif result.hits and not result.review_candidates:
            _emit("")
            _emit("Closest code found, which is not an answer:")
            print_hits(result.hits, preview_lines=0)
    for rejection in result.rejected:
        _emit(f"discarded: {safe_text(rejection)}")
    if result.model:
        approximate = " (estimated)" if result.tokens_are_estimated else ""
        _emit("")
        _emit(
            f"model {result.model}: {result.input_tokens} input + {result.output_tokens} output tokens{approximate}, "
            f"${result.cost_usd:.6f}; {result.sources_sent} passages read, "
            f"{result.sources_dropped} left out by the limits"
        )
    return 0


def _memory_guard(minimum_gb: float):
    """Stops a run between questions, while it can still save its work and leave the machine usable."""
    from repomind.evaluation.memory import available_memory_gb

    def should_stop() -> str | None:
        free = available_memory_gb()
        if free is not None and free < minimum_gb:
            return f"free memory is {free:.1f} GB, below the limit of {minimum_gb:.1f} GB"
        return None

    return should_stop


def command_evaluate_ask(args: argparse.Namespace, settings: Settings) -> int:
    from repomind.evaluation.ask_runner import Checkpoint, run_ask_evaluation, run_identity
    from repomind.evaluation.dataset import DEFAULT_DATASET_PATH, load_dataset, load_holdout_unanswerable
    from repomind.qa.llm import load_llm_config

    if not args.no_llm and load_llm_config() is None:
        raise RepoMindError(
            "No LLM is configured, so there are no answers to evaluate.",
            hint="Set REPOMIND_LLM_BASE_URL and REPOMIND_LLM_MODEL (see README).",
        )
    output = _output_path(args.output)
    _check_can_write(output, args.overwrite)
    checkpoint_path = output.with_name(output.name + ".checkpoint.jsonl")
    if args.overwrite:
        checkpoint_path.unlink(missing_ok=True)
    dataset = load_dataset(Path(args.dataset) if args.dataset else DEFAULT_DATASET_PATH)
    # The old negatives held back from phase 1 belong only with the set they were written for.
    holdout = () if args.dataset else load_holdout_unanswerable()
    should_stop = _memory_guard(args.min_free_memory_gb)
    # Before any model is loaded, because loading is when memory runs out fastest.
    reason = should_stop()
    if reason:
        raise RepoMindError(f"Not starting: {reason}.", hint="Close other programs, or set --min-free-memory-gb.")
    with _store(settings) as store:
        qa = _qa_engine(store, settings, args)
        checkpoint = Checkpoint(checkpoint_path, run_identity(qa, dataset))
        results = run_ask_evaluation(
            qa, dataset, tuple(args.languages), _question_progress, holdout, checkpoint, args.limit, should_stop
        )
    run = results["run"]
    _emit(f"model {results['model']} at {results['base_url']}")
    _emit(f"  {'status':<32} {run['status']}")
    _emit(f"  {'questions':<32} {run['questions_done']} done of {run['questions_planned']} in the set")
    for name, value in results["summary"].items():
        _emit(f"  {name:<32} {value}")
    _write_json(output, results)
    _emit(f"\nFull results: {output}")
    if run["complete"]:
        checkpoint_path.unlink(missing_ok=True)
    elif run["stopped_because"]:
        _emit(f"Stopped early. Answers so far are in {checkpoint_path}. Run the same command again to continue.")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repomind", description="Find and explain code with verifiable citations.")
    commands = parser.add_subparsers(dest="command", required=True)

    index = commands.add_parser("index", help="index one commit of a Git repository (read-only)")
    index.add_argument("--path", required=True, help="path to a local Git repository")
    index.add_argument("--name", help="repository name (default: from the origin URL)")
    index.add_argument("--ref", default="HEAD", help="commit to index (default: HEAD)")
    index.add_argument("--force", action="store_true", help="parse again even if this commit is already indexed")
    index.add_argument("--no-embed", action="store_true", help="skip embeddings; keyword search still works")
    index.add_argument("--model", help="embedding model")
    index.set_defaults(handler=command_index)

    embed = commands.add_parser("embed", help="compute embeddings that are not cached yet")
    embed.add_argument("--model", help="embedding model")
    embed.add_argument("--repo", action="append", help="limit to a repository (repeatable)")
    embed.set_defaults(handler=command_embed)

    repos = commands.add_parser("repos", help="list indexed repositories and commits")
    repos.set_defaults(handler=command_repos)

    search = commands.add_parser("search", help="search indexed code")
    search.add_argument("query")
    search.add_argument("--mode", choices=MODES, default="hybrid")
    search.add_argument("--repo", action="append", help="limit to a repository (repeatable)")
    search.add_argument("-k", "--limit", type=int, default=5)
    search.add_argument("--explain", action="store_true", help="show why each result was ranked")
    search.add_argument("--model", help="embedding model")
    search.add_argument(
        "--include-tests",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="default: only when the query mentions tests",
    )
    search.add_argument(
        "--translate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="also search with a local English translation of Turkish queries",
    )
    search.set_defaults(handler=command_search)

    evaluate = commands.add_parser("evaluate", help="measure Recall@5, MRR and citation validity")
    evaluate.add_argument("--dataset", default=None, help="evaluation set (default: eval/questions.yaml)")
    evaluate.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    evaluate.add_argument("--languages", nargs="+", choices=["tr", "en"], default=["tr", "en"])
    evaluate.add_argument("--split", choices=["all", "dev", "test"], default="all")
    evaluate.add_argument("--model", help="embedding model")
    evaluate.add_argument("--rrf-k", type=int, default=60)
    evaluate.add_argument("--include-tests", action=argparse.BooleanOptionalAction, default=None)
    evaluate.add_argument("--translate", action=argparse.BooleanOptionalAction, default=True)
    evaluate.add_argument("--output", help="write full per-question results to this JSON file")
    evaluate.add_argument("--overwrite", action="store_true", help="allow replacing a finished result")
    evaluate.set_defaults(handler=command_evaluate)

    ask = commands.add_parser("ask", help="answer a question with verified citations")
    ask.add_argument("question")
    ask.add_argument("--repo", action="append", help="limit to a repository (repeatable)")
    ask.add_argument("--model", help="embedding model")
    ask.add_argument("--translate", action=argparse.BooleanOptionalAction, default=True)
    ask.add_argument("--no-llm", action="store_true", help="only list the closest lines, even if a model is configured")
    ask.set_defaults(handler=command_ask)

    evaluate_ask = commands.add_parser("evaluate-ask", help="measure answers from the configured LLM")
    evaluate_ask.add_argument("--dataset", help="question set (default: eval/questions.yaml and its held-back negatives)")
    evaluate_ask.add_argument("--languages", nargs="+", choices=["tr", "en"], default=["tr"])
    evaluate_ask.add_argument("--model", help="embedding model")
    evaluate_ask.add_argument("--translate", action=argparse.BooleanOptionalAction, default=True)
    evaluate_ask.add_argument(
        "--output", required=True, help="write answers to this JSON file; each question is also saved next to it as it finishes"
    )
    evaluate_ask.add_argument("--no-llm", action="store_true", help="measure the evidence selection alone, with no model")
    evaluate_ask.add_argument("--overwrite", action="store_true", help="start again from nothing, replacing a finished result")
    evaluate_ask.add_argument("--limit", type=int, help="answer only this many questions, evenly spaced across the set")
    evaluate_ask.add_argument(
        "--min-free-memory-gb",
        type=float,
        default=3.0,
        help="do not start, and stop between questions, when less than this much memory is free (default: 3, 0 turns it off)",
    )
    evaluate_ask.set_defaults(handler=command_evaluate_ask)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if getattr(args, "dataset", "unset") is None and args.command != "evaluate-ask":
        from repomind.evaluation.dataset import DEFAULT_DATASET_PATH

        args.dataset = DEFAULT_DATASET_PATH
    try:
        return args.handler(args, Settings.load())
    except RepoMindError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
