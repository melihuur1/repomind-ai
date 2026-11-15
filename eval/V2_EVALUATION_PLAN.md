# Plan for comparing v2 with v1, once

Written on 2026-11-15, before any of it is run. Nothing here has been measured. The run needs the user's approval, and this file is not changed after the first question is asked, except to add the results under a new heading.

**The question.** `feature/source-accuracy` ("v2") changes what RepoMind stores for a commit and how it answers. `main` ("v1") is what is published. No run so far compares the two with a model: the trials on the branch each changed one thing, on questions already read, and v2 has never answered with an index made by its own code. This plan compares them once, on questions neither has seen, and fixes beforehand what the result has to be for v2 to be released.

## What is compared

| | v1 | v2 |
|---|---|---|
| RepoMind code | `4e4a71f`, tagged `accuracy-baseline`, the tip of `main` and `origin/main` | the commit tagged `v2-candidate` |
| Index version | 2 | 3 |
| Index | made by v1's own code, in the scratch database `repomind_rt_eval_v1` | made by v2's own code, in `repomind_rt_eval_v2` |
| Settings | as published | defaults: `choose`, linked evidence off |

- **`v2-candidate`** is a local tag, set when the user approves the run, on the commit of `feature/source-accuracy` to be judged. Its full hash is written into the record before the first question. If anything under `repomind/` changes after the tag, the tag is not moved: the run is of the tagged commit, or it does not happen. The tag is not pushed.
- **The code of each version** is taken from Git into its own folder outside the project (`git archive`), and each version runs from its folder. The record holds the folder's commit, and the tool refuses to start if the package it imported is not from that folder.
- **The repositories asked about** are the same two, at the same commits as every earlier measurement: StockGuard at `b6698d81475232f2b8ca95af45336fae5665cd81` and SyncNotes at `bb217cddc89088f27b68715cf9585e94ce2fe3a6`. They are read through Git only, never run, and never written to.
- **Each version's index** holds those two commits, indexed by that version's code with the same embedding model. The main `repomind` database is not read and not changed. Both scratch databases are kept until the user has read the report, and then dropped.
- **The model** is `qwen2.5:7b` for both, on the project's own Ollama container with its usual settings: one model in memory, 8192 tokens of context. `qwen2.5-coder:7b`, `judge` and linked evidence are not part of this comparison.

A check without a model comes first, and stops the run if it fails: v2's index stores no text that `find_credential` flags, both indexes hold the pinned commits, and each version reports its own index as current.

## The questions

48 new questions, in `eval/questions_v6.yaml`, written before the run and never asked to any version of RepoMind before it.

| | Turkish | English | Total |
|---|---|---|---|
| Answerable | 20 | 12 | 32 |
| Unanswerable | 10 | 6 | 16 |
| Total | 30 | 18 | 48 |

Half are about each repository: 16 answerable and 8 unanswerable each.

**Answerable, 32.**

- 24 have their answer in one place.
- 8 need two places. 4 of these ask for the value of a named constant, where the line that uses it and the line that declares it are both needed. This is what completion was built for, and they are marked `targets_completion` in the file. The other 4 need two places of another kind, such as a check and the constraint behind it.

**Unanswerable, 16.**

- 8 ask about something the repository does not have at all.
- 8 ask for a detail that is absent of something the repository does have, such as a limit that is never set. These are the ones a wrong source is given for.

**How they are written.**

- From the code at the pinned commits, by reading it. No search, question or trace is run for a candidate question in either version, so a question cannot be kept or dropped by how it does.
- None repeats or rewords a question of an earlier set, and none is about the six cases named in earlier records (`sim-timeout`, `search-length`, `title-length`, `ttl-max`, negative stock, `v5-sn-neg-reminder`).
- Turkish questions are written in Turkish first, not translated from English.
- The user reads the file before the run and may reject any question. After the first question is asked, none is dropped, reworded or relabelled.

**Accepted sources.** Each answerable question lists every place that states its answer, not one preferred place. Before the run, each answer is looked for across the whole repository at the pinned commit with `git grep`, so that a second place that says the same thing, such as a web field's limit beside the server's, is listed as accepted too. For a question that needs two places, the file says which places are needed and lists the accepted alternatives of each.

**A source outside the list** can still be right. Such a source is judged blind: the grader sees the question and the lines, and not which version or run returned them. The decision and its reason are recorded, and it then counts the same way for both versions and both runs. A source is never accepted because it is close to an accepted one.

## Two measurements, kept apart

### Part A: choosing sources, with the same translation

This is what the release conditions are judged on.

- Each Turkish question is translated once, before Part A, by `qwen2.5:7b` with the translation prompt, which is the same file in both versions. The wording is stored in the record.
- Both versions are given that stored wording in place of asking the model to translate. Everything else on the Turkish path runs as each version does it: the local translator's second wording, the glossary seats and the refusal check.
- Each version answers all 48 questions twice, in the order v1, v2, v1, v2. Two runs each show how much one version differs from itself, which on earlier trials was up to 2 questions of 18.

### Part B: the application as a user meets it

This is reported, and decides nothing except the safety conditions that say so.

- The 30 Turkish questions are asked once to each version through its own API, as the web page asks them, so each version translates for itself.
- The English questions are not repeated, because nothing differs from Part A for them.
- The report says, for each version, how many translations differ from the stored one, and on how many questions the outcome differs from that version's own Part A runs. That is the cost of translation, kept apart from the choice of sources.

## How an answer is counted

Every source returned with the role `answer` is first judged right or wrong: right when it touches an accepted place, or was accepted blind. Sources to review and the closest lines are not answers, and are counted on their own.

| Outcome | A question with one place | A question needing two places |
|---|---|---|
| **Correct** | a right source, and no wrong one | a right source for each place, and no wrong one |
| **Partial** | does not apply | a right source for one place only, and no wrong one |
| **Mixed** | a right source and a wrong one | any right source and a wrong one |
| **Wrong** | sources, none right | sources, none right |
| **No answer** | refused, or nothing passed the evidence rule | the same |
| **Invalid** | the model's reply could not be used | the same |

- Only **correct** counts as correct. Partial is reported beside it and never added to it.
- **Mixed** and **wrong** both count as a wrong source given, because a reader cannot tell which lines to trust.
- An unanswerable question is **refused** when no source has the role `answer`, and **wrongly accepted** when one does.
- "No answer" is split into refused before the model, refused by the model, and chosen but turned down by the evidence rule, with whether the right lines had been offered.

## What is reported

For each version and each run of Part A, and once for Part B:

- the outcomes above, for all 32 answerable questions;
- the same **without the 4 constant-value questions**, on the other 28. v2 was built with those 4 in mind, so a gain that exists only there is a gain on one kind of question, and the report has to show that;
- the same split by language, by repository, and by one place or two;
- unanswerable questions refused and wrongly accepted, split into the two kinds;
- how far the right lines got with no model: among the candidates, in a passage, passing the evidence rule;
- time per question, peak GPU memory, and the lowest free physical memory and commit space;
- every question on which a version's two runs differ.

Every figure is a count out of its total. No percentage is given for fewer than 20 questions.

## Conditions, fixed now

Counts are over both Part A runs of a version, so out of 64 for answerable and 32 for unanswerable questions, unless a line says otherwise.

### Safety. Each one has to hold, or v2 is not released.

1. **Wrong acceptance.** Unanswerable questions wrongly accepted by v2 are not more than by v1.
2. **Wrong sources.** Answerable questions with a wrong or mixed outcome in v2 are not more than in v1.
3. **Real lines.** Every source v2 returns, in Part A and Part B, matches Git exactly at the pinned commit.
4. **Credentials.** No prompt sent to the model by v2, no API reply and no stored chunk or signature holds text that `find_credential` flags. The tool keeps every prompt and checks them all. Only counts and paths are written to the report.
5. **Usable replies.** Invalid replies in v2 are not more than in v1 plus 1.
6. **Refusing what it had.** Questions v2 left unanswered although the right lines were offered are not more than in v1 plus 2.
7. **Part B.** In Part B, v2 wrongly accepts no more unanswerable questions than v1 does, and gives no more wrong or mixed outcomes than v1 plus 1.
8. **Hardware.** Peak GPU memory stays under 4096 MiB, and the median time per answered question in v2 is not above 1.25 times v1's.

### Accuracy. All four have to hold for v2 to be called more accurate.

1. Correct answers in v2 are at least 6 more than in v1, of 64. That is 3 questions per run, more than the difference a version shows against itself.
2. v2's lower run is not below v1's higher run.
3. Without the 4 constant-value questions, v2 is not below v1, of 56.
4. On the 20 Turkish questions, v2 is not below v1, of 40.

### What follows

| Safety | Accuracy | Decision |
|---|---|---|
| all hold | all hold | v2 can be released, and the README may give these figures as measured on this set |
| all hold | not all | v2 can be released for what it fixes (credentials in code, declarations found by scope, the index version) with no claim of better answers, provided correct answers in v2 are not more than 2 below v1, of 64. Otherwise it is not released |
| any fails | either | v2 is not released. The report names the questions behind the failure |

Whatever the result, the report gives every correct source that was not found, and no figure is improved by counting a refusal as a success.

## Limits of the run

- **Once.** The set is asked once under this plan. After it, the questions have been read, and can only serve as development data. A fix made because of the result is not measured again on them as if they were new.
- **Interrupted.** Each answer is saved as it arrives. A run that stops continues with the questions not yet asked. No question is asked again in the same run, and no run is repeated because its result was unwelcome.
- **Memory.** Before the model is loaded, `check_before_loading()` needs 12 GB of free physical memory and 12 GB of free commit space, read twice. If it refuses, the tool waits and asks again, for at most 20 minutes in all. No application, container or WSL instance is closed to make room, and the memory protection is never switched off.
- **Time.** About 20 minutes of model time are expected for Part A and about 8 for Part B: roughly 5 seconds for a question that reaches the model, and 2.5 more for a translation. The run stops and is reported as incomplete if model time passes 60 minutes, or wall time 2 hours.
- **One model.** Nothing else is in the model server's memory during the run, and the model is unloaded at the end.
- **Nothing downloaded, nothing paid.** The model and the embedding model are already on the machine.

## What it will not show

- 32 answerable questions on two repositories. A difference of 1 or 2 means nothing, which is why the bar is 6 of 64.
- The repositories are the two that every change so far was developed on. The questions are new, and the code is not. Nothing here says how RepoMind does on a repository it has not met.
- One model, one prompt, one machine.
- Whether linked evidence helps. It stays off, and experimental.

## Before the run can start

None of this is built or started yet, and none of it changes `repomind/`.

1. The user approves the run, and `v2-candidate` is tagged.
2. `eval/questions_v6.yaml` is written and read by the user.
3. A tool under `eval/tools/` that takes a code folder, a database and the question file, asks through that version's own engine with a stored wording in place of the model's translation, saves every prompt and answer, and resumes. `model_trial.py` and `app_check.py` already do most of this for one version.
4. The two code folders and the two scratch indexes are made, and the check without a model passes.
5. The memory gate allows the model to load.
