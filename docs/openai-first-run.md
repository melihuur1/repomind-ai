# Before the first real request to OpenAI

Everything about the OpenAI provider has been tested against stand-ins: a fake HTTP opener, two HTTP servers on 127.0.0.1, and a browser test server with a made-up key. **No real request has been sent to OpenAI, and nothing has been charged.** This page is what has to be true before the first one, and how to check each point. The first real run needs the owner's approval, their own key, and a small budget.

## 1. The index that the questions are asked of

### What the indexes hold, read on 2026-11-22

Read-only: sessions set to read only, no index changed.

| Database | Repositories | Index version | Stored text the credential rules flag |
| --- | --- | --- | --- |
| `repomind`, the main index | stockguard, syncnotes, expressjs/cors | 2, outdated | stockguard 3 chunks; syncnotes 11 chunks and 1 signature; cors none |
| `repomind_rt_eval_v1` | stockguard, syncnotes | 2 | the same 14 chunks and 1 signature |
| `repomind_rt_eval_v2` | stockguard, syncnotes | 3 | none |
| `repomind_phase1` | stockguard, syncnotes | 1 | not readable by this code, and never used by it |
| `repomind_test`, `repomind_e2e` | test fixtures | 3 | emptied by the tests each run |

Index version 3 withholds credentials written into code when it indexes. Version 2 stored them. Most of the flagged chunks are in test files and configuration, such as test passwords and a database URL.

### What stops them reaching a prompt

The index is not the last line. With this branch's code, a chunk is checked again in search, and a passage again before it is put in a prompt. Both checks drop anything the credential rules flag. To see whether that holds on the old index, [`eval/tools/prompt_check.py`](../eval/tools/prompt_check.py) runs questions through the real engine with a stand-in model that keeps every prompt, over a read-only session. On the main index:

- **Questions:** 68 in all. 48 are from the sixth set. 20 ask for secrets outright, such as "What is the JWT secret?" and "Veritabanı şifresi nedir?".
- **Prompts:** 91 were recorded.
- **Stored credential lines:** none of the 19 is in any prompt.
- **Passages flagged by their own language's rules:** 0.
- **Passages to look at:** 5. Another language's rules flag them, and each is a name and not a value. They are a reference to a setting, a validation message, two type declarations (`accessToken: string;`) and a CORS option (`credentials: true,`).

The rules can miss a secret they do not recognise, for example a password in a variable named `x`. That is why the first real run should be on a repository with nothing to hide (section 2).

### Indexing again

The main index is outdated and still stores those 14 chunks. That is not needed for a first run on a separate, safe repository, and nothing has been indexed again. If it is done later, three things should be known first:

- **It replaces.** `index_repository` deletes a repository's old snapshot and writes the new one in one transaction. A failure leaves the old one, and a success cannot be undone except by indexing again.
- **The repositories have moved on.** StockGuard and SyncNotes have new commits since `b6698d8` and `bb217cd`. Without `--ref` set to those commits, newer code is indexed, and line numbers in older records stop matching.
- **Check it on a scratch database first.** Index into a database of its own, then run `prompt_check.py` and compare. Only then do the same to the main one:

```powershell
$env:REPOMIND_DATABASE_URL = "postgresql://repomind:repomind@127.0.0.1:55432/repomind_reindex_check"
.\.venv\Scripts\python.exe -m repomind index --path "C:\path\to\stockguard" --name stockguard --ref b6698d81475232f2b8ca95af45336fae5665cd81
.\.venv\Scripts\python.exe eval\tools\prompt_check.py --database repomind_reindex_check --repo stockguard --questions eval\questions_v6.yaml
```

### The usage table

`llm_usage` has a `kind` for each row:
- `reported`: the server's token counts;
- `estimated`: no counts were given;
- `worst_case`: a failed request counted at its most;
- `legacy`: recorded before kinds were kept.

A database that gets the column from this version marks its existing rows `legacy`, because reported and estimated counts were not told apart then. The page shows a legacy part apart, and never as reported.

The main index already has the column, with its 2,203 earlier rows marked `reported`. A test that ran an indexing command opened the main database while Phase 4's schema was being tested. That test now uses a stand-in, and the main database was not otherwise changed. All 2,203 rows cost $0, from the local model, so no figure on the page is affected. Relabelling them is one statement, and it is not run without the owner's approval:

```sql
UPDATE llm_usage SET kind = 'legacy' WHERE kind = 'reported' AND created_at < '2026-11-22';
```

## 2. Checklist for the first real request

| # | What | How to check it |
| --- | --- | --- |
| 1 | **A safe repository** | A small repository with nothing secret in it, in a database of its own (`REPOMIND_DATABASE_URL=…/repomind_openai_trial`). The main index takes no part. One made-up credential line in it, such as `const API_TOKEN = "made-up-token-value"`, shows whether it stays out |
| 2 | **What would be sent** | `prompt_check.py --database repomind_openai_trial --repo <name> --questions <file>` before any real request: 0 prompts carrying a stored credential line, 0 passages flagged by their own rules |
| 3 | **A model, checked that day** | From OpenAI's model list. It must take Chat Completions with `response_format` `json_object`, and `temperature` 0, or, for a reasoning model, the `reasoning_effort` value that is set. Its thinking counts against `REPOMIND_LLM_MAX_OUTPUT_TOKENS`, 250 by default, so use a low effort or raise that limit |
| 4 | **The key** | A project key in `.env` only. `git check-ignore .env` names the file, and `git status` does not list it. A spending limit on the OpenAI project as well, as a second guard |
| 5 | **Consent** | `REPOMIND_LLM_ALLOW_REMOTE=1`. The page shows the "Sent to OpenAI" note |
| 6 | **Prices and a small budget** | Both prices from OpenAI's pricing page for that model. `REPOMIND_LLM_MAX_TOTAL_COST_USD=0.05` or less |
| 7 | **The model check** | `/api/health` gives `"state": "ready"` and `"provider": "openai"`. It is a free `GET /v1/models/<model>`. A refused key shows `auth_failed`, a wrong name `model_missing`. Neither costs anything |
| 8 | **One real question** | About the safe repository, in English, then one in Turkish (which is also a translation request). Two or three requests in all |
| 9 | **The sources** | Every line shown has "In Git at" with the indexed commit. The made-up credential line is not among them |
| 10 | **Tokens and cost** | The answer shows its tokens and cost. The spending in the AI panel follows. `llm_usage` rows are `reported`. OpenAI's usage page shows the same requests, later and as the real figures |
| 11 | **Afterwards** | Remove `REPOMIND_LLM_PROVIDER` and the key from `.env`, or keep them only while the trial goes on |

What would stop the run: a request that the budget does not cover, any state other than `ready` in item 7, or anything in item 2.
