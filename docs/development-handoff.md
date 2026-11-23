# Development handoff

The state of RepoMind AI on 2026-11-23, after the MCP server, written for whoever continues it: what it is, what was built on the branch `feature/openai-provider`, what was measured, and what is not on GitHub yet. It holds no key, password or other secret.

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
- **Three ways in, one engine.** `repomind/api` is a FastAPI layer over the same engine the command line uses, and `web/` is the React client. `python -m repomind serve` runs both on 127.0.0.1. `python -m repomind.mcp_server` serves search and verified lines to an MCP client over stdio.
- **Where the details are.** [architecture.md](architecture.md) has the full design, and [evaluation.md](evaluation.md) and `eval/*.md` hold every measurement as it was taken.

## Technologies

| Part | What |
| --- | --- |
| Engine | Python 3.13, Tree-sitter, PostgreSQL 17 with pgvector, fastembed (`intfloat/multilingual-e5-small`), a local opus-mt Turkish-English translator |
| Search | BM25 and embeddings, fused with reciprocal rank fusion, plus a small Turkish glossary |
| Models | Ollama with `qwen2.5:7b`, the default and local. The OpenAI API through Chat Completions, optional |
| API | FastAPI, Pydantic. The OpenAPI document generates the client's TypeScript types |
| Web | React, TypeScript, Vite, TanStack Query |
| MCP | MCP Python SDK 2.x, over stdio, as the optional extra `[mcp]` |
| Tests | pytest, Vitest, Playwright, which drives the real API and database on a fixture repository |
| Running | Docker Compose: `repomind-db`, and `repomind-ollama` behind a profile. Windows with WSL. GPU optional |

## Where the work is

- **Branch:** `feature/openai-provider`, local only, started from `feature/source-accuracy` at `f822593`. That branch is on GitHub and not merged.
- **`main`:** `19e4a54`, the same as `origin/main`. It has one commit this branch lacks, the bilingual README (`README.md` in Turkish, `README.en.md` in English), so merging will need the README changes made here carried into both files.
- **Not tracked by Git:** the made-up trial repository and its questions, in the ignored `.data/` folder. The databases live in RepoMind's container.

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

The owner approved each test. Both ran on a database of their own, which holds only a made-up lending library with two planted fake credentials. The model was `gpt-6-luna` with `reasoning_effort: none`. Its prices were read from OpenAI's pricing page on 2026-11-22: $0.10 input, $0.01 cached input, $0.125 for the 30-minute cache write and $0.50 output, per million tokens. The budget was $0.05.

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

## The MCP server

`python -m repomind.mcp_server` gives an MCP client, such as Claude Code, read-only access to RepoMind's index over stdio. It opens no network port, asks no model, and loads neither the question engine, the web API nor a model client. It was built in four steps, each approved before it started:

| Step | Commit | What |
| --- | --- | --- |
| M1 | `e883e05` | The server and `list_repositories`. A database name and a list of repositories are required, and the session is read-only |
| M2 | `c66b180` | `search_code`, on `SearchEngine.search`. Every result is checked against Git and for credentials before it is returned |
| M3 | `cbf5fbc` | `get_verified_source`, which reads lines of an indexed file from Git at the indexed commit, through the same function as the web app's file view. Output size limits for both tools |
| M4 | `3fb2794` | Lines are returned only when Git's bytes are the Git object indexing recorded. The server was connected to Claude Code and tested through it |

What every tool keeps to:
- only listed repositories, and only indexes made by this version;
- a read-only database session, checked on every call;
- for `get_verified_source`, only paths the index lists, never a path with `..` or a drive, and never a file from the working folder;
- credential lines hidden whole, also across several lines;
- at most 12,000 characters of code in a response, after the checks;
- every result marked as the repository's data, which may hold instructions that do not come from the user.

**The test through Claude Code**, on 2026-11-23 with Claude Code 2.1.330. The server was added with `--scope local`, so the entry is private to the project folder and no `.mcp.json` was made. It holds only the database and the list of repositories, set to the made-up library. Separate sessions kept nothing and could use no other tool:
- **`list_repositories`:** listed only that library.
- **`search_code`:** "late fee" found `src/fees.ts:6-10` first. Every preview equalled Git at `a265b92`.
- **`get_verified_source`:** read lines 6 to 10 back exactly as Git holds them, with `matches_index` true.
- **Credentials:** the two planted fake credentials came back as hidden lines, and neither value was anywhere in the output.
- **Refused:** an unlisted repository and a path with `..`.

Details: [architecture.md](architecture.md#mcp-server).

## Tests on 2026-11-23

| Suite | Result |
| --- | --- |
| Python, with outbound HTTPS blocked | 1682 passed, 1 skipped. The skipped test is the real-model test, which runs only with `REPOMIND_RUN_LLM_TESTS=1` |
| MCP server in its own process | Included above: 8 tests start it as a process of its own and speak raw JSON-RPC to it. Those that call its tools watch it for any connection or name lookup |
| TypeScript | no errors |
| Vitest | 45 passed |
| Web build | succeeds |
| Playwright | 53 passed |
| `pip check` | no broken requirements |

A fixture in `tests/conftest.py` blanks every `REPOMIND_LLM_*` setting for every test, so the model settings and key in a developer's `.env` never reach one. The OpenAI tests use fake HTTP and a made-up key. The real-model test is the one exception: with `REPOMIND_RUN_LLM_TESTS=1` it uses the configured provider, so with OpenAI configured it sends a real, paid request.

## Security risks to keep in mind

- **Code leaves the machine with OpenAI.** Each question and the passages chosen for it are sent. The credential rules remove what they recognise, and can miss a secret they do not, such as a password in a variable called `x`. `store: false` is not zero data retention.
- **Code leaves the machine through an MCP client.** What the MCP server returns goes to the client's own model, which for Claude Code is Anthropic's API.
- **One person, one machine.** There is no login and no rate limit. The server listens on 127.0.0.1 only, and must not be put on the internet, through a tunnel or a proxy either.
- **The budget is not an exact ceiling.** It depends on prices entered by hand. The token estimate can be low, by at most one request. Two clients asking at once can both pass the check. Requests turned away with a 4xx, 429 or 503 are assumed not to be charged, which OpenAI's documentation does not say.
- **An index made before version 3 stores credentials written into code.** Search and the prompt filter them out again, and the MCP server does not serve such an index at all. Rebuilding one replaces the old snapshot and needs `--ref` set to the pinned commits, as [openai-first-run.md](openai-first-run.md) describes.
- **The MCP server has no database role of its own.** Its session is read-only, and a role that may only read is described in [architecture.md](architecture.md#mcp-server) and not set up.

## Not on GitHub yet

- Every commit of `feature/openai-provider` from `557780e` on. None is pushed or merged.
- The tags `v2-candidate` and `accuracy-baseline`, which are local only.
- The made-up trial repository and its database, which are local data and not meant for Git.
- The scripts that ran the real requests and the measurements. They were kept outside the repository, in a temporary folder, and may be gone.

## Next

New features have stopped. What is left before the project is shared, each step with the owner's approval:
- the README in both languages: the OpenAI provider and the MCP server in the Turkish `README.md` and the English `README.en.md`, from `main`;
- merging `main` into this branch, so that its bilingual README is kept;
- pushing the branch and opening a pull request.
