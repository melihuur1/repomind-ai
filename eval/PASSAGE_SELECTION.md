# From candidates to the five passages

After the [search round](RETRIEVAL_LOSSES.md), this looks at passage selection: how the five passages shown to the model are picked from the chunks search hands on. No LLM ran. All of these questions have been read, so this is development work and not an independent result.

Everything was measured on a scratch index of the two pinned commits, built with the current code, so the main `repomind` index was left as it is. The tools are [`passage_trace.py`](tools/passage_trace.py) and [`accuracy_funnel.py`](tools/accuracy_funnel.py). Rows are in [`dev-passage-trace.json`](results/dev-passage-trace.json) and [`dev-selection-variants.json`](results/dev-selection-variants.json).

## Two losses, kept apart

| Of 101 answerable questions | Turkish | English |
|---|---|---|
| Right code in a passage | 80 | 75 |
| **Not a candidate:** not among the 12 chunks and glossary seats | 10 | 10 |
| ↳ fused 13th to 20th | 2 | 2 |
| ↳ fused 21st to 30th | 2 | 3 |
| ↳ further | 6 | 5 |
| **Not chosen:** a candidate, with no passage touching its lines | 11 | 16 |
| ↳ the five passages were taken by windows ranked above it | 9 | 15 |
| ↳ its file already had three passages | 1 | 0 |
| ↳ no window touching it shares a word with the question | 1 | 1 |

These counts leave out the refusal check before passages, which the table below includes, so the English figure in a passage is 75 here and 71 there.

The five questions from the search round:

| Question | Turkish | English |
|---|---|---|
| `v5-sn-migrate-first` | Not chosen: no window of its lines shares a word with the question | The same |
| `v5-sg-root-redirect` | Not chosen: window 25th, five taken above it | Not chosen: window 12th |
| `v5-sg-zero-quantity` | Not a candidate: fused 13th | Not chosen: its chunk was taken with other lines, and the line holding `CHECK (quantity > 0)` was not shown |
| `v5-sn-version-kinds` | Not chosen: `api-types.ts` window 28th | Not a candidate: fused 13th |
| `v5-sn-version-viewer` | Not a candidate: fused 18th | Not a candidate: fused 42nd |

## What was measured

| Variant | Kind of loss it addresses |
|---|---|
| Every window shares its chunk's search rank | Not chosen. A window was ranked by its position in the list of all windows, so the fifteen windows of a long first chunk put every window of the seventh chunk past fifteenth |
| Only each chunk's best window takes its chunk's search rank, and the others follow | The same, without letting a long chunk's windows crowd the top |
| 16 or 20 chunks handed to passage selection, still at most five passages | Not a candidate, for the right chunks fused just past 12th |
| At most two passages per file instead of three | Not chosen, where passages from one file filled the five |

Turkish / English, of 101 answerable and 46 unanswerable each:

| | Current | Chunk rank | Best window | 16 chunks | 20 chunks | 2 per file |
|---|---|---|---|---|---|---|
| Right code among candidates | 91 / 91 | 91 / 91 | 91 / 91 | 92 / 92 | 93 / 93 | 91 / 91 |
| In a passage | 80 / 71 | 81 / 72 | 80 / 73 | 80 / 71 | 80 / 73 | 78 / 73 |
| Wholly inside a passage | 58 / 47 | 57 / 50 | 57 / 51 | 57 / 47 | 57 / 49 | 58 / 48 |
| Critical line shown | 75 / 68 | 72 / 68 | 72 / 68 | 74 / 67 | 74 / 69 | 74 / 69 |
| Passing the evidence rule | 66 / 67 | 65 / 66 | 65 / 67 | 66 / 68 | 66 / 69 | 65 / 69 |
| Refused before the model | 7 / 6 | 7 / 6 | 6 / 6 | 7 / 6 | 7 / 6 | 7 / 6 |
| Unanswerable reaching the model | 22 / 19 | 23 / 19 | 23 / 18 | 23 / 19 | 23 / 18 | 22 / 19 |
| Questions whose passages changed | | 179 | 201 | 100 | 143 | 106 |
| Passage selection per question | 817 / 581 ms | 807 / 599 ms | 840 / 563 ms | 1003 / 772 ms | 1153 / 799 ms | 813 / 588 ms |
| Prompt tokens, mean / highest | 1324 / 1988 | 1327 / 1988 | 1345 / 1988 | 1328 / 1988 | 1329 / 1988 | 1335 / 1884 |

The peak memory of the measuring process stayed at 1.12 GB in every run.

- **The two search orders** move the right code into a passage for 5 or 6 questions and out for 3 or 4. The same Turkish questions lose it in both: `v2-sg-error-status-map`, `v3-sn-redis-access-events` and `sn-auto-version`. Passing the evidence rule goes down, the critical line is shown 3 times fewer in Turkish, and one more unanswerable question reaches the model.
- **20 chunks** gives the only gain with no loss in the evidence rule: 2 English questions, `sg-stock-consistency` and `sn-auto-version`. It also moves 2 Turkish questions out of a passage, `v4-sg-db-port` and `v5-sg-sku-locked`, and one unanswerable question changes language, leaving the total equal. It costs about 0.2 to 0.35 s more per question, to embed more windows on the CPU.
- **16 chunks** and **2 per file** trade about as much as they gain.

None of the five questions reaches the evidence rule under any variant. The wider pool makes `zero-quantity` and `version-kinds` candidates in both languages, and passage selection still drops them.

## Decision

No variant is a clear gain, so passage selection is unchanged. The best is 20 chunks, with 2 more answers able to pass the evidence rule, of 202, and none lost. That is too small to tell from what the next question set would move, and it changes the passages of 143 questions.

## Security

[`test_secret_paths.py`](../tests/test_secret_paths.py) indexes a compose file whose credentials sit beside the settings that answer questions. It checks that no search candidate in any mode, no passage, no prompt and nothing shown to the user holds one. A question about the withheld value itself is refused before any model call. With the YAML filter switched off in a copy of the test, 7 of its 8 tests fail. The one left checks that the settings can be found.

## What this leaves

- **The lines and the question share no word.** `migrate-first`'s right lines, `depends_on: migrate: { condition: service_completed_successfully }`, share no word with a question about updating the schema before the API starts. They are dropped before any ranking, and the evidence rule would turn them down too.
- **A chunk just over 16 lines is cut.** `zero-quantity`'s `CREATE TABLE` is 18 lines, and its passage held other lines of it. In 22 Turkish and 24 English questions, the right lines are touched but not shown whole. In 5 of each, the critical line is not shown.
- **Competition for five places.** Most right windows not chosen rank between 8th and 36th among windows. Changing the order mostly trades one question for another.
