# Rules for the fifth question set

**Result: it did not pass.** See [the results](../docs/evaluation.md#fifth-question-set). The rest of this page is as committed before the run.

These rules were committed with [`questions_v5.yaml`](questions_v5.yaml), before any search or model run on those questions.

**24 questions, written by the same developer who built the system and graded by one reviewer, are not an independent external test.** They were written from code regions no earlier gold span covers, and no setting was tuned on them, so this is the fairest measure available here. It cannot prove general reliability.

## What is measured

The system exactly as it stands at tag `v5-evaluated`, on the same index and commits:

- `qwen2.5:7b` on the GPU, with the current translation prompt.
- The strict evidence rule.
- Sources to review shown apart, and never counted as answers.

It runs once, in Turkish. An interrupted run continues from its checkpoint. No question, span, rule, prompt or setting changes after this commit, and nothing in the system changes because of the result. The earlier question sets and their results stay as recorded.

## Outcomes

As on the fourth set, computed from the results:

- **Correct source:** the lines shown as the answer touch a gold span or an acceptable span.
- **Wrong source:** lines were shown as the answer, and none touches either.
- **No answer:** the system said it does not know, or gave no valid reply. On an answerable question this is an unnecessary refusal, whatever caused it.

Unanswerable questions are either **refused** or **answered**.

**Refusing is not a way to pass.** The bar needs correct sources as well as few wrong ones, and the answer rate is reported.

## The bar

The same proportions as before, on 16 answerable and 8 unanswerable questions:

| Measure | Target |
| --- | --- |
| Correct source, of 16 | at least 8 |
| Wrong source, of 16 | at most 2 |
| Unanswerable refused, of 8 | at least 7 |
| Invalid file or line references | 0 |

## Also reported

**Where each answerable question was lost:**

- **Search:** the gold code is not in the search candidates.
- **Window selection:** it is in the candidates, and not in the 5 passages.
- **The model's choice:** it was offered, and the model chose other lines, or declined.
- **The evidence rule:** it was offered, and the rule refused before the model, or turned down the model's choice of it.

**Sources to review:** how many there are, and whether their lines answer the question, graded without labels. They do not change any outcome above.

**Lines existing and lines answering are reported apart.** Invalid references say whether lines exist at the commit. Correct, partly correct and wrong say whether they answer, by [the grading rules](tools/GRADING.md), with labels hidden, by one reviewer.

**Also:** mean seconds per question, memory, GPU and token use.

## After this

Whatever the result, no further question set, model comparison, reranker, prompt or threshold tuning follows from it, and the code does not change because of it. The result is recorded with its known problems, and the next phase is the product.
