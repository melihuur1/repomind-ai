# A small check of the real flow with qwen2.5-coder:7b

Written before the run, with the questions in [`app_check.yaml`](app_check.yaml) fixed and committed first. The tool is [`app_check.py`](tools/app_check.py).

## Why

The [model trial](MODEL_TRIAL.md) gave both models the stored qwen2.5:7b translations, so that only the choice of lines differed. In the application, the configured model also translates every Turkish question. The local opus-mt translator adds a second wording, whatever the model. So with `qwen2.5-coder:7b` configured, the Turkish translation changes too, and the trial did not measure that.

## What runs

- **Six new questions** through `POST /api/ask` on a server started for this check, with `REPOMIND_LLM_MODEL=qwen2.5-coder:7b` in its own environment only. `.env` and the default model stay as they are. There are three questions in Turkish and three in English, and one in each language is unanswerable. They were written by reading the code at the pinned commits, in regions no earlier question accepts.
- **The negative-stock question**, which has been read many times. It is reported apart, and not counted with the six.
- **The 32 development questions of the trials**, translated by the same model, still loaded, with the engine's own translation prompt and parsing. They are compared with the stored qwen2.5:7b translations. Then, with no model, each translation is searched once, to see whether the right code still reaches the candidates, the passages and the evidence rule.

## What is reported

For each of the six:

- the English wording the model made;
- whether the answer is a correct source, a wrong one or none;
- for an unanswerable question, whether it was given a source;
- each returned line checked against Git at its commit, and the GitHub link built the way the web application builds it, with whether GitHub has that file at that commit;
- seconds, tokens, peak GPU memory, and the lowest free memory and commit space.

Six questions prove nothing statistically, and this is not an evaluation. It is a check that the flow works with this model, and of what its translations change.

## Safety

The check starts only when two readings a second apart each show 12 GB of free physical memory and 12 GB of free commit space, and only when no other model is in the model server's memory. Only `qwen2.5-coder:7b` is loaded, and it is unloaded at the end. The application records each request's token use in its own usage table, as it does for any question.

## Result, added after the run

The questions and their accepted lines are as they were. Rows: [`dev-app-check-qwen2.5-coder-7b.json`](results/dev-app-check-qwen2.5-coder-7b.json), [`dev-app-check-qwen2.5-coder-7b-retrieval.json`](results/dev-app-check-qwen2.5-coder-7b-retrieval.json).

The check started at 14.88 and 14.91 GB of free physical memory and 22.2 GB of commit space. The server reported `qwen2.5-coder:7b` ready before the first question, and every answer that used a model names it.

### The six questions

| Question | Model's English | Result | Where it was decided |
|---|---|---|---|
| `app-sg-ttl-max` (tr) | What is the longest wait time that can be set when creating a reservation? | No answer | Search: the request record was not among the passages. The closest lines held the default of 15 minutes, not the limit |
| `app-sn-title-length` (tr) | What is the maximum number of characters that a note title can be saved as? | **Wrong source**, by the accepted lines | The model chose `title-input.tsx:53-57`, which holds `maxLength={200}` on the web page's title field. That states the right number for another place, the browser, which the accepted lines left out. It still counts as wrong |
| `app-sg-barcode` (tr, unanswerable) | How is a barcode generated for a product? | Refused | Before the model: "barcode" is not in the code |
| `app-sg-sim-timeout` (en) | | No answer | The model declined. The passage held `.timeout(REQUEST_TIMEOUT)`, accepted, but not line 49 with the value of 60 seconds |
| `app-sn-search-length` (en) | | No answer | Before the model: "longest" and "accepted" are not in the code, so the refusal check stopped it |
| `app-sn-webhook` (en, unanswerable) | | Refused | The model declined |

Of the four answerable questions: 0 correct, 1 wrong by the accepted lines, 3 without an answer, two of them before the model. Both unanswerable questions were refused. No reply was invalid.

The negative-stock question, apart: the model translated it as "How does StockGuard prevent stock quantities from dropping below zero?" and chose the CHECK constraints in `V2__catalog_and_stock.sql:14-27`.

### Lines and links

All 34 lines returned across the seven questions match Git exactly at the indexed commit, which is the pinned one. These repositories were indexed from local folders, so they have no GitHub address, and the web application links each line to its own Git view rather than to GitHub. No GitHub link was built, and none was checked.

### Cost

From 0.05 s, refused before the model, to 14.1 s, with a median of 4.8 s for the six. Peak GPU memory in use was 2814 MiB. The model was 3282 of 6082 MiB in GPU memory, 54%, as in the trial. The lowest free memory during a question was 8.53 GB physical and 12.44 GB commit space.

### Its translations of the 32 development questions

All 32 came back as usable translations, in 2.0 s each on average. 30 are worded differently from the stored qwen2.5:7b ones. Some are better: `v5-sn-version-viewer` became "for a note" where 7B wrote "for a pull request comment", and `v2-sg-neg-transfer` became "warehouse" where 7B wrote "repository". Some are worse: `v5-sn-version-kinds` became "types of releases", `sg-oversell` "excessive sales", and `v5-sg-movements-order` lost "order".

Searched once with each translation, with no model, of the 20 answerable questions without the negative-stock one:

| | qwen2.5:7b's stored translations | qwen2.5-coder:7b's own |
|---|---|---|
| Right code among the candidates | 17 | 17 |
| In a passage | 15 | 13 |
| Passing the evidence rule | 12 | 11 |
| Unanswerable reaching the model, of 11 | 10 | 9 |

The one lost from the evidence rule is `v5-sg-simulation-default`, which the coder model answered correctly in the trial with the stored translation. `v5-sn-version-kinds`, a wrong source for both models in the trial, no longer reaches the model at all with the coder's translation. These are differences of one or two questions.
