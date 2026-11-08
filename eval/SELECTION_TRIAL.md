# Choosing at once against judging each passage

Written before the full trial ran, and not changed after it. The tool is [`selection_trial.py`](tools/selection_trial.py).

## What is compared

- **choose**, the default: the model sees all passages for a question in one request and picks among them.
- **judge**, experimental: one request per passage, which the model labels as answering, calling, observing or unrelated. Only lines labelled as answering, and passing the evidence rule, become the answer.

Both ways get the same questions, the same stored 7B translations and the same passages, from the same search and the same whole-unit windows. Only the selection differs. The tool records the offered passages for both ways, and reports any question where they differ.

## Questions

32 questions, all of them already read, so this is **development data and not an independent result**:

- the fifth set, 16 answerable and 8 unanswerable;
- 4 earlier answerable questions whose right lines were offered and whose choice went wrong or empty, and 3 earlier unanswerable questions that whole units newly let reach the model;
- the negative-stock question that started this work. Its accepted lines are the CHECK constraints in `V2__catalog_and_stock.sql:23-25` and the guard in `StockLevelRepository.java:57-58`. **It is reported on its own and left out of the totals.**

So the totals cover 20 answerable and 11 unanswerable questions.

## Outcomes

For each way: correct source, wrong source and no answer of 20; refusals while the right lines were offered; unanswerable questions given a source, of 11; invalid replies. "Wrong in all" is wrong sources plus unanswerable questions given a source.

Cost, for each way: seconds per question (mean, median, highest), model requests, input and output tokens, peak GPU memory in use, and the lowest free physical memory and commit space during a question.

## What decides

`judge` becomes the default only if all of these hold:

1. **Correct sources:** judge ≥ choose.
2. **Wrong in all:** judge ≤ choose − 2. A difference of one question on this many is noise.
3. **Refusals while the right lines were offered:** judge ≤ choose.
4. **Invalid replies:** judge ≤ choose + 1.
5. **Cost:** judge's median seconds per question ≤ 2 × choose's, and peak GPU memory stays under 4096 MiB.

If 1 to 4 hold and 5 does not, judge does not become the default as it is. Making it cheaper would be the next step, decided with the user. If any of 1 to 4 fails, choose stays the default and judge stays experimental.

Fewer wrong sources alone is not success, and neither is the negative-stock question changing.

## What this does not measure

- **The whole-unit windows.** Both ways use them. The evidence for keeping them is the no-LLM measurement in [`dev-accuracy-baseline.json`](results/dev-accuracy-baseline.json) and [`dev-accuracy-units.json`](results/dev-accuracy-units.json). The choose rows can also be set beside the recorded fifth-set run, which had the older windows and the same translations. That comparison is only descriptive: another day, and the guarded project-name rule has been added since.
- **New questions.** Every question here has been read.

## The earlier partial run

A first run stopped after 15 questions, and its rows are in [`dev-selection-partial-unchecked-memory.log`](results/dev-selection-partial-unchecked-memory.log). Its start check read memory once and did not print the reading. Free memory had been 9.9 GB a few minutes before, and 8.5 to 8.8 GB during the run, so it may have started below the 12 GB limit. It is not part of this comparison, and the full trial starts again from nothing. Its figures were seen before these rules were written: on those 15, choose had 9 correct and 2 wrong, judge 8 correct and 1 wrong, and judge took about three times as long.

## Safety

The trial starts only when two readings a second apart each show at least 12 GB of free physical memory and 12 GB of free commit space, and prints both. It stops between questions below 4 GB, and saves every finished question.

## Result, added after the run

The rules above are as they were. Rows: [`dev-selection-trial.json`](results/dev-selection-trial.json). It started at 12.06 and 12.07 GB of free physical memory and 17.07 GB of commit space, and finished all 32 questions. The passages were the same for both ways on every question.

| Of 20 answerable, 11 unanswerable | choose | judge |
|---|---|---|
| Correct source | 10 | 10 |
| Wrong source | 3 | 1 |
| No answer | 7 | 9 |
| Refused while the right lines were offered (of 15) | 3 | 5 |
| Unanswerable given a source | 2 | 0 |
| Wrong in all | 5 | 1 |
| Invalid replies | 0 | 0 |
| Seconds per question, median (mean, highest) | 5.7 (5.9, 14.9) | 19.4 (19.3, 26.5) |
| Model requests per question | 0.97 | 4.84 |
| Input / output tokens per question | 1023 / 29 | 2937 / 182 |
| Peak GPU memory in use | 2814 MiB | 2814 MiB |
| Lowest free physical memory / commit space | 5.11 / 7.40 GB | 5.08 / 7.22 GB |

| Rule | |
|---|---|
| 1. Correct sources: judge ≥ choose | holds, 10 = 10 |
| 2. Wrong in all: judge ≤ choose − 2 | holds, 1 against 5 |
| 3. Refusals while the right lines were offered: judge ≤ choose | **fails**, 5 against 3 |
| 4. Invalid replies: judge ≤ choose + 1 | holds, 0 = 0 |
| 5. Cost: median ≤ 2 × choose, GPU under 4096 MiB | **fails**, 3.4 times as long |

**Choose stays the default, and judge stays experimental.**

Where they differ, on development questions that were read before:

- **Judge removed wrong sources:** two unanswerable questions (`v5-sg-neg-price`, `v5-sn-neg-google`), where it labelled the lines choose picked as only calling the feature; and `v5-sg-sku-locked`, where it labelled the right lines as answering but the evidence rule turned them down, so it said it does not know.
- **Judge found right lines choose missed:** `v2-sg-error-status-map` and `v2-sn-redis-durability`.
- **Judge lost right answers choose gave:** `v5-sg-api-language`, whose YAML lines it labelled as observing, and `v5-sg-simulation-default`, where its one unreadable reply of 155 was for the passage with the answer. On the partial run that question was correct, so judge is not stable from run to run either.
- **The evidence rule turned down right lines** that the model picked, with no correct answer left: 2 questions for each way (`v5-sn-log-format` and `sg-oversell` for choose, `v5-sg-sku-locked` and `sg-oversell` for judge).

The negative-stock question, apart from the totals: both ways chose the CHECK constraints in `V2__catalog_and_stock.sql:14-27`, which now reaches the model as the whole `CREATE TABLE`, and neither chose `SimulationService`. Judge also chose the guard in `StockLevelRepository.java:50-61`, which was offered to both. This is one question with a fixed English wording, and does not show a general gain.

The fifth set's choose rows beside the recorded run with the older windows, descriptive only: right lines offered 10 to 11 of 16, correct 8 to 9, wrong 4 to 2, no answer 4 to 5, unanswerable given a source 2 to 2 of 8.
