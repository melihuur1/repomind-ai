# Completing evidence with what it depends on

The [real-flow check](APP_CHECK.md) gave four new answerable questions and no correct source. This round looked for why, and added what the existing index and Tree-sitter can resolve reliably. The four questions and the development sets have been read, so nothing here is an independent result.

## Why the four failed

Traced with [`retrieval_trace.py`](tools/retrieval_trace.py) and [`passage_trace.py`](tools/passage_trace.py), with the coder model's own translations:

| Question | Lost at | Cause |
|---|---|---|
| `app-sg-ttl-max` | The cut at 12 candidates: the request record was fused 16th | Words: "longest wait time" against `ttlSeconds` and `@Max(3600)`. The only structural path to the record takes two steps: `command.ttlSeconds()`, then `CreateReservationCommand`, then the controller's mapping from `CreateReservationRequest` |
| `app-sn-title-length` | Passage selection: `ydoc.ts:4-6` was the 10th candidate, its window 9th, and five passages were taken above it | The web page's `maxLength={200}` was chosen instead. Nothing in the code links that input to the collaboration server's `.slice(0, MAX_TITLE_LENGTH)`, which run in different applications |
| `app-sg-sim-timeout` | The value | The passage held `.timeout(REQUEST_TIMEOUT)`, and the declaration with 60 seconds, 98 lines up, was not shown |
| `app-sn-search-length` | The refusal check, before the model | "longest" and "accepted" are not in the code. Without the check, `dto.ts:5-20`, with `@MaxLength(100)`, would have been the 4th passage |

## What the index holds

The symbol table has classes, interfaces, records, enums, methods, functions, multi-line variables and configuration keys. It has no constants: a one-line `private static final` field or `export const` is below the indexer's size for a symbol. The imports table keeps each file's module and imported names. So a constant's declaration is not in the index, and the indexed file is.

## What was built

**A constant's declaration, completed into the passages** ([`completion.py`](../repomind/qa/completion.py)). After passage selection, each constant a passage names, in capitals with an underscore, is looked up in the file read from Git at the indexed commit and parsed with Tree-sitter. It follows only what resolves to exactly one declaration:

- the passage's own file;
- in TypeScript and JavaScript, a relative module the file imports the name from;
- in Java, `Name.CONSTANT`, when exactly one class, interface, enum or record is called `Name`.

The declaration has to be at most four lines and inside an indexed chunk, and is checked for a credential. At most two are added, each after the passage that uses it. Only indexed files are read from Git, so a file left out of the index is never opened. When the chosen lines use a constant whose value they do not show, the answer says so, with where the value is or why it is unknown. Nothing is re-indexed.

**Words that ask for a limit**, in the refusal check ([`sufficiency.py`](../repomind/qa/sufficiency.py)). Longest, largest, minimum, maximum and the like count as known where the code writes `max` or `min`, as the Turkish glossary already does for "en fazla" and "en az". The threshold of two unknown words is unchanged.

## What was not built, and why

- **A function call to its definition.** A call names a method that can be overloaded, inherited or injected, and a body is long. Call edges were measured before and would have reached 1 or 2 of 11 lost questions.
- **A configuration key to its value.** `defaultTtl` in a Java record becomes `default-ttl` in `application.yml` only through Spring's naming rules and a prefix. That is one framework's convention, and none of the four needed it.
- **A field in the web page to the server's check.** No import or call links the two applications. They are not counted as the same evidence, and the web field still reads as the web field.
- **A condition to the operation it guards, across types**, as `ttl-max` would need. It takes two hops through different types, and was not reliable enough to build.
- **A looser refusal check.** Allowing three unknown words would let 8 more unanswerable English questions through, to rescue 4 answerable ones.

## Results with no model

The main index, read only, on the five development sets in both languages. Rows: [`dev-completion-before.json`](results/dev-completion-before.json), [`dev-completion-after.json`](results/dev-completion-after.json), [`dev-completion-and-limits.json`](results/dev-completion-and-limits.json).

| Turkish / English, of 101 answerable and 46 unanswerable | Before | Completion | Completion and limit words |
|---|---|---|---|
| Right code among candidates | 90 / 90 | 90 / 90 | 90 / 90 |
| In a passage | 81 / 71 | 81 / 71 | 81 / 73 |
| Wholly inside a passage | 58 / 47 | 58 / 47 | 58 / 48 |
| Critical line shown | 75 / 68 | 75 / 68 | 75 / 70 |
| Passing the evidence rule | 66 / 67 | 66 / 67 | 66 / 69 |
| Refused before the model | 7 / 6 | 7 / 6 | 7 / 4 |
| Unanswerable reaching the model | 22 / 19 | 22 / 19 | 22 / 19 |
| Declarations added | | 22 in 15 questions / 24 in 19 | the same |
| Time added per question | | 15.7 / 4.1 ms | 15.8 / 4.2 ms |
| Prompt tokens, mean | 1325 / 1271 | 1335 / 1285 | 1335 / 1283 |

- **Completion** added 46 declarations, 14 distinct. A check of every one found that the line declares the name, and that the file using it is that file or imports the name from it. No measure of where the right code got to changed, for better or worse: no development question's accepted lines are a constant's declaration that was missing. No unanswerable question newly reaches the model.
- **Limit words** let two English questions through, `v2-sg-simulation-limit` and `v3-sn-register-password`, which now reach the evidence rule. Turkish is unchanged, as is every unanswerable question. Of the 10 unanswerable English questions this check refused before, all 10 are still refused.
- Peak memory of the measuring process stayed at 1.12 GB.

On the real-flow questions, with no model: `sim-timeout` now has `SimulationService.java:49` as its second passage, right after the line that uses it, and `search-length` passes the refusal check with its right lines in a passage that passes the evidence rule. `ttl-max` and `title-length` are unchanged.

## Results with the model

The same seven questions, asked again through the API with `qwen2.5-coder:7b`, the model of the earlier check, so that only the code differs. It started at 14.88 and 14.97 GB of free physical memory and 21.9 GB of commit space. Rows: [`dev-app-check-qwen2.5-coder-7b-completion.json`](results/dev-app-check-qwen2.5-coder-7b-completion.json).

| Question | Before | After |
|---|---|---|
| `app-sg-ttl-max` | no answer | no answer |
| `app-sn-title-length` | wrong source, by the accepted lines: the web field | the same |
| `app-sg-barcode` (unanswerable) | refused | refused |
| `app-sg-sim-timeout` | no answer | no answer, with the right line as a source to review |
| `app-sn-search-length` | refused before the model | **correct**: `dto.ts:5-20`, "a `q` field with a maximum length of 100 characters" |
| `app-sn-webhook` (unanswerable) | refused | refused |
| `manual-negative-stock`, apart | correct | correct |

For `sim-timeout`, the model chose the added declaration, `REQUEST_TIMEOUT = Duration.ofSeconds(60)`. The evidence rule turned it down, because the line shares one word with the question, "request", and the rule asks for two. So it is shown as a source to review, and the status stays without an answer. One way to change that would be to let a declaration count as evidence when the line that uses it does. That is a change to the evidence rule, decided after seeing this result, so it was not made.

Every returned line matched Git at the indexed commit. Peak GPU memory in use was 2814 MiB, and the lowest free memory 8.48 GB physical and 12.18 GB commit space.

## Linked evidence, added after the user approved it

A declaration that completion added is evidence as a pair with the line that uses it ([`linked_evidence`](../repomind/qa/completion.py)). The general evidence rule is unchanged: every other passage needs two shared words, as before.

**When a declaration the model chose counts as evidence.** Every one of these has to hold:

- completion itself added it, so it resolved to exactly one declaration, inside an indexed chunk, and passed the credential check;
- the line that uses it was offered to the model in the same request;
- that line passes the evidence rule on its own;
- both come from the same repository and commit, and both were checked against Git before the model saw them;
- the declaration shares at least one word with the question, so that a constant beside the point is not taken in with a line that is.

Then both are shown as the answer, the use first, with a warning that the lines exist in Git and that whether they answer the question is the model's reading.

**When it never does.** A declaration search found by itself, or one completion only noted as already shown; a declaration completion did not add because it was ambiguous, declared twice, outside the index, holding a credential or too long; a use that was not offered, that fails the rule, or that comes from another commit; a declaration sharing no word with the question. A use the model chose alone never brings its declaration into the answer. The answer only says where the value is.

The judging way of choosing, which is experimental, does not use pairs.

### With no model

On the development sets, nothing that can be measured without a model changed. Linking decides only about lines the model chose. Rows: [`dev-completion-linked.json`](results/dev-completion-linked.json).

How many questions have a declaration that would count only as a pair, if the model chose it:

| | Turkish | English |
|---|---|---|
| Answerable | 5, none of them the right lines | 7, none of them the right lines |
| Unanswerable | 1 | 1 |

Both unanswerable ones are `v5-sn-neg-reminder`, "How is a reminder set for a note?". Its pairable declaration is `NOTE_ROLE_KEY = 'syncnotes:noteRole'`, which shares "note" with the question, beside the line using it, `decorators.ts:10`. That line already passes the rule, so choosing it would already have given a wrong source. A choice of the declaration would before have been a source to review, and could now be a wrong source. This is the new risk. The one shared word is a weak tie when the word is as common as "note".

On the real-flow questions, only `sim-timeout`'s declaration counts as a pair.

### With the model

Through the API with `qwen2.5-coder:7b`, the same seven questions as before, started at 15.67 GB of free physical memory and 23.9 GB of commit space. Rows: [`dev-app-check-qwen2.5-coder-7b-linked.json`](results/dev-app-check-qwen2.5-coder-7b-linked.json).

| Question | Before completion | With completion | With linked evidence |
|---|---|---|---|
| `app-sg-sim-timeout` | no answer | no answer, the right line to review | **correct**: `SimulationService.java:147-156` and `:49`, "REQUEST_TIMEOUT is set to 60 seconds" |
| `app-sn-search-length` | refused before the model | correct | correct |
| `app-sn-title-length` | wrong source, the web field | the same | the same |
| `app-sg-ttl-max` | no answer | no answer | no answer |
| `app-sg-barcode`, `app-sn-webhook` (unanswerable) | refused | refused | refused |
| `manual-negative-stock`, apart | correct | correct | correct |

The new risk, `v5-sn-neg-reminder`, asked in Turkish with the stored translation, once with each model: both had the pairable declaration offered, and both declined, with no source given. The default model started at 12.19 GB of free physical memory and the coder model at 15.41 GB, after two starts refused below 12 GB with nothing loaded. Rows: [`dev-linked-reminder-qwen2.5-7b.json`](results/dev-linked-reminder-qwen2.5-7b.json), [`dev-linked-reminder-qwen2.5-coder-7b.json`](results/dev-linked-reminder-qwen2.5-coder-7b.json).

`sim-timeout` is one known failure, fixed on purpose. It does not show that answers are more accurate in general.

## Since this record

Added on 2026-11-14. Nothing above was changed, and the result files are as they were. The code has changed since, so a run today would not repeat every row:

- **Linked evidence is off by default.** A review found that the pair is in effect a one-shared-word rule, with the word usually coming from the constant's own name: "note" is in 65% of SyncNotes' files and "request" in 47% of StockGuard's, and 1 of the 15 declarations it could accept is a right source. It now needs `REPOMIND_LLM_LINKED_EVIDENCE=1`, and is experimental. The rows under "With linked evidence" were made while it was always on. With it off, `app-sg-sim-timeout` is "no answer, the right line to review" again, as in the column before it.
- **The use that linking adds is labelled.** When it is on and the model chose only the declaration, the line that uses the constant is marked as added by RepoMind, and not as chosen by the model.
