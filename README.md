# RepoMind AI

**Intelligent GitHub repository analyzer.** Ask where and how something is done in a codebase, and get the code that answers it, with file and line citations verified against the exact Git commit.

> **Status: first phase.** This is the retrieval and answering engine with a command line interface. It is measured on real repositories, and the results below are reported as they are, including where they are weak. There is no web application yet, by design.

## Results

Measured on 31 questions, asked in Turkish and in English, over two real projects: [StockGuard](https://github.com/melihuur1/stockguard) (Java) and [SyncNotes](https://github.com/melihuur1/syncnotes) (TypeScript). The numbers come from the 20 held-out questions, which were not used for any tuning.

| Questions in | Method | Recall@5 | Hit@5 | MRR |
| --- | --- | --- | --- | --- |
| English | Keyword / symbol (BM25) | 0.617 | 0.850 | 0.663 |
| English | Semantic (pgvector) | 0.592 | 0.850 | 0.703 |
| English | Hybrid (RRF) | 0.592 | 0.800 | 0.674 |
| Turkish | Keyword / symbol (BM25) | 0.450 | 0.650 | 0.476 |
| Turkish | Semantic (pgvector) | 0.425 | 0.550 | 0.488 |
| Turkish | Hybrid (RRF) | 0.475 | 0.650 | 0.562 |

What these numbers say:

- **Hybrid search is not clearly better than either method alone.** With 20 questions the differences are mostly within noise; the only significant one is Turkish MRR against semantic search (+0.074). Hybrid is still the default because it is never clearly worse and is the most stable across languages.
- **Every citation was valid.** All 930 search results checked in the final run match the Git objects at the indexed commit.
- **The largest gain came from failure analysis, not fusion.** Leaving test code out by default and translating Turkish queries raised held-out Turkish hybrid MRR from 0.307 to 0.562.
- **Recall@5 is moderate.** Most missed code is ranked 6 to 50 rather than not found, which makes reranking the most promising next step.

The optional answer layer is clearly weaker than retrieval. With a small local model (`qwen2.5:3b`, CPU, $0), a manual review of 27 answers found 5 correct, 12 partly correct and 10 wrong, while every citation shown was real. Of 8 fresh questions that the code cannot answer, 7 were refused; 4 of 31 answerable questions were wrongly refused or discarded. Details are in the [full evaluation](docs/evaluation.md#answer-layer).

## How it works

1. **Read** a commit from Git's object database. Analyzed projects are never modified and their code is never executed.
2. **Parse** Java, TypeScript and JavaScript with Tree-sitter into symbols, imports and syntax-aligned chunks. Large functions are split at statement boundaries.
3. **Exclude** hidden files, dependency and build folders, generated code and anything containing credentials.
4. **Embed** chunks locally with `intfloat/multilingual-e5-small` (384 dimensions, no API cost), caching by content so unchanged code is never embedded twice.
5. **Search** with BM25, pgvector, or both through Reciprocal Rank Fusion.
6. **Answer** optionally through an LLM, verifying its citations in the backend.

The design and its trade-offs are in [docs/architecture.md](docs/architecture.md).

## Getting started

Requires Python 3.11+, Git and Docker.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
docker compose up -d db          # PostgreSQL + pgvector on 127.0.0.1:55432
```

The first indexing or semantic search downloads the local models into `.models/` (about 1.3 GB including the candidates used for evaluation, less for the default model alone).

### Index and search

```bash
python -m repomind index --path "/path/to/repository"
python -m repomind repos

python -m repomind search "Stok tutarlılığı nasıl sağlanıyor?"
python -m repomind search "How is overselling prevented?" --repo stockguard --explain
python -m repomind search "cancelReservation" --mode keyword
```

Search needs no API key and no LLM. Test code is left out unless the query mentions tests or `--include-tests` is passed.

### Answering questions

```bash
python -m repomind ask "Rezervasyon iptali nerede yönetiliyor?" --repo stockguard
```

Without a configured LLM, `ask` returns the relevant code and says plainly that this is not an AI answer. To enable answers with a free local model, which needs no account or API key:

```bash
docker compose --profile llm up -d ollama
docker exec repomind-ollama ollama pull qwen2.5:3b
```

Then add to `.env`:

```bash
REPOMIND_LLM_BASE_URL=http://127.0.0.1:11535/v1
REPOMIND_LLM_MODEL=qwen2.5:3b
```

`qwen2.5:3b` is under the Qwen Research License rather than an open source license, so check that it suits your use. Any OpenAI-compatible endpoint works. Sending code to a remote endpoint must be enabled explicitly, and paid use needs prices and a budget in `.env`.

**Limits of answers.** Citations are verified to exist at the commit, and questions about things the code never mentions are refused. That does not make the sentences true: in the evaluation, the model often misdescribed real code. Use answers to find the relevant code, not as a replacement for reading it.

### Evaluate

```bash
python -m repomind index --path "<stockguard clone>" --name stockguard --ref b6698d81475232f2b8ca95af45336fae5665cd81
python -m repomind index --path "<syncnotes clone>" --name syncnotes --ref bb217cddc89088f27b68715cf9585e94ce2fe3a6
python -m repomind evaluate
python -m repomind evaluate-ask      # requires a configured LLM
```

### Test

```bash
pytest
```

Tests that need the database, the evaluation repositories or a running LLM skip themselves when those are unavailable. Set `REPOMIND_REPO_STOCKGUARD` and `REPOMIND_REPO_SYNCNOTES` to run the gold label checks.

## Evaluation discipline

- Questions and gold labels were committed **before any search code existed**, as the Git history shows, and have not been changed since.
- All tuning used a dev split derived from a hash of each question's id. The held-out split was run once, after the configuration was frozen.
- Every gold span is checked by test against the pinned commit. Every "unanswerable" question has its absent terms verified the same way.
- Results include 95% bootstrap confidence intervals and paired comparisons.

The full method, every stage of the investigation and the rejected ideas are in [docs/evaluation.md](docs/evaluation.md).

## Known limitations

- Only Java, TypeScript and JavaScript are indexed. SQL migrations and YAML configuration are invisible.
- Turkish retrieval depends on an imperfect local translator.
- The evaluation is small, covers two repositories and was labelled by one person. The gold labels are not exhaustive, so some reasonable results count as misses.
- Answer quality is limited by the small local model, as described above.
- Secret detection recognizes known token formats, not arbitrary hardcoded passwords.

See [docs/architecture.md](docs/architecture.md#known-limitations) for the complete list.
