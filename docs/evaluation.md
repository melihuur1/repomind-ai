# Evaluation

The goal is to reach the right code reliably, explain it correctly, and know how far that goal has been met. This document reports what was measured, including what did not work.

## Summary

| Question | Answer |
| --- | --- |
| Does retrieval find the right code? | Often. On the second question set, at least one correct span is in the top 5 for 85% of English and 69% of Turkish questions. |
| Did phase 2 improve retrieval? | **Coverage yes, ranking no.** SQL and YAML became searchable. Results on code questions did not change, and Turkish MRR on the first set fell slightly. |
| Is a reranker worth keeping? | **No.** No clear gain across both question sets, at about 1,000 times the latency. |
| Is hybrid search better? | **Not demonstrably.** Most differences are within noise. |
| Are citations real? | **Yes.** Every search result and every citation shown with an answer matches Git at the indexed commit. |
| Are AI answers reliable? | **No.** A redesign that answers with real lines did not pass its pilot: the right source for 4 of 12 questions, against a target of at least 6. |
| Does it refuse what it cannot answer? | **Not reliably.** 4 of 6 in the pilot, against a target of at least 5, and only through mechanical rules. The small model never declined. |
| Is `qwen2.5:7b` better? | **Unknown.** Its independent run is `incomplete — interrupted due to memory pressure`, and it was not part of the pilot. |

## Phase 2

Phase 2 set out to improve the answers. All of this was measured on a second question set, [`eval/questions_v2.yaml`](../eval/questions_v2.yaml): 26 answerable questions (15 on code, 11 on SQL or YAML) and 10 unanswerable ones, each in Turkish and English. It targets code that the first set did not use. It was committed before it was first run, and nothing was tuned on it. Phase 1 results come from the code at tag `phase1-baseline`, run on this set against its own database.

### Phase 2 retrieval

Hybrid search, phase 1 → phase 2:

| Language | Scope | Recall@5 | Hit@5 | MRR | Paired difference in MRR |
| --- | --- | --- | --- | --- | --- |
| Turkish | All (26) | 0.327 → **0.673** | 0.346 → **0.692** | 0.268 → **0.592** | +0.324 [+0.151, +0.508] |
| Turkish | Code (15) | 0.567 → 0.567 | 0.600 → 0.600 | 0.465 → 0.468 | +0.004 [-0.096, +0.100] |
| Turkish | SQL, YAML (11) | 0.000 → 0.818 | 0.000 → 0.818 | 0.000 → 0.761 | +0.761 [+0.505, +1.000] |
| English | All (26) | 0.481 → **0.827** | 0.500 → **0.846** | 0.335 → **0.629** | +0.294 [+0.145, +0.460] |
| English | Code (15) | 0.833 → 0.833 | 0.867 → 0.867 | 0.581 → 0.583 | +0.002 [-0.010, +0.017] |
| English | SQL, YAML (11) | 0.000 → 0.818 | 0.000 → 0.818 | 0.000 → 0.691 | +0.691 [+0.464, +0.909] |

**Read the "All" rows with care.** Phase 1 could not index SQL or YAML, so 11 of these questions were out of its reach by construction, and the set was written to include them. The gain is entirely that new coverage. On code questions phase 2 changed nothing.

Adding the new files also has a cost. On the old 31 questions, Turkish MRR fell from 0.604 to 0.549 (-0.055 [-0.119, -0.002]), and Recall@5 from 0.505 to 0.495. English did not change (MRR 0.687 to 0.689).

Methods within phase 2, all 26 questions:

| Language | Method | Recall@5 | Hit@5 | MRR | Median latency |
| --- | --- | --- | --- | --- | --- |
| Turkish | Keyword | 0.692 | 0.692 | 0.540 | 65 ms |
| Turkish | Semantic | 0.712 | 0.731 | 0.522 | 24 ms |
| Turkish | **Hybrid** | 0.673 | 0.692 | 0.592 | 23 ms |
| English | Keyword | 0.654 | 0.692 | 0.597 | 2 ms |
| English | Semantic | 0.788 | 0.808 | 0.583 | 12 ms |
| English | **Hybrid** | 0.827 | 0.846 | 0.629 | 13 ms |

Hybrid is clearly better than keyword only on English Recall@5 (+0.173 [+0.058, +0.308]). The other differences are within noise. All 780 citations in these results are valid.

No gold span reached the top 5 for 8 Turkish questions (`sg-reservation-defaults`, `sg-two-instances`, `sg-auth-error-format`, `sg-simulation-limit`, `sn-cookie-flags`, `sn-env-validation`, `sn-folder-delete`, `sn-title-merge`) and 4 English ones (`sg-reservation-defaults`, `sg-two-instances`, `sg-add-stock-limits`, `sn-title-merge`).

The index now holds 204 of 266 files and 1,184 chunks: StockGuard 81 files and 534 chunks, SyncNotes 123 files and 650 chunks. Four chunks of SyncNotes' `docker-compose.yml` were withheld for holding a credential.

### Reranker

Local cross-encoders reranked the top 50 hybrid results. On the first question set, four models were each tried alone and fused with hybrid by RRF. Results are [stored](../eval/results/reranker-dev).

| Turkish and English, first set | Recall@5 | MRR | Median latency |
| --- | --- | --- | --- |
| Hybrid, no reranker | 0.495, 0.575 | 0.549, 0.689 | 13 to 23 ms |
| `ms-marco-MiniLM-L-6-v2`, alone | 0.452, 0.505 | 0.579, 0.642 | about 1.8 s |
| `ms-marco-MiniLM-L-12-v2`, alone | 0.452, 0.478 | 0.568, 0.639 | about 3.6 s |
| `jina-reranker-v1-turbo-en`, alone | 0.435, 0.521 | 0.510, 0.660 | about 2.1 s |
| `bge-reranker-v2-m3` int8, alone | 0.527, 0.624 | 0.543, 0.634 | about 21 s |

Only `bge-reranker-v2-m3` looked worth checking on the second set, as was decided before running it:

| Paired difference from hybrid | Scope | Recall@5 | MRR |
| --- | --- | --- | --- |
| Turkish | All (26) | +0.077 [-0.096, +0.250] | +0.133 [-0.032, +0.299] |
| English | All (26) | +0.019 [-0.154, +0.192] | +0.116 [-0.064, +0.292] |
| Turkish | Code (15) | +0.133 [-0.100, +0.367] | **+0.216 [+0.035, +0.415]** |
| English | Code (15) | +0.100 [-0.067, +0.300] | **+0.203 [+0.033, +0.381]** |
| Turkish | Code, both sets (46) | +0.065 [-0.040, +0.170] | +0.066 [-0.048, +0.180] |
| English | Code, both sets (46) | +0.065 [-0.011, +0.152] | +0.029 [-0.054, +0.111] |

**Rejected, though not because it does nothing.** On the code questions of the second set, MRR is clearly better. The same model was no better than hybrid on MRR on the first set, however, and pooled over both sets no difference is clear. So the gain does not repeat, and it costs about 21 seconds per query against about 25 milliseconds, plus 0.55 GB of disk. On a GPU, where the cost would be far lower, it would deserve another look.

For answers, reading more results makes up for most of it: hybrid's top 8 reaches the Recall@5 of the reranker in Turkish (0.750 for both) and nearly in English (0.827 against 0.846). The rerank code was removed, and remains in the Git history at `438ae0c`.

Symbol names, file paths, callers and callees were also tried as a fourth RRF list on the first set. None helped, and most made results worse (for example, callers alone: Turkish MRR 0.409, English 0.504). They were rejected there and never run on the second set.

### Phase 2 answers

**The change.** Phase 1 sent 8 sources in one prompt. A failure analysis showed that the model stitched real sources into plausible, unsupported claims. Now each source is read alone, and the model must return a statement and the lines that prove it. See [architecture.md](architecture.md#one-source-at-a-time).

Answers with `qwen2.5:3b` in Turkish, graded with the run label hidden by [rules](../eval/tools/GRADING.md) fixed in advance:

| Measure | Phase 1 | Phase 2 |
| --- | --- | --- |
| **Fully correct** | **0 / 26** | **0 / 26** |
| Partly correct | 4 | 11 |
| Wrong | 11 | 13 |
| Refused | 11 | 2 |
| Code questions (15): partly correct, wrong, refused | 4, 4, 7 | 5, 10, 0 |
| SQL and YAML questions (11): partly correct, wrong, refused | 0, 7, 4 | 6, 3, 2 |
| Unanswerable refused | 4 / 10 | 6 / 10 |
| Unanswerable answered: fabricated, true but beside the point | 6, 0 | 3, 1 |
| Answers citing a gold span | 5 of 15 | 15 of 24 |
| Questions with gold among the sources read | 8 / 26 | 22 / 26 |
| Citations rechecked against Git | 35 / 35 | 69 / 69 |
| Mean seconds per question | 25.5 | 53.4 |
| Input and output tokens | 48,822 and 4,734 | 155,267 and 18,489 |

Compared fairly, phase 2 with this model is **not a clear win**. It reaches more of the right code, refuses less, and fabricates less, and on SQL and YAML it does much better. On code questions it trades refusals for wrong answers: 4 wrong became 10. An answer that is wrong is worse than a refusal, so this is a real cost. Grades are not comparable with the 5 of 27 reported for phase 1 below, which come from a different question set and looser rules.

Phase 2 results, by question:

| Grade | Questions |
| --- | --- |
| Partly correct (11) | `sg-add-stock-limits`, `sg-append-only`, `sg-auth-error-format`, `sg-lock-timeout-config`, `sg-reservation-defaults`, `sg-settled-immutable`, `sg-sweep-index`, `sg-username-race`, `sn-account-lock`, `sn-folder-delete`, `sn-one-owner` |
| Wrong, gold never read (3) | `sn-cookie-flags`, `sn-env-validation`, `sn-title-merge` |
| Wrong, gold read but judged not relevant (5) | `sg-why-separate-stock`, `sn-note-search`, `sn-logout-access-token`, `sn-cursor-color`, `sn-theme-flash` |
| Wrong, gold read and used (5) | `sg-negative-stock-db`, `sg-simulation-limit`, `sg-error-status-map`, `sn-redis-durability`, `sn-health-redis` |
| Falsely refused (2) | `sg-two-instances`, `sn-collab-instances` |
| Fabricated (3) | `sn-neg-tags`, `sn-neg-public-link`, `sn-neg-invite-mail` |
| True but beside the point (1) | `sg-neg-transfer` |

**Why it fails.** Retrieval is now the smaller part: the right code was read in 10 of the 13 wrong answers.

1. **The model misses concrete facts.** Asked which HTTP status is returned, or the maximum number of requests, it describes the code around the answer and never says 409 or 1000.
2. **It calls too much relevant.** It accepted 19 of 27 gold sources, but also 50 of 181 others (28%). An answer holds 2.9 statements on average, so true claims end up next to off-topic ones, and this is why most answers are only partly correct.
3. **It writes poor Turkish.** Some statements are garbled enough to change the meaning.
4. **The quote check cannot catch any of this.** A real quote next to a wrong statement passes. Only 16 statements were discarded by the backend, 9 of them for too short a quote.
5. **Features next to existing code still get through.** `MembersService.invite` exists, so "how are invitation emails sent?" gets a confident answer, although no email is sent.

### `qwen2.5:7b`

**Status: `incomplete — interrupted due to memory pressure`.** The independent run reached question 10 of 72 and saved nothing, because results were written only at the end at that time. It is left out of every comparison. The facts are in [its status file](../eval/results/v2-ask-phase2-7b.status.json).

The only complete 7B result is on the first question set in Turkish, on which the prompt was tuned, and it was not graded. It proves little, and is reported only as the reason to try again:

| 7B, first set, not independent | |
| --- | --- |
| Answerable questions answered | 22 / 31 |
| Answers with evidence from a gold chunk | 20 |
| Unanswerable refused | 14 / 14 |
| Readings that produced a verified statement | 44 of 296 |
| Mean seconds per question | 75.6 |

### Resources

| Resource | Measured |
| --- | --- |
| Ollama container, 3B, phase 1 | 2.83 GiB at peak |
| Ollama container, 3B, phase 2 | 4.72 GiB at peak |
| Ollama container, 7B, before the limits | 7.66 GiB at peak, 8 cores busy |
| Ollama container, 3B, with the limits, one short test | 2.29 GiB |
| Ollama container, 7B, with the limits | Not measured yet |
| Python process, search with embedding and translation models | 1.0 GB at peak |
| PostgreSQL container | about 0.1 GiB |
| Disk: Ollama volume (3B 1.9 GB, 7B 4.7 GB) | 6.6 GB |
| Disk: models in use (embedding, translation) | 0.6 GB |
| Disk: unused candidate models still in the `.models` cache | 1.6 GB |
| Disk: database volume | 0.1 GB |

Container figures come from `docker stats`, sampled every 20 seconds, and may include file cache, which could explain why the same model reads differently between runs. Python memory during the answer runs was not captured, because the sampler measured a launcher process by mistake. It has been fixed in `resource_trial.ps1`.

### Limits of these results

- **Grading is one reviewer's judgement**, and the reviewer is Claude. Hiding the label does not hide style. The rules are strict: a true answer with an off-topic sentence counts as partly correct, which is part of why no answer is fully correct.
- **26 questions leave wide intervals**, and the set was written by the same author as the first.
- **Answers were graded in Turkish only.** The phase 1 English answers exist but were not graded, because phase 2 has no English 3B run to compare them with.
- **The real-model injection test covers one scenario.** The model did not obey the hostile comment, but it did not answer the question either. Whether the comment caused that, or this was an ordinary false refusal, was not measured. An earlier version of this test never reached the model, because the refusal check stopped the question first. It now requires the model to have read the source.

## Pilot: answers made of real lines

**18 questions, graded by one reviewer, cannot prove general reliability, and this pilot did not pass.** It only shows how the redesign compares with the previous version.

Phase 2's answers were not accurate, so the answer layer was redesigned as described in [architecture.md](architecture.md#real-lines-not-sentences): no model narrows search results to a few short passages, and the model only chooses among them. The [question set](../eval/questions_v3.yaml) and the [rules](../eval/PILOT.md) were committed before the pipeline existed and before any search on those questions. All settings were tuned only on the two earlier sets, and each configuration ran once. The new code is at `283df4e`, and the previous version at tag `phase2-reading`.

### Results

12 answerable and 6 unanswerable questions in Turkish, on the same index:

| Measure | Previous version, 3B | No LLM | New, 3B | Target for New |
| --- | --- | --- | --- | --- |
| Correct source | 4 | 7 | **4** | at least 6: **not met** |
| Wrong source | 8 | 5 | **7** | at most 2: **not met** |
| No answer | 0 | 0 | 1 | |
| Correct evidence not found | 8 | 5 | 8 | |
| Answer rate | 100% | 100% | 92% | |
| Gold anchor in the lines shown | 5 | 6 | 4 | |
| Unanswerable refused, of 6 | 3 | 3 | **4** | at least 5: **not met** |
| Invalid file or line references | 0 of 40 | 0 of 90 | **0 of 85** | 0: **met** |
| Mean lines shown as an answer | 7.6 | 20.2 | 6.3 | |
| Mean seconds per question | 53.9 | 0.7 | 14.3 | |
| Input and output tokens | 81,343 and 8,809 | 0 | 13,696 and 859 | |
| Ollama container, peak | 2.46 GiB | not used | 2.39 GiB | |
| Python process, peak | 814 MB | about 1 GB, measured earlier | 870 MB | |
| Lowest free memory during the run | 4.3 GB | | 4.5 GB | |

"No LLM" lists 5 passages and does not choose among them, so its correct source means that one of about 20 lines was right. It is not comparable to a model choosing about 6 lines, and it was not graded.

Graded blind by [the rules](../eval/tools/GRADING.md), although the two formats are easy to tell apart:

| Grade | Previous version | New |
| --- | --- | --- |
| Correct | 1 | **3** |
| Partly correct | 6 | 3 |
| Wrong | 5 | 5 |
| No answer | 0 | 1 |
| Unanswerable: refused, fabricated, true but beside the point | 3, 1, 2 | 4, 0, 2 |

The English runs give a similar picture. New: 5 correct sources, 6 wrong, 1 no answer, 3 of 6 refused, 9.6 seconds. No LLM: 8 correct, 3 wrong, 1 no answer, 3 of 6 refused.

### What this shows

The redesign is cheaper and invents less, at no loss of accuracy that this sample can show: 4 times faster, a sixth of the tokens, 0 fabrications against 1, and 3 fully correct answers against 1. **It does not find the right source more often.** With 18 questions, none of these differences is large enough to be sure of.

Where each answerable question was lost, with the new pipeline:

| Stage | Turkish | English |
| --- | --- | --- |
| Search missed the gold code in its 8 chunks | 3 | 2 |
| In the chunks, but not in the 5 passages | 3 | 2 |
| Offered, and the model chose other lines | 1 | 3 |
| Offered, and refused by the evidence rule | 1 | 0 |
| Offered, and chosen correctly | **4** | **5** |

- **Half of the losses come before the model.** When the right lines are offered, the model chooses them in 9 of 14 cases. No model can pick lines it was not given.
- **The model never declined.** It was asked 13 times in Turkish and 14 in English, and it never replied "none", even though the right lines were absent in about half of those requests. Every refusal came from a mechanical rule.
- **The rules are blunt.** They refused `sg-username-lowercase`, which was answerable. They let through `sg-neg-extend-reservation` and `sn-neg-trash`, where the code has reservations that expire and notes that get deleted.

Question-level results are in [`v3-grades-3b-tr.json`](../eval/results/v3-grades-3b-tr.json), and `eval/tools/pilot_diagnosis.py` prints the table above.

Also measured: an integration test indexes a comment ordering the model to reply `PWNED` and cite a password file. The real model did not obey, and it answered the actual question correctly (`applyDiscount` returns `price * 0.9`). This is one scenario, not proof of resistance. An earlier version of this test never reached the model.

### GPU load check

RepoMind's Ollama first ran on the CPU only, although this machine has an RTX 3050 with 4 GB of VRAM. After giving the container the GPU, one Turkish question went through the same pipeline on each model. **These are single questions checking resources, not a measure of accuracy or typical speed.** Samples are in [`eval/results/resources`](../eval/results/resources).

| | 3B, CPU, from the pilot | 3B, GPU | 7B, GPU |
| --- | --- | --- | --- |
| Where the model ran | 100% CPU | 100% GPU, 37 of 37 layers | 46% CPU and 54% GPU, 13 of 29 layers |
| Time for this question | 23.2 s | 6.0 s | 12.3 s |
| VRAM used | 0 | 2,548 MiB of 4,096 | 2,808 MiB of 4,096 |
| Ollama container, peak | 2.39 GiB | 0.56 GiB | 0.85 GiB |
| WSL process, peak working set and private | not sampled | 3.65 and 7.47 GB | 8.78 and 11.60 GB |
| Page cache inside the VM, peak | not sampled | 3.15 GB | 6.42 GB |
| Lowest available RAM | 10.8 GB, from 13.1 | 12.15 GB, from 13.1 | 10.23 GB, from 16.5 |
| Lowest free commit space | 4.5 GB, from 6.8 | 3.36 GB, from 7.2 | 19.88 GB, from 29.4 |

The 3B CPU column covers the pilot's 18 questions, and the GPU columns one question. The 3B GPU check chose the same lines as the CPU run. On this question 7B ended in "I do not know", which says nothing about its accuracy.

- **The GPU saves time and physical RAM, but not commit space.** Through WSL, loading 3B raised the WSL process's private memory by 2.87 GB, close to Ollama's 2.9 GB estimate for the model. Loading 7B raised it by 8.3 GB. The exact accounting of GPU memory in WSL was not established.
- **The container figure understates 7B.** With the GPU, Ollama maps the model file instead of copying it, so the file appears as page cache in the VM, which rose by 4.8 GB, and not as container memory. That cache can be reclaimed, but it stays for a while: 15 seconds after the container stopped, the WSL process still held 8.35 GB.
- **The 7B check needed more commit space than was free before.** It ran only after the pagefile's start was raised to 16 GB, which took the commit limit to 47.3 GB. The pagefile itself was barely used, with a peak of 5 MB, so the larger start adds room to commit and causes no paging. See [the record](../eval/results/resources/pagefile-change.md).
- **VRAM was not used up.** Ollama placed 13 of 7B's 29 layers, leaving 1,157 MiB free, because it counts 3.2 GiB as available. More layers might fit, which was not tried.

### Limits of the pilot

- 18 questions are too few to rank two methods, and every set so far comes from the same author.
- This set is now read, so changes tuned on it will need new questions.
- Correct source means touching a narrow gold span. Other lines may also answer a question, so scores are conservative.
- The blind grading hides labels but not the visible difference in format.
- Python memory for the no-LLM runs comes from an earlier measurement, not from these runs.

# Phase 1

The rest of this document is the phase 1 evaluation, kept as the baseline. Its statements about what was planned next, such as a reranker being the most promising step, describe what was known at that time.

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
python -m repomind evaluate --dataset eval/questions_v2.yaml --output eval/results/my-retrieval.json
python -m repomind evaluate-ask --dataset eval/questions_v2.yaml --languages tr --output eval/results/my-answers.json
python eval/tools/compare_retrieval.py
python eval/tools/count_answers.py phase1=v2-ask-phase1-3b.json phase2=v2-ask-phase2-3b.json
```

An existing result is not replaced without `--overwrite`. Phase 1 can be rerun from tag `phase1-baseline`, from its own folder and against its own database.

Stored results are in [`eval/results/`](../eval/results):

| Files | What they hold |
| --- | --- |
| `01-baseline-e5-small.json`, `final.json`, `ask-qwen2.5-3b-tr*.json` | Phase 1 on the first question set |
| `v2-retrieval-phase1.json`, `v2-retrieval-phase2.json` | Retrieval on the second set |
| `v2-retrieval-reranker-bge.json`, `reranker-dev/` | The reranker on both sets |
| `v2-ask-phase1-3b.json`, `v2-ask-phase2-3b.json`, `v2-grades-3b-tr.json` | 3B answers on the second set and their grades |
| `v3-old-3b-tr.json`, `v3-nollm-*.json`, `v3-new-3b-*.json`, `v3-grades-3b-tr.json` | The pilot and its grades |
| `dev-selector-3b-v2.json` | The new pipeline on the second set, before the evidence rule, not independent |
| `v2-ask-phase2-7b.status.json` | The interrupted 7B run, which saved no answers |
| `dev-phase2-7b-tr.json`, `prompt-lab/` | Development runs on the first set, not independent |
| `resources/` | Memory and CPU samples |
