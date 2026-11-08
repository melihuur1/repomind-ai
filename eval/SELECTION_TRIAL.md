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
