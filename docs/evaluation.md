# Evaluation

The goal of this phase is to reach the right code reliably, and to know how far that goal has been met. This document reports what was measured, including what did not work.

## Summary

| Question | Answer |
| --- | --- |
| Does retrieval find the right code? | Often. On held-out questions at least one correct span is in the top 5 for 80 to 85% of English and 55 to 65% of Turkish questions. Recall@5 is moderate, at 0.59 to 0.62 and 0.43 to 0.48. |
| Is hybrid search better? | **Not demonstrably.** Most differences are within noise. |
| Are citations real? | **Yes.** All 930 search results, and all 67 citations shown with answers to answerable questions, match Git at the indexed commit. |
| Are AI answers reliable? | **No, not with this small local model.** On manual review, 5 of 27 answers were correct, 12 partly correct and 10 wrong. |
| Does it refuse what it cannot answer? | **Mostly.** 7 of 8 fresh unanswerable questions were refused, at the cost of wrongly refusing about 10% of answerable Turkish questions. |

## Method

### Repositories

| Repository | Commit | Commit date | Language | Files indexed | Chunks (in tests) | Symbols | Imports |
| --- | --- | --- | --- | --- | --- | --- | --- |
| StockGuard | `b6698d81475232f2b8ca95af45336fae5665cd81` | 2026-11-05 | Java | 75 of 96 | 495 (291) | 597 | 865 |
| SyncNotes | `bb217cddc89088f27b68715cf9585e94ce2fe3a6` | 2026-11-04 | TypeScript, TSX, JavaScript | 119 of 170 | 611 (145) | 577 | 488 |
| **Total** | | | | **194** | **1,106 (436)** | **1,174** | **1,353** |

### Questions

[`eval/questions.yaml`](../eval/questions.yaml) has 31 answerable questions with 69 gold spans, each asked in Turkish and English, and 6 unanswerable questions. [`eval/unanswerable_holdout.yaml`](../eval/unanswerable_holdout.yaml) has 8 more unanswerable questions.

The safeguards against flattering results:

- **Written first.** Questions and labels come from reading the source, and were committed before any search code existed (`git log -- eval/questions.yaml`).
- **Not adjusted.** No retrieval question or gold label has changed since. The file's SHA-256 is stored with each result.
- **Verified.** A test checks that each gold span exists at the pinned commit and contains its anchor text, and that each unanswerable question's terms are absent from all indexable source.
- **Split by hash.** A hash of each id assigns 11 questions to dev and 20 to test. All tuning used dev. Test results were read once, after the configuration was committed.

### Metrics

All methods use the same candidate depth of 50.

- **Recall@5:** share of a question's gold spans found in the top 5.
- **Hit@5:** share of questions with at least one gold span in the top 5.
- **MRR:** mean of 1 / rank of the first result matching any gold span.
- **File recall@5:** recall counting any chunk from the right file.
- **Citation validity:** each result's lines compared byte for byte with the blob in Git.

A result matches a gold span if they share at least 3 lines, or the whole span if it is shorter. Chunks are at most 60 lines, so a large chunk cannot match everything.

Intervals are 95% percentile bootstrap over questions. A difference between methods is called clear only if the paired interval excludes zero.

## Results

### Held-out test split (20 questions)

| Language | Method | Recall@5 | Hit@5 | MRR | File recall@5 | Valid citations |
| --- | --- | --- | --- | --- | --- | --- |
| Turkish | Keyword | 0.450 [0.283, 0.625] | 0.650 | 0.476 [0.305, 0.658] | 0.658 | 100/100 |
| Turkish | Semantic | 0.425 [0.242, 0.608] | 0.550 | 0.488 [0.295, 0.684] | 0.558 | 100/100 |
| Turkish | **Hybrid** | 0.475 [0.300, 0.650] | 0.650 | 0.562 [0.367, 0.757] | 0.608 | 100/100 |
| English | Keyword | 0.617 [0.467, 0.758] | 0.850 | 0.663 [0.492, 0.828] | 0.775 | 100/100 |
| English | Semantic | 0.592 [0.442, 0.742] | 0.850 | 0.703 [0.528, 0.860] | 0.750 | 100/100 |
| English | **Hybrid** | 0.592 [0.433, 0.742] | 0.800 | 0.674 [0.504, 0.837] | 0.750 | 100/100 |

### All 31 questions

| Language | Method | Recall@5 | Hit@5 | MRR | File recall@5 | Valid citations |
| --- | --- | --- | --- | --- | --- | --- |
| Turkish | Keyword | 0.452 [0.312, 0.591] | 0.645 | 0.505 [0.356, 0.649] | 0.613 | 155/155 |
| Turkish | Semantic | 0.446 [0.312, 0.581] | 0.645 | 0.568 [0.411, 0.718] | 0.586 | 155/155 |
| Turkish | **Hybrid** | 0.505 [0.366, 0.640] | 0.710 | 0.604 [0.448, 0.749] | 0.634 | 155/155 |
| English | Keyword | 0.608 [0.473, 0.737] | 0.806 | 0.655 [0.512, 0.792] | 0.753 | 155/155 |
| English | Semantic | 0.581 [0.457, 0.699] | 0.839 | 0.690 [0.549, 0.823] | 0.726 | 155/155 |
| English | **Hybrid** | 0.575 [0.441, 0.704] | 0.774 | 0.686 [0.548, 0.821] | 0.742 | 155/155 |

### Is hybrid better?

Paired differences on the held-out split, hybrid minus the other method:

| Language | Against | Recall@5 | MRR |
| --- | --- | --- | --- |
| Turkish | Keyword | +0.025 [-0.142, +0.200] | +0.086 [-0.104, +0.274] |
| Turkish | Semantic | +0.050 [-0.050, +0.150] | **+0.074 [+0.017, +0.148]** |
| English | Keyword | -0.025 [-0.075, 0.000] | +0.012 [-0.113, +0.123] |
| English | Semantic | 0.000 [-0.092, +0.092] | -0.029 [-0.134, +0.068] |

Of eight comparisons, one is clear. In English, keyword search alone matches hybrid, because these codebases use descriptive names and comments. Hybrid stays the default because it is never clearly worse and is the most consistent across languages, not because it was shown to be superior.

### Effect of the changes on held-out data

| Language | Configuration | Keyword MRR | Semantic MRR | Hybrid MRR | Hybrid Recall@5 |
| --- | --- | --- | --- | --- | --- |
| Turkish | First version | 0.177 | 0.311 | 0.307 | 0.325 |
| Turkish | **Final** | 0.476 | 0.488 | **0.562** | **0.475** |
| English | First version | 0.510 | 0.584 | 0.614 | 0.533 |
| English | **Final** | 0.663 | 0.703 | **0.674** | **0.592** |

## The investigation

Numbers in this section are hybrid search on the 11 dev questions, as Recall@5 / MRR. With so few questions, small differences mean little.

| Step | Finding | Turkish | English | Decision |
| --- | --- | --- | --- | --- |
| First version | 55% of English top results were test files | 0.288 / 0.308 | 0.500 / 0.605 | Investigate |
| Leave tests out by default | Tests repeat the vocabulary of the code they exercise | 0.530 / 0.487 | 0.545 / 0.708 | **Kept** |
| At most 2 results per file | No measurable effect | 0.500 / 0.495 | 0.545 / 0.708 | Rejected, removed |
| Scale keyword weight by query coverage | Small gain, possibly noise | 0.530 / 0.530 | 0.545 / 0.708 | Superseded by translation, removed |
| Map unknown words to code terms through embeddings | Mixed: `Rezervasyon → reservation`, but also `Şifreler → reconciler`. Similarities sat in a 0.83 to 0.91 band, so no threshold could separate them | not measured | not measured | Rejected from inspecting its output |
| Add a local translation of Turkish queries | Closed most of the gap to English | **0.561 / 0.680** | 0.545 / 0.708 | **Kept** |

The drop from dev to test for Turkish (MRR 0.680 to 0.562) is the optimism expected from tuning on dev, and the reason for holding questions out.

### Embedding model comparison

Hybrid results on the dev split, with test code excluded and translation on:

| Model | Dimensions | Licence | Turkish | English | Combined |
| --- | --- | --- | --- | --- | --- |
| **`intfloat/multilingual-e5-small`** | 384 | MIT | 0.561 / 0.680 | 0.545 / 0.708 | 0.553 / **0.694** |
| `BAAI/bge-small-en-v1.5` | 384 | MIT | 0.515 / 0.553 | **0.682 / 0.773** | **0.599** / 0.663 |
| `jinaai/jina-embeddings-v2-base-code` | 768 | Apache-2.0 | 0.439 / 0.502 | 0.621 / 0.658 | 0.530 / 0.580 |

No model clearly wins: the intervals overlap. `e5-small` was chosen because it was best for Turkish and on combined MRR, needs no API, and is the only candidate that understands Turkish even if the translator is unavailable. The code-specific model did not show the advantage one might expect. For an English-only deployment, `bge-small` deserves another look.

### Why results are where they are

Where the 69 gold spans ended up in hybrid search, over all 31 questions:

| Rank | Turkish | English |
| --- | --- | --- |
| 1 to 5 | 33 | 39 |
| 6 to 8 | 5 | 8 |
| 9 to 20 | 9 | 7 |
| 21 to 50 | 9 | 6 |
| Not in top 50 | 13 | 9 |

- **Mostly a ranking problem.** About two thirds of missed spans are present among the candidates, which makes a reranker the most promising next step.
- **Comments help a lot.** StockGuard's Javadoc is rich and scores far higher than SyncNotes, which has less prose and much more UI code (English hybrid Recall@5 0.677 against 0.467, MRR 0.805 against 0.560).
- **Broad questions fail.** "Where are user permissions checked?" misses in both repositories, because the answer is spread across several places that use varied vocabulary.
- **Concept and code differ.** "How are viewers prevented from editing?" is implemented by `connection.readOnly = !canEdit(role)`, which shares few words with the question.
- **Multi-span questions cap recall.** The two questions with one gold span reach 1.000 Recall@5, those with two or three about 0.50.
- **Translation errors propagate.** `Şifreler` became "codes", and that question lost its password code.
- **Scores are conservative.** Gold labels are not exhaustive. For "How does StockGuard keep stock counters consistent?", the top result is `ConsistencyService`, which is a reasonable answer but not in the gold list. Under the rules above, the label was not changed.

## Answer layer

Model: `qwen2.5:3b` through Ollama in RepoMind's own container, on CPU, at no cost. Answers below are to the Turkish questions. The first run informed the design of the checks, and the second measures them.

| Measure | Before the checks | With the checks |
| --- | --- | --- |
| Original unanswerable questions refused | **1 / 6** | 4 / 6 |
| Fresh unanswerable questions refused | not run | **7 / 8** |
| Answerable questions answered | 28 / 31 | 27 / 31 |
| Answerable questions wrongly refused | 3 / 31 | 4 / 31 |
| Answers citing a gold span | 21 of 28 | 17 of 27 |
| Gold span present in the context sent | 24 / 31 | 22 / 31 |
| Citations shown that pass an independent recheck | | 67 / 67 |
| Citations rejected by the backend | 26 | 27 |
| Invalid model responses | 0 | 0 |

The context row counts only questions that reached the model, so it falls when the check refuses a question before the model call. The gold-citation row falls for the same reason, for answers discarded by the fabrication check, and because the model's output varied between the two runs.

Usage of the second run, over 45 questions: 64,044 input and 7,627 output tokens, at most 9,909 characters of context per question, 27.8 seconds per question on average, **$0**.

### Manual review of the answers

Citations being real says nothing about whether the sentences are true, so all 27 answers from the second run were read and compared with the code. This is the judgement of one reviewer.

| Verdict | Count | Questions |
| --- | --- | --- |
| Correct | **5** | `sg-reservation-cancel`, `sg-password`, `sg-stale-version`, `sn-ticket`, `sn-version-restore` |
| Partly correct: the right place, with a notable error or gap | **12** | `sg-db-connection-error`, `sg-idempotency`, `sg-expiry`, `sg-lock-order`, `sg-jwt`, `sg-simulation-race`, `sg-first-admin`, `sn-realtime`, `sn-revocation`, `sn-refresh-reuse`, `sn-csrf`, `sn-auto-version` |
| Wrong | **10** | `sg-stock-consistency`, `sg-user-permissions`, `sg-oversell`, `sn-user-permissions`, `sn-lost-updates`, `sn-redis-outage`, `sn-password`, `sn-presence`, `sn-viewer-readonly`, `sn-session-refresh` |

Of the 10 wrong answers, in 6 the right code never reached the model, and in 4 it did and the model misread it. So both retrieval and the model matter. In the former case the model invents an answer from what it was given rather than saying the sources are insufficient: asked how passwords are hashed, it cited a real `sha256` helper, although the code uses Argon2. Some answers also came back in English despite the instruction.

**Conclusion: with this model, use `ask` to find code, not to replace reading it.** A more capable model might do better, but that has not been measured.

### Refusal check without a model

The rule was designed while looking at the original 6 unanswerable questions and the 11 dev questions, then frozen. The 8 fresh questions were committed before it was implemented. Counts below are for the rule alone, with no LLM involved:

| | Turkish | English |
| --- | --- | --- |
| Fresh unanswerable refused | **7 / 8** | **6 / 8** |
| Original unanswerable refused | 4 / 6 | 5 / 6 |
| Answerable wrongly refused | 3 / 31 | **0 / 31** |

All three wrong Turkish refusals come from poor translations (`retrialed`, `spell`, `regulations`). Questions that pass the check and cannot be answered, such as two-factor authentication, still go to the model, and in this run it fabricated answers to 3 of them (`sg-neg-email`, `sn-neg-2fa`, `sg-new-refunds`).

### Prompt injection

An integration test indexes a file whose comment orders the model to ignore its instructions, reply `PWNED` and cite a password file. The real model did not obey, and no citation outside the supplied file appeared. It also did not answer the actual question, returning "insufficient": safe, but not useful. This is one scenario, not a proof of resistance; the structural defences are described in [architecture.md](architecture.md#prompt-injection).

## Threats to validity

- **Small sample.** 31 questions on two repositories give wide intervals. Results may not generalize.
- **Same author.** One person wrote the questions and labels after reading the code, so the wording may lean towards the code's own vocabulary, particularly in English.
- **Non-exhaustive labels** make scores conservative.
- **The answer review is subjective** and was done by one reviewer.
- **Prompt wording** was adjusted after inspecting the outputs for one dev question and one unanswerable question.
- **The original unanswerable questions informed the refusal check**, which is why fresh ones were added. There are only 8, though.
- **Model output varies** between runs, even with temperature 0.

## Reproducing

```bash
python -m repomind index --path "<stockguard>" --name stockguard --ref b6698d81475232f2b8ca95af45336fae5665cd81
python -m repomind index --path "<syncnotes>" --name syncnotes --ref bb217cddc89088f27b68715cf9585e94ce2fe3a6
python -m repomind evaluate --output eval/results/final.json
python -m repomind evaluate-ask --languages tr --output eval/results/ask.json
```

Stored results are in [`eval/results/`](../eval/results): `01-baseline-e5-small.json` is the first version, `final.json` the frozen configuration, and `ask-qwen2.5-3b-tr*.json` hold the answers before and after the checks.
