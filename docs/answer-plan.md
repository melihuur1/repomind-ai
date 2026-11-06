# Plan for the answer layer

**Status: steps 1 and 2 are built, and step 3's pilot ran on `qwen2.5:3b` and did not pass.** See [the results](evaluation.md#pilot-answers-made-of-real-lines) and [what comes next](#after-the-pilot).

The analysis below came first, and is kept as written. With `qwen2.5:3b`, none of 26 answers was fully correct. It uses stored results and one short probe with no LLM ([`replay_evidence.py`](../eval/tools/replay_evidence.py), [`passage_probe.py`](../eval/tools/passage_probe.py), which has since been changed to use the built selection). Both question sets had already been read, so these numbers guide the design and do not measure it.

## What the measurements say

### Why do unrelated sources get into answers?

- `ask` always reads 8 whole chunks, about 110 lines per question, although most useful sources come early: 23 of the 27 gold sources read by 3B, and 34 of 44 read by 7B, were among the first 4.
- The prompt says "If you are unsure, answer true", which was added to reduce false refusals.
- Nothing removes a statement afterwards. Everything that passes is joined.
- The models differ in what they reject, not in what they accept:

| Sources that produced a statement | Gold sources | Other sources |
| --- | --- | --- |
| 3B, second question set | 19 of 27 (70%) | 50 of 181 (**28%**) |
| 7B, first question set, which the prompt was tuned on | 32 of 44 (73%) | 12 of 204 (**6%**) |

### Can relevance be checked by rule?

Only at a high cost. Requiring two question terms in the quote raises the gold share of 3B statements from 0.28 to 0.52, but answered questions fall from 24 to 11, including 4 partly correct ones. Rank is no help either: 3B made 42 statements from ranks 1 to 4 and 27 from ranks 5 to 8.

So the better move is to give the model less to judge, not to add thresholds after it.

### Can claims not in the source be blocked?

Not by filtering sentences:

- Requiring every technical term to be in the quote rejects 38 of 75 statements, but also 13 of 32 good 7B statements, which name a class or a product the quote does not contain.
- Requiring only the numbers to be in the quote removes a single wrong answer. 3B rarely invents a number. It fails to state one.

A sentence cannot be verified mechanically, so a sentence should not be the answer.

### Can concrete values be extracted reliably?

The answers are often written in the code already: `INSUFFICIENT_STOCK(HttpStatus.CONFLICT)`, `@Max(1000)`, `lock_timeout = '3s'`, and comments such as "Notes inside the folder are not deleted". The probe cut the same 8 sources into 3-line windows and ranked them with the local embedding model and shared terms, with no LLM, in under a second per question. It counts a question as found when a gold anchor, a text verified to be in the gold code, is in a top window:

| Question found by | Second set, TR | Second set, EN | First set, TR | First set, EN |
| --- | --- | --- | --- | --- |
| Anywhere in the 8 sources | 21 of 26 | 21 of 26 | 25 of 31 | 27 of 31 |
| Top 3 windows, at most 9 lines | **17** | 14 | 11 | 14 |
| Top 5 windows, at most 15 lines | 18 | 18 | 12 | 17 |
| A quote the model chose | 3B: **11** | not run | 7B: 12 | not run |

A simple ranker finds the proof as often as the models do, and more often than 3B. In 8 of the 13 answers graded wrong, the proof was in the top 3 windows. This is not a complete solution: at 5 windows about 55% of questions get their proof, and broad questions on the first set do poorly. An anchor is also only one phrase, so a useful window can count as a miss.

### Can refusals be decided by a threshold?

No. Neither signal separates unanswerable questions, which sit next to real code:

| Signal | Second set, EN | First set, EN | Second set, TR | First set, TR |
| --- | --- | --- | --- | --- |
| Best window's share of question terms, answerable and negative medians | 0.46, 0.50 | 0.67, 0.46 | 0.35, 0.37 | 0.50, 0.41 |
| Best cosine, how well it ranks answerable above negative (AUC) | 0.57 | 0.81 | 0.49 | 0.66 |

Refusing therefore stays a judgement. Requiring one question term in the quote helps a little: on 3B, answered negatives fall from 4 to 2 with no partly correct answer lost, though on 7B answers with gold evidence fall from 20 to 17. Features next to existing code remain the hard case. `MembersService.invite` exists, while invitation emails do not.

## The design: the answer is real lines

```
question → hybrid search, 8 chunks → best 5 windows, no LLM → model picks from them, one request
         → chosen lines from Git + at most one sentence, clearly labelled as the model's reading
         → or "I do not know" + the closest lines
```

| | Today | Proposed |
| --- | --- | --- |
| Model reads | About 110 lines, 8 requests | At most 15 lines, 1 request |
| Model's job | Write a statement about each source | Choose lines, or none |
| The answer is | The model's sentences, with quotes | The code's own lines |
| A misread sentence | Becomes the answer | Sits under the real line, labelled |
| Without an LLM | Closest chunks | The same best lines |

This removes a prompt rule and 7 of 8 requests rather than adding anything. The expected time and token savings have not been measured.

## Three steps

1. **Narrow the evidence, without an LLM.** Move the probe's window ranking into `repomind/qa/passages.py` and use it in `ask`. Every window's lines are checked against Git. This also improves `ask` when no LLM is configured.
2. **Make the model a judge, not an author.** One request lists the windows and asks which of them answer the question, with "none" as an ordinary reply. Remove "If you are unsure, answer true". Show the chosen lines themselves. Keep a sentence only if its numbers are in those lines and the quote shares a question term. If the model chooses none, say "I do not know".
3. **Decide with a small new question set.** Write 12 answerable and 6 unanswerable questions, and commit them before the first run. Measure in order and stop when a step fails: the ranker alone, then 3B, then 7B if memory allows. Agree on the bar in advance. A proposal: at most 2 wrong of 12, at least 5 of 6 negatives refused, and every line verified.
   - If 3B passes, keep it.
   - If only 7B passes, make it the default.
   - If neither passes, ship `ask` as "show me the lines" with no model sentence, and consider a stronger API model with the owner's approval.

A later option, only if step 3 shows values being missed: index configuration keys, validation annotations and SQL constraints as structured facts.

## After the pilot

The pilot met one target of four. It also changed what the plan assumed:

- **Passages are longer than planned:** about 20 lines in total, not 15. On the earlier sets, this reached noticeably more of the gold.
- **Adding the search rank as a third list, and letting neighbouring windows merge,** took the first set from 12 to 17 of 31 in Turkish. The analysis above predates both.
- **Expecting the model to say "none" was wrong for 3B.** It never did, so refusals now rest on a mechanical rule.

`qwen2.5:7b` then ran on the same 18 questions, with no tuning. It missed the bar by one wrong source. It became the default, search and passage selection were tuned on the three earlier sets, and a [fourth question set](evaluation.md#fourth-question-set) measured the result once. **That also missed the bar:** 6 correct sources of 16 and 3 wrong ones, and the search changes showed no clear gain over the baseline.

[Later search work](evaluation.md#search-work-after-the-fourth-set), with no model, took the right code reaching the passages from 61 to 68 of 85 questions already read. That is not independent. Any claim about it needs a new question set, because all four have been read.

[Two short trials](evaluation.md#two-short-trials) then tested the evidence rule and glossary terms in the translation prompt. Neither passed its bar, so the strict rule and the original prompt stay. The next step is the owner's decision. A fifth question set has not been written.

## Trying 7B safely

The rules are in [`resource_trial.ps1`](../eval/tools/resource_trial.ps1):

- It does not start with less than 12 GB of free physical memory and commit space. That is the container's 8 GB limit, 1 GB for Python and 3 GB of margin.
- It stops between questions below 4 GB, and saves every question as it finishes.
- It loads one model with one request slot, downloads nothing, starts only RepoMind's container, and stops it afterwards.
- Other projects' containers and Claude Code's memory guard stay as they are.

The trial should wait for step 2. The current pipeline takes about 100 seconds per question on 7B, and its results would describe code that is about to change. With one request per question, the same trial should be much shorter.

## Using a stronger API model later

No paid API or account is needed for any of this, and nothing is built until a provider is chosen.

**Already in place.** Every model call goes through `QAEngine._call`, behind the small `LLMClient` protocol, which has one method, `complete(system, user, max_output_tokens)`. Any OpenAI-compatible endpoint works today. Sending code to a remote endpoint must be enabled explicitly, and a budget is checked before each request. `search` and everything below it import nothing from `repomind/qa`, so search keeps working with no AI at all.

**Needed at that point:**

- A `REPOMIND_LLM_PROVIDER` setting, defaulting to `openai-compatible`, and one small class per provider that has a different API.
- JSON mode moved inside the client, because `response_format` is not universal.
- Retries with backoff on rate limits. A failed request already stops the run cleanly, and the checkpoint continues it.
- A printed worst-case cost before an `evaluate-ask` run.

The design above also helps here: one short reading request instead of 8 costs a fraction of today's tokens. Turkish questions still need one more request for translation.
