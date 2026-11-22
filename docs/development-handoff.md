# Development handoff

The state of RepoMind AI on 2026-11-23, written for whoever continues it: what it is, what was built on the branch `feature/openai-provider`, what was measured, and what is still only local. It holds no key, password or other secret.

## Purpose

RepoMind AI answers questions about a code repository with the repository's own lines, each checked against Git. Search finds the closest code, a model only chooses among a few real passages, and RepoMind accepts a choice only if it passes an evidence rule. When nothing passes, the answer is "I do not know", and the closest lines are shown as what they are. A wrong source counts as worse than no answer.

It is a portfolio project, for one person on their own machine.

## Architecture

```text
Git repository ──read at one commit──▶ parsing (Tree-sitter) ──▶ chunks, symbols, imports
                                                │
                       credential filters ◀─────┤  files, lines and statements that may hold a secret are left out
                                                ▼
                       PostgreSQL + pgvector: chunks, embeddings, snapshots, usage
                                                │
question ─▶ translation (Turkish) ─▶ hybrid search: BM25 + embeddings, fused with RRF
                                                │
                       passage selection ─▶ declarations of constants ─▶ credential check ─▶ Git check
                                                │
                       refusal rules (words nowhere in the code, no passage passes the evidence rule)
                                                │
                       model chooses passage ids ─▶ evidence rule ─▶ answer, source to review, or "I do not know"
```

- **Read-only on repositories.** Repositories are read through Git plumbing at a pinned commit, and their code is never run.
- **Two copies of the same pipeline.** `repomind/api` is a FastAPI layer over the same engine the command line uses, and `web/` is the React client. `python -m repomind serve` runs both on 127.0.0.1.
- **Where the details are.** [architecture.md](architecture.md) has the full design, and [evaluation.md](evaluation.md) and `eval/*.md` hold every measurement as it was taken.

## Technologies

| Part | What |
| --- | --- |
| Engine | Python 3.13, Tree-sitter, PostgreSQL 16 with pgvector, fastembed (`intfloat/multilingual-e5-small`), a local opus-mt Turkish-English translator |
| Search | BM25 and embeddings, fused with reciprocal rank fusion, plus a small Turkish glossary |
| Models | Ollama with `qwen2.5:7b`, the default and local. The OpenAI API through Chat Completions, optional |
| API | FastAPI, Pydantic. The OpenAPI document generates the client's TypeScript types |
| Web | React, TypeScript, Vite, TanStack Query |
| Tests | pytest, Vitest, Playwright, which drives the real API and database on a fixture repository |
| Running | Docker Compose: `repomind-db`, and `repomind-ollama` behind a profile. Windows with WSL. GPU optional |

## Where the work is

- **Branch:** `feature/openai-provider`, local only, started from `feature/source-accuracy` at `f822593`. That branch is on GitHub and not merged.
- **`main`:** `19e4a54`, the same as `origin/main`. It has one commit this branch lacks, the bilingual README (`README.md` in Turkish, `README.en.md` in English), so merging will need the README changes made here carried into both files.
- **Not tracked by Git:** the trial repository and its questions in `.data/openai-trial/`. The databases live in RepoMind's container.

## The OpenAI provider

`REPOMIND_LLM_PROVIDER=openai` selects it. The default, `ollama`, works as before.

- **Settings, all in `.env`, which Git ignores:**
  - the model, the key and `REPOMIND_LLM_ALLOW_REMOTE=1`;
  - the prices for input, cached input, the 30-minute cache write and output;
  - a budget;
  - optionally `REPOMIND_LLM_REASONING_EFFORT`.

  The address is fixed at `https://api.openai.com/v1`. A configuration pointing elsewhere cannot be built, and redirects are not followed, so the key cannot go to another server.
- **The request.** Chat Completions with:
  - `max_completion_tokens`;
  - `response_format` `json_object`;
  - `store: false`;
  - either `temperature` 0, or `reasoning_effort` without a temperature.

  It is sent again once at most, and only after a 429 rate limit or a 503 that asks to wait 10 seconds or less.
- **Errors.** Each failure has a kind: auth, forbidden, quota, rate limit, bad request, not found, server, timeout, connection, redirect or bad response. No message repeats the server's text or the key. Whatever stops the model, a question is still answered by search, and the page says why.
- **Status.** A free `GET /v1/models/<model>`, asked once a minute. The states are:
  - `ready`, `not_configured`, `misconfigured`;
  - `auth_failed`, `budget_reached`, `limited`, `rejected`;
  - `model_missing`, `offline`, `low_memory`.

  The memory rule applies only to a local model.
- **Cost.**
  - **Pricing.** Input is priced in three parts, as OpenAI bills it: uncached, read from the cache, and written to it. A write replaces the uncached rate and is not added to it.
  - **Budget check.** Before a request, every input token is counted at the highest of the three rates.
  - **Cautious counts.** A request that failed after it may have reached the model is recorded at the most it could cost.
  - **Kinds.** Each usage row has a kind: `reported`, `estimated`, `worst_case`, or `legacy` for rows from before kinds were kept.
- **The page.** It shows:
  - the provider and where the model runs;
  - a note on what is sent to OpenAI, which says that `store: false` is not a promise that nothing is kept;
  - the spending RepoMind has counted, as its own count and not a bill;
  - each answer's tokens and cost.

  An accuracy figure appears only for the model it was measured with, `qwen2.5:7b`.
- **The prompt check.** `eval/tools/prompt_check.py` runs questions through the real engine with a stand-in model, over a read-only session. It looks for anything the index holds as a credential, or planted fake values, in the prompts.

How each piece was decided is in the commit messages, [architecture.md](architecture.md#the-openai-api) and [openai-first-run.md](openai-first-run.md).

## The real API tests

The owner approved each test. Both ran on the database `repomind_openai_trial`, which holds only a made-up lending library with two planted fake credentials. The model was `gpt-6-luna` with `reasoning_effort: none`. Its prices were read from OpenAI's pricing page on 2026-11-22: $0.10 input, $0.01 cached input, $0.125 for the 30-minute cache write and $0.50 output, per million tokens. The budget was $0.05.

| | English question, 1 request | Turkish question, 2 requests |
| --- | --- | --- |
| Question | "How is the late fee calculated when a book is returned after its due date?" | "Bir üye aynı anda en fazla kaç kitap ödünç alabilir?" |
| HTTP, JSON | 200, valid JSON | 200 and 200, valid JSON |
| Translation | – | "What is the maximum number of books a member can borrow at one time?" |
| Model's choice | `src/fees.ts:6-10`, the `lateFee` function, which is right | `src/loans.ts:4`, `MAX_ACTIVE_LOANS = 3`, which is right |
| RepoMind's answer | The chosen lines, with the declaration of the constants they use | "Not enough evidence": the line shares no word with the question. It is shown to review |
| Git check | Every line shown exists at commit `a265b92` | The same |
| Tokens | 1,050 in, of which 1,047 written to the cache, and 45 out | 61 in and 25 out, then 1,058 in, of which 1,055 written, and 43 out |
| Cost | $0.000153675 by today's pricing. Recorded as $0.000128, before cache writes were priced, and left as recorded | $0.0000186 + $0.000153675 = $0.000172275 |
| Fake credentials | In no request and no answer | In no request and no answer |

The trial database records 3 usage rows, $0.000301 in all. Two questions measure the integration, not accuracy.

## Decisions on answers and sources

- **The evidence rule stays.** A chosen passage must share at least two terms with the question (`MIN_SHARED_TERMS = 2`). Looser rules were tried and measured, and each let in more wrong sources than right ones.
- **Linked evidence stays off.** It would accept a declaration the model chose through a line that uses it. Measured on 342 questions with no model, about two in three such acceptances were a wrong place or an unanswerable question, both as built and in a variant without its word condition.
- **A declaration the rule turned down is shown with the line that uses it.** This is role `review_use`, in a folded section that says "added by RepoMind: not an answer". The verdict does not change. It is not counted as an accuracy gain: of 28 lines it could add across the question sets, 7 were at a right place.
- **The value is shown with the line that names it.** When the model chooses a line that uses a constant, the constant's declaration is shown with it. This goes one way only.
- **Choosing stays the default.** `choose` is the default way of selecting, and `judge` stays experimental. The model's note is shown folded and marked as unverified. In a replay of the last evaluation, 20 of 52 notes named something not in the lines shown.
- **The last measurement of accuracy.** On the sixth question set, on 2026-11-15, `qwen2.5:7b` gave a fully right source for 13 of 32 questions and a wrong one for 6. Later changes were not measured with a model. No OpenAI model has been evaluated in RepoMind.

## Tests on 2026-11-23

| Suite | Result |
| --- | --- |
| Python, with outbound HTTPS blocked | 1515 passed, 1 skipped. The skipped test is the real-model test, which runs only with `REPOMIND_RUN_LLM_TESTS=1` |
| TypeScript | no errors |
| Vitest | 45 passed |
| Web build | succeeds |
| Playwright | 53 passed, among them a test for the section that shows a declaration with the line that uses it |

A fixture in `tests/conftest.py` blanks every `REPOMIND_LLM_*` setting for every test, so the model settings and key in a developer's `.env` never reach one. The OpenAI tests use fake HTTP and a made-up key.

## Security risks to keep in mind

- **Code leaves the machine with OpenAI.** Each question and the passages chosen for it are sent. The credential rules remove what they recognise, and can miss a secret they do not, such as a password in a variable called `x`. `store: false` is not zero data retention.
- **One person, one machine.** There is no login and no rate limit. The server listens on 127.0.0.1 only, and must not be put on the internet, through a tunnel or a proxy either.
- **The budget is not an exact ceiling.** It depends on prices entered by hand. The token estimate can be low, by at most one request. Two clients asking at once can both pass the check. Requests turned away with a 4xx, 429 or 503 are assumed not to be charged, which OpenAI's documentation does not say.
- **The main index is outdated.** The `repomind` database is at index version 2 and still stores 14 chunks and 2 signatures that hold credentials, mostly from test files. Search and the prompt filter them out again: a read-only check found none in 91 prompts. Rebuilding it replaces the old snapshot and needs `--ref` to the pinned commits, as [openai-first-run.md](openai-first-run.md) describes.
- **The main database's usage rows.** It has 2,203 earlier usage rows marked `reported`. They should be `legacy`, and all cost $0. The statement to relabel them is in openai-first-run.md and has not been run.

## Not on GitHub yet

- All 13 commits of `feature/openai-provider`, from `557780e` to this handoff. None is pushed or merged.
- The tags `v2-candidate` and `accuracy-baseline`, which are local only.
- The trial repository in `.data/openai-trial/` and the database `repomind_openai_trial`, which are local data and not meant for Git.
- The scripts that ran the two real requests and the measurements. They were kept outside the repository, in a temporary folder, and may be gone.

## Next: MCP

The next goal is an MCP server, so that an MCP client can use RepoMind's search and its source-checked answers. Nothing of it is designed yet. The design should start from what RepoMind already guarantees, and keep each guarantee:

- **What to offer.** Likely search, reading a file's lines at the indexed commit, and asking a question. Each comes back with its commit and lines, and with the same roles as the API, so that a client cannot take a source to review for an answer.
- **How to connect.** A local transport, such as stdio, for one person on their own machine, with nothing listening on a network.
- **Limits.** Read-only on repositories and the index. The same credential filters and Git checks. No remote model unless the settings already allow it, and the same budget.
- **Testing.** The same way as the rest: a fake client, no real model requests, and the fixture repository.
