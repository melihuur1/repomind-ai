# Before the first real request to OpenAI

The OpenAI provider was built and tested against stand-ins: a fake HTTP opener, two HTTP servers on 127.0.0.1, and a browser test server with a made-up key. The tests still send nothing to OpenAI.

**Three real requests have been sent since, on 2026-11-22**, each approved by the owner beforehand. They went to a made-up lending library, indexed in a database of its own, with two planted fake credentials:
- one English question, one request;
- one Turkish question, two requests: one to translate it, one to choose.

They cost about $0.0003 in all. No planted value was in any request or answer, and every line shown was in Git at the indexed commit. They checked that the integration works, not how accurate the answers are. What they showed is in [development-handoff.md](development-handoff.md#the-real-api-tests).

This page is what has to be true before a first real request on any other setup, and how to check each point. It needs the owner's own key, their approval, and a small budget.

## 1. The index that the questions are asked of

### What an index can hold

Index version 3 withholds credentials written into code when it indexes. Version 2 stored them. On the two evaluated repositories, StockGuard and SyncNotes, a version 2 index held 14 chunks and 1 signature that the credential rules flag, most of them in test files and configuration, such as test passwords and a database URL. A version 1 index cannot be read by this code. The test databases are emptied by the tests on every run.

### What stops them reaching a prompt

The index is not the last line. With this branch's code, a chunk is checked again in search, and a passage again before it is put in a prompt. Both checks drop anything the credential rules flag. To see whether that holds on an old index, [`eval/tools/prompt_check.py`](../eval/tools/prompt_check.py) runs questions through the real engine with a stand-in model that keeps every prompt, over a read-only session. On a version 2 index of the two evaluated repositories:

- **Questions:** 68 in all. 48 are from the sixth set. 20 ask for secrets outright, such as "What is the JWT secret?" and "Veritabanı şifresi nedir?".
- **Prompts:** 91 were recorded.
- **Stored credential lines:** none of the 19 is in any prompt.
- **Passages flagged by their own language's rules:** 0.
- **Passages to look at:** 5. Another language's rules flag them, and each is a name and not a value. They are a reference to a setting, a validation message, two type declarations (`accessToken: string;`) and a CORS option (`credentials: true,`).

The rules can miss a secret they do not recognise, for example a password in a variable named `x`. That is why the first real run should be on a repository with nothing to hide (section 2).

### Indexing again

An index made before version 3 still stores such chunks. Indexing it again is not needed for a first run on a separate, safe repository. If it is done, three things should be known first:

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

A database that got the column from an earlier build of this branch has its older rows marked `reported` instead. Rows from the local model cost $0, so no figure on the page changes. Relabelling them is one statement, to run only on purpose:

```sql
UPDATE llm_usage SET kind = 'legacy' WHERE kind = 'reported' AND created_at < '2026-11-22';
```

## 2. Checklist for the first real request

| # | What | How to check it |
| --- | --- | --- |
| 1 | **A safe repository** | A small repository with nothing secret in it, in a database of its own (`REPOMIND_DATABASE_URL=…/repomind_openai_trial`). The main index takes no part. One made-up credential line in it, such as `const API_TOKEN = "made-up-token-value"`, shows whether it stays out |
| 2 | **What would be sent** | `prompt_check.py --database repomind_openai_trial --repo <name> --questions <file> --must-not-contain <each planted fake value>` before any real request. Expect 0 prompts carrying a stored credential line, 0 holding a planted value, and 0 passages flagged by their own rules. A planted value withheld at indexing leaves no stored line to compare with, so `--must-not-contain` checks for it directly |
| 3 | **A model, checked that day** | From OpenAI's model list. It must take Chat Completions with `response_format` `json_object`, and `temperature` 0, or, for a reasoning model, the `reasoning_effort` value that is set. Its thinking counts against `REPOMIND_LLM_MAX_OUTPUT_TOKENS`, 250 by default, so use a low effort or raise that limit |
| 4 | **The key** | A project key in `.env` only. `git check-ignore .env` names the file, and `git status` does not list it. A spending limit on the OpenAI project as well, as a second guard |
| 5 | **Consent** | `REPOMIND_LLM_ALLOW_REMOTE=1`. The page shows the "Sent to OpenAI" note |
| 6 | **Prices and a small budget** | From OpenAI's pricing page for that model: input, output, the 30-minute cache write (required) and cached input (optional). `REPOMIND_LLM_MAX_TOTAL_COST_USD=0.05` or less |
| 7 | **The model check** | `/api/health` gives `"state": "ready"` and `"provider": "openai"`. It is a free `GET /v1/models/<model>`. A refused key shows `auth_failed`, a wrong name `model_missing`. Neither costs anything |
| 8 | **One real question** | About the safe repository, in English, then one in Turkish (which is also a translation request). Two or three requests in all |
| 9 | **The sources** | Every line shown has "In Git at" with the indexed commit. The made-up credential line is not among them |
| 10 | **Tokens and cost** | The answer shows its tokens and cost. The spending in the AI panel follows. `llm_usage` rows are `reported`. OpenAI's usage page shows the same requests, later and as the real figures |
| 11 | **Afterwards** | Remove `REPOMIND_LLM_PROVIDER` and the key from `.env`, or keep them only while the trial goes on |

What would stop the run: a request that the budget does not cover, any state other than `ready` in item 7, or anything in item 2.
