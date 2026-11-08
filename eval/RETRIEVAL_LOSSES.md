# The five questions whose right lines never reached the model

In the [selection trial](SELECTION_TRIAL.md), five answerable questions of the fifth set never had their right lines offered to the model. This looks at where search lost each, with no LLM. These questions have been read, so this is development work, and nothing here is an independent result.

[`retrieval_trace.py`](tools/retrieval_trace.py) follows each chunk holding accepted lines through every list. It does this for the question as asked, with the stored 7B translation; with the set's own English wording in place of the 7B one; and for the English question alone. Search keeps 50 per list, fuses them by RRF, keeps 12, and adds up to 5 glossary seats. Rows are in [`dev-retrieval-trace.json`](results/dev-retrieval-trace.json).

## Where each was lost

| Question | Keyword rank | Semantic ranks (asked / translation / second) | Fused | Lost at | Cause |
|---|---|---|---|---|---|
| `v5-sg-root-redirect` | 2 | 167 / 45 / 105 | 26 | Passages. It is a candidate only through a glossary seat (13th) | Meaning: nothing in "root address" is like `addRedirectViewController("/", ...)`. Only `redirect` matches. The set's English wording ranks the same, so translation is not the cause |
| `v5-sg-zero-quantity` | 67 | 5 / 29 / 11 | 13 | The cut at 12 | Translation: both translators made "miktar" "amount", and the code says `quantity`. With the set's wording it is fused 2nd |
| `v5-sn-version-viewer` | 34 | 83 / 1 / 78 | 18 | The cut at 12 | Meaning: the question asks whether a viewer can create a version, and the code says "Only editors can manage versions". The 7B translation was wrong ("pull request comment"), but the set's correct wording ranks it lower, so translation is not the cause |
| `v5-sn-version-kinds` | 11 | 192 / 7 / 31 | 14 (enum), 11 (`api-types.ts`) | The cut at 12, and passages for `api-types.ts` | Translation and vocabulary: "türler" became "types" and "species", and the code says `VersionKind`. With "kinds" the keyword rank goes to 5. The two-line enum is weak in meaning |
| `v5-sn-migrate-first` | not indexed | not indexed | not indexed | The index | The `migrate` and `api` services were withheld whole, because their `environment` holds a password in a URL and a default JWT secret. Their `depends_on` went with them |

So two are lost to translation, two to the question and the code meaning the same thing in different words, and one to the index.

## What was tried

| Change | On | Result | Kept |
|---|---|---|---|
| Keyword and semantic get equal total weight in RRF, instead of one vote per semantic wording | The 4 indexed questions, simulated | Root redirect 26 → 7. Zero quantity 13 → 24, version viewer 18 → 25 | No: a trade, not a fix |
| The glossary's senses join the keyword query, not only its seats | All five sets, no LLM | Zero quantity reached the candidates (8th), but not the passages. Two questions lost their right lines, one more Turkish question was refused | No |
| A credential withholds only the flat list of settings that holds it, not its whole service | All five sets, no LLM | See below | Yes |

The third went through one revision. At first it took entries apart down to single variables. That made one-line chunks such as `COOKIE_SECURE`, which took passage seats: one English question was then refused. Flat lists of settings are now withheld whole, as before.

## Result of the kept change

Both indexes were built from the same pinned commits in scratch databases: the old parser in one, the new in the other. The old one gives the same totals as the main index, and the same passages for 293 of 294 questions. Rows: [`dev-retrieval-before.json`](results/dev-retrieval-before.json), [`dev-retrieval-after.json`](results/dev-retrieval-after.json).

| Of 101 answerable, 46 unanswerable, each language | TR before | TR after | EN before | EN after |
|---|---|---|---|---|
| Right code among the candidates | 90 | 91 | 90 | 91 |
| In a passage | 81 | 80 | 71 | 71 |
| Wholly inside a passage | 58 | 58 | 47 | 47 |
| In a passage passing the evidence rule | 66 | 66 | 67 | 67 |
| Refused before the model | 7 | 7 | 6 | 6 |
| Unanswerable reaching the model | 22 | 22 | 19 | 19 |
| Search time per question | 42.0 ms | 39.4 ms | 15.0 ms | 14.5 ms |

- **Gained:** `v5-sn-migrate-first` in both languages, among the candidates. Its two right chunks are now fused 4th (`services.migrate`) and 7th (`services.api.depends_on`). They still do not reach the passages, which pick `services.api`'s `build` lines instead.
- **Lost:** `sn-redis-outage` in Turkish lost a passage touching its right lines, `degraded-reconciler.ts:21-25`. That passage failed the evidence rule before as well, so no answer could come from it. No question lost evidence, and no unanswerable question newly reaches the model.
- **Cost:** 4 more chunks of 1,188, all in SyncNotes' `docker-compose.yml`, and none holding a credential line. StockGuard is unchanged. The peak memory of the measurement process stayed at 1.12 GB.
- **Security:** each emitted chunk is still checked, line by line, as before. Tests check that no chunk holds a line `credential_lines` flags, for nested, commented, listed and top-level credentials.

## What this leaves

For three of the five, and for `migrate-first` now, search puts the right chunk among the candidates or just past the cut. The passage step then drops it. Passage selection was left alone in this round, so that this change could be measured on its own.

The main `repomind` index is not rebuilt. It keeps the old chunks until it is indexed again with `--force`.
