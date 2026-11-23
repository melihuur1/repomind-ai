# After the comparison: what the stored rows say

Written on 2026-11-15, after [V2_EVALUATION.md](V2_EVALUATION.md). Nothing here was measured anew. No question was asked again, searched again or traced again, and no question, accepted place, decision or threshold of the sixth set was changed. The result stands as it was recorded: **v2 was not shown to be more accurate than v1.**

Everything below comes from the rows in `eval/results/v6-*`, the prompts the tool kept, the model server's log, and the code. Where those do not hold an answer, this says so.

## 1. The empty reply to `v6-sg-replay-header`

### What is certain

| | Evidence |
|---|---|
| The model server answered both times with HTTP 200 | its log: 279 requests in the run, 279 with status 200 |
| It worked on the request for 6.44 s and 6.34 s | the same log. v1's request for this question took 4.51 s and 4.41 s |
| It reported 0 input tokens | the engine's own warning in the row: "The endpoint reports 0 input tokens for a prompt of about 1506" is only written for a number that was reported |
| The reply could not be used as a choice | the row's status, `invalid_response` |
| The prompt was the same in both runs | the kept prompts are identical |
| The prompt was ordinary | 3,167 characters of plain ASCII, five passages. Four other prompts of v2 were longer, up to 3,540, and were answered. The context is 8,192 tokens and this is about 1,000 |
| It was not a timeout, a refused connection or an HTTP error | each of those raises an error in the client, and none was raised. The timeout is 300 s |
| It was not the output limit as the endpoint counts it | a reply that runs into the limit of 250 tokens ends normally, with its counts. This one came with none |
| RepoMind's parsing did not cause the zero | the count comes from the endpoint's `usage`, before any parsing |

So the endpoint ended the reply without its closing figures. A reply that ends normally carries the number of input tokens.

### What is not known

- **The text of the reply.** The tool kept every prompt and no reply. Empty, only whitespace, or the start of something: the rows cannot say.
- **Why the endpoint stopped**, and what its `finish_reason` was. The client did not read it.
- **Whether v2's passages caused it.** v2 got it on 1 of 35 prompts, twice, and v1 on none of 35, twice. Both runs of v2 are the same request in the same order, so this is one observation, not two. It may be this prompt, the state of the server after the request before it, or both. Part B shows that the order of requests changes what the model returns for an identical prompt.

One explanation fits all of it and is not confirmed: in JSON mode a model can produce nothing but whitespace, and a server may stop such a generation without a normal end. Nothing in the log says so, because the server was not logging at debug level. It stays a guess.

**The question was not asked again**, as instructed, so none of this was tested against the model.

### What was wrong in RepoMind, and what changed

The answer itself was already handled safely: no source, the status `invalid_response`, a verdict of its own in the API. Three things were wrong around it.

| Before | Now |
|---|---|
| Every unusable reply got the same sentence, "The model did not return the required JSON", so an empty reply could not be told from a wrong one | `reply_problem` says which: `empty`, `cut_off`, `not_json`, `no_choice_list`, `not_offered`, or `no_judgement`. It is on `AskResult`, in the API and in evaluation rows |
| The reply was thrown away | its first 2,000 characters are kept in `AskResult.invalid_reply`, and in evaluation rows. It is not shown to a reader |
| 0 input tokens was read as a small count: the reader was told the prompt "may have been truncated", and the request was recorded as costing nothing | 0 is a missing count. The size is estimated, the request is accounted for, and no truncation is claimed |
| The client did not read why the endpoint stopped | `finish_reason` is read, and a reply cut off at the output limit is named as that |

An invalid reply is still not asked again. Both runs gave the same empty reply to the same prompt, so there is no evidence that a second request would help, and it would double the time of a failure.

It is not relabelled as a refusal. `{"chosen": []}` is the only reply that means the model chose nothing. An empty reply keeps its own status, and counts as invalid wherever results are counted.

Tests: [`tests/test_invalid_reply.py`](../tests/test_invalid_reply.py), with stand-in models that return fixed text. Among them is the reply recorded here: no text, 0 and 0.

## 2. Turkish: where the answers were lost

From `python eval/tools/v6_diagnosis.py`, which reads the stored rows only. Part A, with the stored wordings. The two versions differ on one question, so one column is given, v2's.

### The 20 answerable Turkish questions

| What happened first | Questions | Outcome |
|---|---|---|
| Correct | 7 | |
| **No right place among the passages**, and the model chose other lines | 4 | wrong source |
| No right place among the passages, and the model chose nothing | 1 | no answer |
| One of two needed places among the passages | 1 | partial |
| A right place was offered, and the model chose other lines | 2 | wrong source |
| Both places were offered, and the model chose one | 2 | partial |
| Refused before the model, with a right place among the passages | 2 | no answer |
| A right place was chosen, and the evidence rule turned it down | 1 | no answer |

Of the 13 that are not correct:

- **6 never had what they needed in front of the model**: the right place was in no passage, or one of two was.
- **4 had it, and the model chose otherwise**: twice other lines, twice one place of two.
- **3 were stopped by a rule**: two before the model, one after it.

**The 6 wrong sources**: 4 came from questions where no passage held a right place, and the model chose among the wrong ones. 2 came from the model passing over a right place it was offered.

**The 2 wrong acceptances** of 10 unanswerable questions are the same mechanism: 6 of the 10 reached the model, and it chose lines for 2.

### English, for comparison

| | Turkish | English |
|---|---|---|
| Answerable questions | 20 | 12 |
| No right place among the passages, and the question still reached the model | 5 | 0 |
| Refused before the model | 2 | 3 |
| Wrong source | 6 | 0 |
| Unanswerable questions reaching the model | 6 of 10 | 2 of 6 |
| Unanswerable wrongly accepted | 2 | 0 |

In English, a question whose passages hold nothing right did not reach the model in this set: the three such questions were refused first, because words of the question are nowhere in the code. That also cost three answerable questions. In Turkish, five such questions went on to the model, and four came back with a wrong source. The check that stops an English question looks at the words of the question as asked. A Turkish question is checked through a translation, and went through more often.

This is what the rows show for these 32 questions. It is not a measurement of the refusal check, and 12 English questions are few.

### The translations

One reading of the 20 stored wordings of answerable questions, by the person who wrote the questions:

| Wording | Questions | Correct in v2 |
|---|---|---|
| Says what the question says | 11 | 5 |
| Shifts the meaning: "journal entry", "collaborative workspaces", "already registered user" and the like | 6 | 1 |
| Changes the subject: notes became issues, leaving a note became forking a repository, a folder became a repository | 3 | 1 |

- **Proven for one question.** `v6-sn-owner-leave` was refused before the model, and the engine's own message names the cause: "Not found anywhere in the indexed code: fork, repository". Those two words are in the translation and not in the question.
- **Not proven for the rest.** The other questions with a shifted or changed wording fail more often than those with a faithful one, 7 of 9 against 6 of 11. No run exists in which the same question was asked with a correct wording, so the rows cannot say that the wording was the cause, and the numbers are small.
- **A different wording did not rescue any of them.** In part B the model translated again, and 6 of 30 wordings came out differently. Among the answerable ones, "confirmed" became "approved" and the answer stayed wrong, and "the owner tries to fork a repository" became "a contributor tries to fork the repository" and stayed refused.
- The translation prompt tells the model that the question is about a software repository. "Repository", "issue" and "fork" are words of that subject, and none of them is in a question. That the prompt causes them is a reading, not a measurement.

### What the rows cannot say

- **Never found, or found and not made a passage.** The rows hold the passages that were offered, not the 12 to 17 chunks search returned before them. For the 6 questions that lacked a right place, these two cannot be told apart without a trace of search. That needs no model, and was not run, because it would run the questions again.
- **Why the model chose other lines** when a right place was offered.

### Questions needing two places, both languages

8 questions. 1 fully right in each version.

| v2 | Questions |
|---|---|
| Correct | 1 |
| Both places offered, and the model chose one | 3 |
| One of two places among the passages | 2 |
| No right place among the passages | 1 |
| Invalid reply | 1 |

The model may choose two passages. It was offered both places for 5 of these questions: it chose both once, one of them three times, and once its reply was the empty one.

## 3. Every answerable question, both languages, v2

| Lost | Questions of 32 |
|---|---|
| Correct | 13 |
| Before the model saw what it needed: not among the passages, or one place of two | 7 |
| Refused before the model | 5 |
| The model chose otherwise, with what it needed in front of it | 5 |
| The evidence rule turned a right choice down | 1 |
| Invalid reply | 1 |

**12 of the 19 losses happen before the model sees the right lines**, or without it being asked. 5 are the model's choice.

## 4. The next decision

Two ways forward were asked for.

### A. Release v2 for what it fixes, with no claim of better answers

What v2 fixes is real, and is in the published version today:

- v1's index stores 14 chunks that hold a credential written into code, at the pinned commits of the two repositories. v2's stores none.
- v1 has no check between such a chunk and the model, the search results, or the file view. v2 has one at each. None was reached by these 48 questions, and the tests show the path with made-up values.
- v1 treats an index made by older code as current. v2 does not.

What stands in the way, and what would really remove it:

| | What it takes |
|---|---|
| **Safety condition 5 failed**, and stays failed in the record | It cannot be argued away. Either the user decides, in writing, to release with a known failure that is now handled safely and named, or it is removed by new evidence: a run that only counts unusable replies, on prompts of both versions, with the replies kept. That is a model run, and needs approval |
| The main index is still version 2 | Re-indexing the two repositories, which was forbidden so far. Until then v2's rule at indexing is not in effect there, and only the checks at question time are |
| The documents claim nothing yet, in either direction | README and `docs/evaluation.md` have to say what was measured: 13 against 12 of 32, not shown to be better, 6 wrong sources of 20 in Turkish |
| The branch is local | Merging and pushing, which need the user's word |

Nothing else is missing: 1216 Python tests, 13 web unit tests and 43 browser tests pass, and no test needs a model.

### B. Hold the release until answers are better

What the rows point to, in the order of how many questions each could reach:

1. **What reaches the model, 7 of 19 losses.** The first step costs no model: a trace of search for the questions that lacked a right place, to split "never found" from "found and not made a passage". Without it any change here is a guess.
2. **Refusal before the model, 5.** It protects English from wrong sources, and it lets Turkish through. Making Turkish as strict as English would likely remove wrong sources and add refusals. Making either looser was tried once, as the flexible evidence rule, and gave 4 more correct and 3 more wrong.
3. **Choosing one place of two, 3 of 8.** The model is allowed two and takes one. This is the only loss that sits wholly in the model's choice with everything in front of it.
4. **The translation prompt.** One proven loss, and at most a few more.

What was tried before on this project and did not hold, and should not be repeated: keyword and semantic weights, glossary senses in the keyword query, ranking windows by their chunk, 16 and 20 chunks, two passages per file, the flexible evidence rule, glossary terms in the translation prompt, a reranker, judging one passage at a time, the coder model as the default, and linked evidence as the default.

None of the four is small. Six question sets have been spent on this project, and no change to search or selection has yet moved correct answers by more than the model moves by itself.

### Recommendation

**A, and B afterwards as its own work, not as a condition of A.**

- The measurement says v2 answers as well as v1, not better: 13 and 12, 12 and 12 outside the constant questions, 6 and 6 wrong, 2 and 2 wrongly accepted. Waiting for accuracy keeps a version published whose index stores credentials, for a gain that six rounds have not produced and that nothing in these rows promises soon.
- The one thing v2 does worse is the empty reply. It gives no source, it is now named and kept, and its cause is unknown. It should be settled by counting, not by wording: one short run that counts unusable replies for both versions, with replies kept, before the release. If the user would rather release without it, that is their decision to make and to write down, since the condition was set beforehand and failed.
- B is worth doing, starting with the trace that needs no model. It should not hold the security fixes back.
