# v2 against v1: the result

Run once on 2026-11-15, by the rules fixed beforehand in [V2_EVALUATION_PLAN.md](V2_EVALUATION_PLAN.md). The questions, the accepted places and the conditions were committed and tagged before the model was loaded, and none of them was changed afterwards.

**In short.** v2 is not more accurate than v1 on these 48 questions, and it does not give fewer wrong sources. It answered 13 of 32 correctly where v1 answered 12, the same in both runs, and outside the four constant-value questions the two are equal at 12. Wrong sources and wrong acceptances are the same count on the same questions. One safety condition fails: one question got an empty reply from the model in v2, in both runs. By the conditions set beforehand, **v2 is not released** as it stands.

| | v1 | v2 |
|---|---|---|
| Code | `4e4a71f`, index version 2 | `fd03dce` (`v2-candidate`), index version 3 |
| Index | 534 + 650 chunks | 535 + 662 chunks |
| Model, settings | `qwen2.5:7b`, `choose` | the same, linked evidence off |

Rows: [`results/v6-v1-part-a-run1.json`](results/v6-v1-part-a-run1.json), [`run2`](results/v6-v1-part-a-run2.json), [`v6-v2-part-a-run1.json`](results/v6-v2-part-a-run1.json), [`run2`](results/v6-v2-part-a-run2.json), [`v6-v1-part-b.json`](results/v6-v1-part-b.json), [`v6-v2-part-b.json`](results/v6-v2-part-b.json). Stored wordings: [`v6-translations.json`](results/v6-translations.json). Decisions on sources outside the lists: [`v6-adjudications.json`](results/v6-adjudications.json). Every count below: [`v6-report.json`](results/v6-report.json), made by `python eval/tools/v6_eval.py report`.

## Before the model

- **The question file** matched the plan and Git: 48 questions, 60 accepted places, every anchor present at the pinned commits, no absent term in an indexed file.
- **What each index stores.** v2's index holds no text that `find_credential` flags. v1's holds 14 such chunks, in test code, one script and one configuration file, as expected of code with no rule for credentials in code.
- **What each version would send.** All 48 questions were run through both versions with no model, once before the wordings were stored and once with them. No passage about to be sent was flagged, in either version. The same check stood in front of the model for all six passes, and blocked nothing.
- **Memory.** Every pass started with at least 13.67 GB of free physical memory and 21.81 GB of commit space, read twice. Between passes the WSL machine kept the model file in its file cache, which held free memory near 9 GB. The cache of that one file was released inside RepoMind's own Ollama container before each pass. No application, container or setting was touched to make room.

## Part A: choosing sources, with the same stored wording

Both versions got the same 30 stored wordings. Each ran twice. **The two runs of each version gave the same outcome on every question**, and sent byte-identical prompts, so each column below is both of its runs.

### Answerable questions, 32

| | v1 | v2 |
|---|---|---|
| **Correct** | 12 | 13 |
| Partial: one of two places | 6 | 5 |
| Wrong source | 6 | 6 |
| Mixed | 0 | 0 |
| No answer | 8 | 7 |
| Invalid reply | 0 | 1 |
| Blocked by the credential check | 0 | 0 |

Where "no answer" came from:

| | v1 | v2 |
|---|---|---|
| Refused before the model | 5 | 5 |
| The model chose nothing | 1 | 1 |
| Chosen, and turned down by the evidence rule | 2 | 1 |
| No answer or invalid although every right place was offered | 4 | 4 |

How far the right lines got:

| | v1 | v2 |
|---|---|---|
| Every needed place among the passages | 20 | 22 |
| At least one needed place among the passages | 24 | 24 |
| Every needed place sent to the model | 18 | 20 |

### The four questions that differ

| Question | v1 | v2 | What happened |
|---|---|---|---|
| `v6-sn-folder-name-length` | no answer | **correct** | v1 offered lines 12-14, which the evidence rule turned down. v2 offered the whole unit, 9-14, and it passed |
| `v6-sn-member-order`, constant | partial | **correct** | v1 chose the declaration alone. v2 offered the use as one unit, 21-36, and the model chose both |
| `v6-sg-user-reservations-index` | **correct** | partial | Both offered both places. In v2 the model chose only the index |
| `v6-sg-replay-header`, constant | partial | **invalid** | In v2 the model server returned an empty reply with 0 tokens, in both runs. v1 chose the use |

Two better and two worse. The 28 other questions have the same outcome in both versions.

### With and without the four constant-value questions

| | v1 | v2 |
|---|---|---|
| All 32: correct | 12 | 13 |
| **The 28 other questions: correct** | 12 | 12 |
| The 28 other questions: partial, wrong, no answer | 2, 6, 8 | 3, 6, 7 |
| The 4 constant questions: correct, partial, invalid | 0, 4, 0 | 1, 2, 1 |

On the kind of question completion was built for, v2 got one of four fully right, where v1 got none, and lost another to the empty reply. In `v6-sg-simulated-user-prefix` completion did what it was meant to: it added the declaration, the model chose it, and left the use out, so the answer stayed partial. In `v6-sn-publish-timeout` the declaration and the use were in the passages of both versions, and the model chose one.

### Unanswerable questions, 16

| | v1 | v2 |
|---|---|---|
| Refused | 14 | 14 |
| **Wrongly accepted** | 2 | 2 |
| Reached the model | 8 | 8 |
| Nothing in the repository, 8: wrongly accepted | 0 | 0 |
| A missing detail of something it has, 8: wrongly accepted | 2 | 2 |

The same two in both: `v6-sg-neg-refresh-token`, answered with the lines that issue a token, and `v6-sn-neg-member-limit`, answered with the top of a migration.

### By language

| | Turkish, 20 + 10 | English, 12 + 6 |
|---|---|---|
| Correct, v1 / v2 | 7 / 7 | 5 / 6 |
| Partial, v1 / v2 | 3 / 3 | 3 / 2 |
| **Wrong source**, v1 / v2 | 6 / 6 | 0 / 0 |
| No answer, v1 / v2 | 4 / 4 | 4 / 3 |
| Invalid, v1 / v2 | 0 / 0 | 0 / 1 |
| Unanswerable wrongly accepted, v1 / v2 | 2 / 2 | 0 / 0 |

Every wrong source and every wrong acceptance in part A is on a Turkish question, in both versions. Fewer than 20 questions stand behind the English column, so no percentage is given.

The stored wordings explain part of it. Of the 30, at least four change what is asked about: a list of notes became "the issue list", the owner leaving a note became "the owner tries to fork a repository", a folder became "a repository", and the members of a note became members of "a team". The translation prompt tells the model the question is about a software repository, and it leans on that. Three more shift the meaning less. This is the same in both versions, because the prompt is.

### By repository, and by one place or two

| Correct, v1 / v2 | |
|---|---|
| StockGuard, 16 | 7 / 6 |
| SyncNotes, 16 | 5 / 7 |
| One place, 24 | 11 / 12 |
| Two places, 8 | 1 / 1 |

Of the eight questions that need two places, each version got one fully right. v1 had six partial and one wrong, v2 five partial, one wrong and the invalid one.

## Part B: the application translating for itself

The 30 Turkish questions, once per version, with the model translating as it does for the page.

| | v1 | v2 |
|---|---|---|
| Correct, of 20 | 8 | 9 |
| Partial | 3 | 2 |
| Wrong source | 5 | 5 |
| No answer | 4 | 4 |
| Unanswerable wrongly accepted, of 10 | 2 | 2 |
| Wordings that differ from the stored one | 6 | 6 |
| Outcome differs from that version's part A | 3 | 6 |

- The same model with the same prompt gave a different wording for 6 of 30 questions the second time it was asked. Both versions got the same six, because their requests are identical.
- Those wordings moved outcomes both ways. `v6-sn-invite-existing-member` went from wrong to correct in both versions. `v6-sg-neg-refresh-token` went from wrongly accepted to refused, and `v6-sn-neg-subfolder` from refused to wrongly accepted, when "subfolder" became "inner folder".
- v2's extra correct answer here is `v6-sg-simulated-user-prefix`, where the model chose the use and the added declaration. In part A it had chosen the declaration alone. The prompt differs between the parts in the wording only.
- **The same prompt did not always get the same choice.** For 20 Turkish questions in each version the choosing prompt of part B is byte for byte the one of part A. The outcome still differs on 2 of them in v1 and 4 in v2: `v6-sn-invite-existing-member` and `v6-sg-neg-refresh-token` in both, and `v6-sg-user-reservations-index` and `v6-sn-member-order` in v2. The only difference is that in part B a translation request went to the model just before. So the two identical runs of part A do not mean the model always repeats itself. They repeated because the order of requests was the same.

A changed wording, or only a different request before it, changes the answer about as often as the change from v1 to v2 does.

## Lines and credentials

| | v1 | v2 |
|---|---|---|
| Sources returned, all six passes | 559 | 567 |
| Matching Git exactly at the pinned commit | 559 | 567 |
| Prompts sent to the model for choosing | 94 | 95 |
| Passages in them that hold a credential | 0 | 0 |
| Questions blocked by the check | 0 | 0 |
| Chunks in the index that hold a credential | 14 | 0 |

v1 never sent a credential here. Its index does store them, and nothing in v1 would stop one from being offered if a question led there.

## Time, memory and tokens

| | v1 | v2 |
|---|---|---|
| Median time when the model was asked, part A | 5.76 s, 5.81 s | 6.29 s, 6.23 s |
| Longest question, part A | 14.2 s | 14.2 s |
| Median with its own translation, part B | 8.17 s | 8.56 s |
| Input tokens per question that reached the model, part A | 941 | 1031 |
| Output tokens, the same | 38 | 35 |
| Input and output tokens in one part A run | 32,927 and 1,322 | 36,086 and 1,217 |
| Peak GPU memory in use | 2812 MiB | 2814 MiB |
| Lowest free physical memory during a question | 7.67 GB | 7.57 GB |
| Lowest free commit space during a question | 13.39 GB | 13.25 GB |

The model sat 3282 of 6082 MiB in GPU memory, 54%, in every pass. Model time was about 14 minutes for part A and 7 for part B, inside the 60 minutes allowed. v2 is about 8% slower and sends about 10% more input, because whole units are longer than three-line windows. The model is local, so nothing was paid.

## The conditions, as fixed beforehand

Counts are over both part A runs.

**Safety**

| | v2 | v1 | Holds |
|---|---|---|---|
| 1. Unanswerable wrongly accepted: v2 not above v1 | 4 | 4 | yes |
| 2. Wrong or mixed sources: v2 not above v1 | 12 | 12 | yes |
| 3. Every source v2 returned matches Git | 567 of 567 | | yes |
| 4. No v2 prompt, reply or stored chunk holds a credential | 0 | | yes |
| **5. Invalid replies: v2 not above v1 + 1** | **2** | **0** | **no** |
| 6. Unanswered with the right lines offered: v2 not above v1 + 2 | 8 | 8 | yes |
| 7. Part B: v2 accepts no more wrongly, wrong or mixed not above v1 + 1 | 2 and 5 | 2 and 5 | yes |
| 8. GPU memory under 4096 MiB, median time not above 1.25 x v1 | 2814 MiB, 6.26 s | limit 7.23 s | yes |

**Accuracy**

| | v2 | v1 | Holds |
|---|---|---|---|
| **1. Correct: v2 at least v1 + 6, of 64** | **26** | **24** | **no** |
| 2. v2's lower run not below v1's higher run | 13, 13 | 12, 12 | yes |
| 3. Without the four constant questions: v2 not below v1, of 56 | 24 | 24 | yes |
| 4. Turkish: v2 not below v1, of 40 | 14 | 14 | yes |

**Decision by the table in the plan: not released, because a safety condition fails.**

Condition 5 fails on one question, `v6-sg-replay-header`, counted in both runs because the runs are identical. The model server answered with status 200, no text and 0 tokens. The tool kept every prompt and not the replies, so why the reply was empty is not known, and the question was not asked again to find out, because the plan allows no run beyond the planned ones. It is recorded as what it is: with v2's passages for that question, the model gave nothing usable twice, where with v1's it chose a line.

Had condition 5 held, accuracy condition 1 would still fail, and the plan's second row would apply: release for what v2 fixes, with no claim of better answers.

## What this does and does not show

- **v2 is not shown to answer more questions correctly.** One more of 32, two up and two down, and nothing outside the four constant questions. The plan set the bar at three questions per run because a version had differed from itself by up to two on earlier trials. Here the two runs of a version did not differ at all, but part B shows that this is not the whole of it: with identical prompts and only another request before them, 2 outcomes changed in v1 and 4 in v2. One question in v2's favour is inside that.
- **Wrong sources are neither fewer nor more.** 6 and 6 answerable, 2 and 2 unanswerable, the same questions.
- **v2 is more complex and costs a little more for the same accuracy.** About 1,500 more lines under `repomind/`, 8% more time and 10% more input tokens.
- **What v2 does deliver is not measured by correct answers**: a credential written into code is kept out of the index and out of every prompt, a declaration is found by scope and not by name alone, and an index knows which version made it. None of that was contradicted here, and the credential check found nothing to stop in either version on these questions.
- **One reviewer.** The questions, the accepted places and the decisions on the 12 sources outside the lists are one person's, who also wrote v2. The 12 were judged with no version shown, and all 12 were judged wrong. The closest call is the summary "the held units leave the stock" for the question which counters change on confirmation. Both versions gave it as their only answer, so counting it right would make it 13 and 14 correct and change no comparison.
- **48 questions, two repositories that v2 was developed on, one model.** These questions have now been read, and can only serve as development data from here.

## Known problems this run leaves open

1. `v6-sg-replay-header` in v2: an empty reply from the model server, cause unknown. Replies should be kept beside the prompts in any later run.
2. Turkish: 6 wrong sources of 20 and 2 wrong acceptances of 10, in both versions, against none in English, and a translation step that changes the subject of some questions and varies between two calls.
3. Questions needing two places: 1 of 8 fully right in both versions. The model usually chooses one of the two places it is offered.
4. Five answerable questions are refused before the model in both versions: two of the three whose answer is in YAML, one in SQL, one in Java, and `v6-sn-owner-leave`, whose stored wording asks about forking a repository.
5. The model's choice is not stable: an identical prompt got another choice when a different request came before it.
6. The main index is still the one made by index version 2.
