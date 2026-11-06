# Rules for the fourth question set

These rules were committed with [`questions_v4.yaml`](questions_v4.yaml), before the search changes it measures and before any search on those questions.

**24 questions, written by the same developer who built the system and graded by one reviewer, cannot prove general reliability.** This is the first result on questions that no setting was tuned on since the answer redesign. It is not fully independent.

## What is compared

Turkish questions only, on `qwen2.5:7b` with the GPU, at the same commits and on the same index:

| Name | What it is |
| --- | --- |
| Baseline | The pipeline at tag `phase3-baseline`, which is the pipeline that ran the last pilot |
| Improved | The pipeline after the search and passage changes |

Each runs once, after the Improved pipeline is committed. An interrupted run continues from its checkpoint. No question, span, prompt, threshold or setting changes after that. All tuning uses only the three earlier question sets.

The earlier sets are not independent of this work, and are reported as such. The pilot's official result stays as recorded.

## Outcomes

As in [the pilot](PILOT.md), computed from the results:

- **Correct source:** the lines shown as the answer touch a gold span or an acceptable span.
- **Wrong source:** lines were shown as the answer, and none touches either.
- **No answer:** the system said it does not know, or gave no valid reply.

Unanswerable questions are either **refused** or **answered**.

## The bar

The same proportions as the pilot, on 16 answerable and 8 unanswerable questions, rounded towards the stricter side:

| Measure | Target |
| --- | --- |
| Correct source, of 16 | at least 8 |
| Wrong source, of 16 | at most 2 |
| Unanswerable refused, of 8 | at least 7 |
| Invalid file or line references | 0 |

## Also reported

- Where each failure happened: search, passage selection, or the model's choice.
- How often the model said "I do not know" when the right lines had been offered.
- Correct, partly correct and wrong answers, by [the grading rules](tools/GRADING.md), with run labels hidden.
- Mean seconds per question and peak memory.
- Whether Improved beat Baseline by more than the few questions that could be chance on a set this small.
