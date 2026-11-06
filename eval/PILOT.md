# Pilot rules

These rules were committed with [`questions_v3.yaml`](questions_v3.yaml), before the pipeline they measure was built and before any search was run on those questions. **A pilot of 18 questions, graded by one reviewer, cannot prove general reliability.** It can only show whether the new design is worth continuing.

## What is compared

All on the same questions, at the same commits, against the same index:

| Name | What it is | Languages |
| --- | --- | --- |
| Old | The per-source reading pipeline from commit `41c77e3`, with `qwen2.5:3b` | Turkish |
| No LLM | The new evidence selection alone | Turkish, English |
| New | The new evidence selection with `qwen2.5:3b` choosing lines | Turkish, English |

The 7B model is not part of this pilot. It needs separate approval.

Each configuration runs once. An interrupted run continues from its checkpoint. No question, gold span, prompt, threshold or pipeline setting changes after the first run on this set. Prompts and windows may be tuned only on the two earlier question sets.

## Outcomes

Each answerable question gets exactly one outcome, computed from the results, not judged:

- **Correct source:** the lines shown as the answer touch a gold span.
- **Wrong source:** lines were shown as the answer, and none touches a gold span.
- **No answer:** the system said it does not know, or gave no valid reply.

"Correct evidence not found" counts wrong source and no answer together, and is always reported.

Each unanswerable question is either **refused** or **answered**. For No LLM, which has no model to refuse, a question counts as refused only when the system itself reports insufficient evidence. Lines listed as closest code on a refusal are not an answer.

## The bar

For New, in Turkish:

| Measure | Target |
| --- | --- |
| Wrong source, of 12 | at most 2 |
| Unanswerable refused, of 6 | at least 5 |
| Invalid file or line references | 0 |
| Correct source, of 12 | at least 6 |

The first three targets were agreed with the project owner as a starting goal. The last was added when these rules were written, so that a system cannot pass by refusing almost everything. A run below it is reported as not passing, even if the other three are met.

## Also reported

- Correct, partly correct and wrong answers, graded blind by [the grading rules](tools/GRADING.md). For answers made of code lines, lines that visibly show the main point of the note count as saying it. A false model note lowers the grade, even though it is labelled.
- Whether the chosen lines contain the gold anchor, which is stricter than touching the span.
- Answer rate and refusals of answerable questions.
- Mean seconds per question.
- Peak memory of the Ollama container and the Python process.
