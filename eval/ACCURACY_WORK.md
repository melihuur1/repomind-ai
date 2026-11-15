# What was changed after the sixth set, and what was measured

Written on 2026-11-16. The comparison in [V2_EVALUATION.md](V2_EVALUATION.md) stands as recorded, and nothing of the sixth question set was changed. Its questions have been read, so every figure below about them is **development data**. Nothing here was measured with a model choosing sources, so **none of it is a measured gain in correct answers**.

## Where the right lines were lost

`python eval/tools/stage_trace.py` runs every question through the real `ask` with a stand-in for the model, and says for each place an answer needs the first stage it did not pass. On the 32 answerable questions of the sixth set, with the stored wordings and the index made by index version 3:

| First stage a needed place did not pass | Before | After |
|---|---|---|
| Not among the chunks search returned | 3 | 3 |
| Retrieved, and not made a passage | 4 | 4 |
| A passage, and the question refused before the model | 5 | 0 |
| **Every needed place sent to the model** | **20** | **25** |
| Unanswerable questions reaching the model, of 16 | 8 | 9 |

The five refusals: three English questions were stopped by the check for words the code never uses, one for "95th", which was taken for a name, and two for words the code writes in another form ("exposed, shown" beside `exposure` and `show-details`, "supports, creation" beside `created`). Two Turkish questions had the right lines among their passages and were stopped because those lines shared too few words with the translation: once the translation had changed the subject, and once the words were in the file's name and not on the lines.

## What changed

| Change | Where |
|---|---|
| A number is neither a name nor a missing word | `repomind/qa/sufficiency.py` |
| A word is known when the code writes another word of its family | the same |
| The terms of a passage include its file's name and the unit it belongs to | `passage_terms` in `repomind/qa/passages.py` |
| A Turkish question is compared through both translations, each on its own | `repomind/qa/engine.py` |
| The declaration of a constant is shown with the chosen lines that use it | `declared_values` in `repomind/qa/completion.py` |
| A trace of where expected lines were lost, on request | `repomind/qa/trace.py`, `ask --expect` |
| A remote model is probed with its key, named in the page, and not held back by this machine's memory | `repomind/api/services.py` |

## The same rules on the older sets

`eval/tools/accuracy_funnel.py --complete`, no model, 101 answerable questions in each language and 46 unanswerable ones, all read long ago:

| | Turkish before | after | English before | after |
|---|---|---|---|---|
| Right lines in a passage | 80 | 80 | 73 | 73 |
| Right lines in a passage that passes the evidence rule | 66 | 69 | 69 | 71 |
| Unanswerable questions reaching the model, of 46 | 22 | 24 | 19 | 20 |

Five questions gained, none lost, and three more unanswerable questions reach the model. So the looser rules cost something where it matters: what the model does with those three is not known.

## Tried and not kept

**Larger or more passages.** On the sixth set, no model:

| Lines of a unit shown whole, passages | Every needed place sent | Unanswerable reaching the model | Prompt, tokens on average |
|---|---|---|---|
| 16, 5: as it is | 25 | 9 | 1430 |
| 32, 5 | 26 | 11 | 1742 |
| 16, 8 | 26 | 10 | 1870 |
| 32, 7 | 26 | 11 | 2134 |

One more question for one or two more unanswerable ones and 20 to 50% more to read. Not changed.

**The translation prompt.** The prompt says the question is "about a software repository". On 177 Turkish questions of all sets, 9 stored translations name a repository, an issue, a fork, a commit or a branch that the question does not. Two other wordings were tried, each with one translation run of `qwen2.5:7b` behind the memory gate, with no code sent, and then the no-model measurements:

| | Translations naming such a thing, of 177 | Older sets: in a passage, passing the rule | Sixth set: every place sent |
|---|---|---|---|
| The prompt as it is | 9 | 80, 69 | 25 |
| "Say what it says, each word its ordinary meaning" | 0 | 75, 65 | 27 |
| The same framing removed, "usual software terms" kept | 0 | 76, 67 | 25 |

The rule for the second was set before its run: keep it only with at least 78 in a passage and 68 passing the rule on the older sets. It gave 76 and 67, so the prompt is as it was. The first also wrote 6 of 177 translations with underscores or hyphens for spaces, which `plain_sentence` now reads as sentences. The same prompt asked twice gives other wordings for about one question in five, so differences of this size are near what a second run alone would show. A fair test of a prompt needs each one run more than once.

What guards a Turkish question against a wrong translation is therefore in the code, not in the prompt: search uses the question as asked and both translations, and lines count as evidence when they mention enough of either translation.

## What is not known

- **Whether more correct answers come out.** The five questions that now reach the model, the declaration shown with a chosen use, and the lines accepted through a file's name all depend on what the model chooses, and no model was asked.
- **Whether more wrong sources come out.** One more unanswerable question of 16 reaches the model on the sixth set, and three more of 92 on the older ones.
- **The seven questions still lost before the model.** Three are not retrieved. Four are retrieved and lose to other lines of the same files when the five passages are made.
