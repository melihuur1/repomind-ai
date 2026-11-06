# RepoMind AI

**Intelligent GitHub repository analyzer.** Ask where and how something is done in a codebase, and get the code that answers it, with file and line citations verified against the exact Git commit.

> **Status: the engine is finished and measured, and the product phase is next.** This is the retrieval and answering engine with a command line interface. It is measured on real repositories, and the results below are reported as they are, including where they are weak. **Search works and is useful. AI answers are not reliable, and every line must be read.** The web application is [planned](docs/product-plan.md), and not built yet.

## Results

**The final measurement did not pass.** A last set of 24 questions, committed before any run and run once on the fixed system, gave:

| Final set, Turkish, `qwen2.5:7b` | Result | Target |
| --- | --- | --- |
| Correct source, of 16 | 8 | at least 8 |
| Wrong source, of 16 | **4** | at most 2 |
| No answer, of 16 | 4 | |
| Unanswerable refused, of 8 | **6** | at least 7 |
| Invalid file or line references | 0 of 120 | 0 |
| Graded correct, partly correct, wrong | 9, 1, 2 | |
| Mean seconds per question | 6.6 | |

Every line shown exists in Git, and that does not mean it answers the question. The same developer wrote these questions and the system, and one reviewer graded the answers, so this is not an independent external test. See [the details and the known problems](docs/evaluation.md#fifth-question-set).

The rest of this section is how the project got there. Measured on two real projects: [StockGuard](https://github.com/melihuur1/stockguard) (Java) and [SyncNotes](https://github.com/melihuur1/syncnotes) (TypeScript). The numbers below come from a second question set of 26 answerable and 10 unanswerable questions, written and committed before it was first run, and not used for any tuning.

Hybrid search, first phase against current:

| Questions in | Scope | Recall@5 | Hit@5 | MRR |
| --- | --- | --- | --- | --- |
| Turkish | Code (15) | 0.567 → 0.567 | 0.600 → 0.600 | 0.465 → 0.468 |
| Turkish | SQL and YAML (11) | 0.000 → 0.818 | 0.000 → 0.818 | 0.000 → 0.761 |
| English | Code (15) | 0.833 → 0.833 | 0.867 → 0.867 | 0.581 → 0.583 |
| English | SQL and YAML (11) | 0.000 → 0.818 | 0.000 → 0.818 | 0.000 → 0.691 |

What these numbers say:

- **The gain is coverage, not better ranking.** SQL migrations and YAML configuration are now searchable, and that accounts for all of the improvement. Results on code questions did not change, and on the older question set Turkish MRR fell from 0.604 to 0.549, because the new chunks compete with code.
- **A reranker was tested and rejected.** The best candidate gave no clear gain across both question sets and took about 21 seconds per query, against about 25 milliseconds for hybrid search. See [the details](docs/evaluation.md#reranker).
- **Every citation was valid.** All search results and all citations shown with answers match the Git objects at the indexed commit.

Answers on the same set, in Turkish, with the small local model `qwen2.5:3b` (CPU, $0), graded by one reviewer without knowing which version wrote each answer:

| 26 answerable questions | Correct | Partly correct | Wrong | Refused |
| --- | --- | --- | --- | --- |
| First phase | 0 | 4 | 11 | 11 |
| Current | **0** | 11 | 13 | 2 |

Of the 10 unanswerable questions, the first phase refused 4 and that version 6. **No answer was fully correct.** So the answer layer was redesigned: answers are now the code's own lines, and the model only chooses among them. A small pilot measured it against the same 18 new questions, with targets fixed in advance:

| Pilot, Turkish | Previous version, 3B | Real lines, 3B | Real lines, 7B | Target |
| --- | --- | --- | --- | --- |
| Correct source, of 12 | 4 | 4 | 7 | at least 6 |
| Wrong source, of 12 | 8 | 7 | **3** | at most 2 |
| Unanswerable refused, of 6 | 3 | 4 | 6 | at least 5 |
| Invalid file or line references | 0 | 0 | 0 | 0 |
| Fully correct answers, of 12 | 1 | 3 | 6 | |
| Mean seconds per question | 54 | 14 | 6 | |

No configuration passed. `qwen2.5:7b` missed the bar by one question and did much better than `qwen2.5:3b`, but these questions had been used before, and the 7B column ran partly on a GPU. So `qwen2.5:7b` became the default, search was tuned on the earlier sets, and a new set of 24 questions, fixed before those changes, measured the result once:

| New set, Turkish, `qwen2.5:7b` | Before the search changes | After | Target |
| --- | --- | --- | --- |
| Correct source, of 16 | 6 | 6 | at least 8 |
| Wrong source, of 16 | 5 | 3 | at most 2 |
| No answer, of 16 | 5 | 7 | |
| Unanswerable refused, of 8 | 8 | 8 | at least 7 |
| Invalid file or line references | 0 | 0 | 0 |
| Fully correct answers, of 16 | 4 | 4 | |
| Mean seconds per question | 6.0 | 5.7 | |

**This did not pass, and the search changes showed no clear gain.** The system now rarely invents an answer and shows only real lines, but it finds the right source for fewer than half of the questions. The pilot had looked better than this. Search is the largest loss, on short configuration lines and through translation errors. The same developer wrote these questions, and 24 are too few to prove anything in general. Details are in the [full evaluation](docs/evaluation.md#fourth-question-set).

Search was then worked on further, with no model, on these same questions and the earlier ones: the right code reaches the 5 passages for 65 to 68 of 85 Turkish questions, depending on the run of the translations, up from 61, and a named term such as `token-ttl` is shown for 277 of 288 lookups, up from 261. No more unanswerable questions get through. These questions had already been read, so this is not an independent result, and it has not yet been measured on answers. See [the details](docs/evaluation.md#search-work-after-the-fourth-set).

A line existing in Git does not mean it answers the question, and the code keeps these two checks apart. When the model chooses lines that the evidence rule turns down, the answer stays "I do not know", and the lines are shown separately as a source to review, with their path, line numbers and code. See [how this works](docs/architecture.md#two-checks-that-must-not-be-confused).

## How it works

1. **Read** a commit from Git's object database. Analyzed projects are never modified and their code is never executed.
2. **Parse** Java, TypeScript and JavaScript with Tree-sitter into symbols, imports and syntax-aligned chunks. SQL is split into statements and YAML into its structure, as text that is never run.
3. **Exclude** hidden files, dependency and build folders, generated code, personal configuration and anything containing credentials.
4. **Embed** chunks locally with `intfloat/multilingual-e5-small` (384 dimensions, no API cost), caching by content so unchanged code is never embedded twice.
5. **Search** with BM25, pgvector, or both through Reciprocal Rank Fusion.
6. **Answer** by narrowing search results to a few short passages, with no model. An optional LLM only chooses among them. The answer is the code's own lines, checked against Git, and any sentence from the model is shown apart and labelled as unverified.

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

The first indexing or semantic search downloads the local models into `.models/` (about 0.6 GB for the embedding and translation models).

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

Without a configured LLM, or with `--no-llm`, `ask` lists the closest lines and says plainly that this is not an AI answer. To let a free local model choose among them, which needs no account or API key:

```bash
docker compose --profile llm up -d ollama
docker exec repomind-ollama ollama pull qwen2.5:7b
```

Then add to `.env`:

```bash
REPOMIND_LLM_BASE_URL=http://127.0.0.1:11535/v1
REPOMIND_LLM_MODEL=qwen2.5:7b
```

`qwen2.5:7b` is the default, is Apache-2.0 licensed, and took about 6.4 GB across RAM and VRAM in the pilot. The smaller `qwen2.5:3b` also works, but it is under the Qwen Research License rather than an open source license, and it did worse in the pilot. Any OpenAI-compatible endpoint works. Sending code to a remote endpoint must be enabled explicitly, and paid use needs prices and a budget in `.env`.

The container keeps one model in memory at a time, is limited to 8 GB, and uses an NVIDIA GPU through `gpus: all` in `compose.yaml`. On a machine without one, remove that line and it runs on the CPU. To give the memory back when you are done, run `docker compose stop ollama`.

**Limits of answers.** Every line shown is checked against Git, and lines that mention too little of a question do not count as evidence. That does not make them the answer: choosing them is the model's judgement, and in the evaluation it often chose wrongly. A citation alone does not prove an answer. Use `ask` to find the relevant code, and read it.

### Evaluate

```bash
python -m repomind index --path "<stockguard clone>" --name stockguard --ref b6698d81475232f2b8ca95af45336fae5665cd81
python -m repomind index --path "<syncnotes clone>" --name syncnotes --ref bb217cddc89088f27b68715cf9585e94ce2fe3a6
python -m repomind evaluate --dataset eval/questions_v2.yaml
python -m repomind evaluate-ask --dataset eval/questions_v2.yaml --output eval/results/my-run.json      # requires a configured LLM
```

`evaluate-ask` saves every question as it finishes. If it is stopped, the same command continues from there, and its results say `incomplete` until every question is done. It does not start, and stops between questions, when less than 3 GB of memory is free. A finished result is never replaced without `--overwrite`.

To try a local model at low risk, [`eval/tools/resource_trial.ps1`](eval/tools/resource_trial.ps1) runs 10 evenly spaced questions, records memory use, and refuses to start with less than 12 GB free.

### Test

```bash
pytest
```

Tests that need the database, the evaluation repositories or a running LLM skip themselves when those are unavailable. Set `REPOMIND_REPO_STOCKGUARD` and `REPOMIND_REPO_SYNCNOTES` to run the gold label checks.

## Evaluation discipline

- Questions and gold labels were committed **before any search code existed**, as the Git history shows, and have not been changed since.
- All tuning used the first question set. The second set was committed before it was first run, and nothing was tuned on it.
- Every gold span is checked by test against the pinned commit. Every "unanswerable" question has its absent terms verified the same way.
- Results include 95% bootstrap confidence intervals and paired comparisons.
- Answers are graded with the run label hidden, by [rules](eval/tools/GRADING.md) written before any of them was read.

The full method, every stage of the investigation and the rejected ideas are in [docs/evaluation.md](docs/evaluation.md).

## Known limitations

- **AI answers are not reliable.** On the final question set, `qwen2.5:7b` found the right source for 8 of 16 questions and showed a wrong one for 4. It refused 6 of 8 unanswerable ones. No configuration has passed its bar on any set.
- **The right lines often never reach the model.** For 6 of those 16 questions, search or passage selection lost them.
- **The model's notes can be false.** They are labelled as unverified, and one on the final set claimed a feature that does not exist.
- **`qwen2.5:3b` does not decline.** Asked to choose lines or none, it chose every time, so with that model all refusals come from mechanical rules.
- **A verified line is not a proven answer.** Lines are checked against Git. Whether they answer the question is not checked.
- The plan for improving answers is in [docs/answer-plan.md](docs/answer-plan.md).
- Turkish retrieval is weaker than English (Hit@5 0.692 against 0.846) and depends on an imperfect translation, which on the pilot still changed some questions' meaning.
- Answers took about 7 seconds per question with 7B partly on an RTX 3050, and about 14 with 3B on an 8-core CPU.
- The evaluation is small, covers two repositories and was labelled and graded by one reviewer. The gold labels are not exhaustive, so some reasonable results count as misses.
- Secret detection recognizes known token formats and literal credential keys, not every hardcoded password.

See [docs/architecture.md](docs/architecture.md#known-limitations) for the complete list.
