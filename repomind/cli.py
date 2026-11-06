"""Command line interface. Every failure ends in a readable message, not a traceback."""

from __future__ import annotations

import argparse
import json
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
    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = PROJECT_ROOT / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
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

    config = load_llm_config()
    client = OpenAICompatibleClient(config) if config else None
    search = SearchEngine(store, _embedder(settings, args.model), SearchConfig(), _translator(settings, args))
    return QAEngine(store, search, config, client)


_STATUS_MESSAGES = {
    "no_llm": (
        "No LLM is configured, so this is not an AI answer. The most relevant code is listed below.\n"
        "To enable answers, set REPOMIND_LLM_BASE_URL and REPOMIND_LLM_MODEL (see README)."
    ),
    "no_results": "I do not know: no indexed code matches this question.",
    "insufficient": "I do not know: the indexed code does not give enough evidence to answer this.",
    "invalid_response": "I do not know: the model did not return a valid response, so none of it is shown.",
}


def command_ask(args: argparse.Namespace, settings: Settings) -> int:
    with _store(settings) as store:
        result = _qa_engine(store, settings, args).ask(args.question, args.repo)
    if result.translated_query:
        _emit(f"translated query: {safe_text(result.translated_query)}")
    for warning in result.warnings:
        _emit(f"warning: {safe_text(warning)}")
    if result.status == "answered":
        _emit("")
        _emit(safe_text(result.answer))
        _emit("")
        _emit("Verified sources:")
        for citation in result.citations:
            _emit(
                f"  [{citation.source_id}] {citation.path}:{citation.start_line}-{citation.end_line}"
                f"  ({citation.repo}@{citation.commit_sha[:12]})"
            )
    else:
        _emit(_STATUS_MESSAGES[result.status])
        if result.hits:
            _emit("")
            _emit("Closest code found:" if result.status != "no_llm" else "Sources:")
            print_hits(result.hits, preview_lines=0)
    for rejection in result.rejected:
        _emit(f"rejected citation: {safe_text(rejection)}")
    if result.model:
        approximate = " (estimated)" if result.tokens_are_estimated else ""
        _emit("")
        _emit(
            f"model {result.model}: {result.input_tokens} input + {result.output_tokens} output tokens{approximate}, "
            f"${result.cost_usd:.6f}; {result.sources_sent} sources sent ({result.context_chars} chars), "
            f"{result.sources_dropped} left out by the limits"
        )
    return 0


def command_evaluate_ask(args: argparse.Namespace, settings: Settings) -> int:
    from repomind.evaluation.ask_runner import run_ask_evaluation
    from repomind.evaluation.dataset import DEFAULT_DATASET_PATH, load_dataset, load_holdout_unanswerable
    from repomind.qa.llm import load_llm_config

    if load_llm_config() is None:
        raise RepoMindError(
            "No LLM is configured, so there are no answers to evaluate.",
            hint="Set REPOMIND_LLM_BASE_URL and REPOMIND_LLM_MODEL (see README).",
        )
    dataset = load_dataset(DEFAULT_DATASET_PATH)
    with _store(settings) as store:
        qa = _qa_engine(store, settings, args)
        results = run_ask_evaluation(qa, dataset, tuple(args.languages), _progress, load_holdout_unanswerable())
    _emit(f"model {results['model']} at {results['base_url']}")
    for name, value in results["summary"].items():
        _emit(f"  {name:<32} {value}")
    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = PROJECT_ROOT / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
        _emit(f"\nFull results: {output}")
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
    evaluate.set_defaults(handler=command_evaluate)

    ask = commands.add_parser("ask", help="answer a question with verified citations")
    ask.add_argument("question")
    ask.add_argument("--repo", action="append", help="limit to a repository (repeatable)")
    ask.add_argument("--model", help="embedding model")
    ask.add_argument("--translate", action=argparse.BooleanOptionalAction, default=True)
    ask.set_defaults(handler=command_ask)

    evaluate_ask = commands.add_parser("evaluate-ask", help="measure answers from the configured LLM")
    evaluate_ask.add_argument("--languages", nargs="+", choices=["tr", "en"], default=["tr"])
    evaluate_ask.add_argument("--model", help="embedding model")
    evaluate_ask.add_argument("--translate", action=argparse.BooleanOptionalAction, default=True)
    evaluate_ask.add_argument("--output", help="write answers and details to this JSON file")
    evaluate_ask.set_defaults(handler=command_evaluate_ask)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if getattr(args, "dataset", "unset") is None:
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
