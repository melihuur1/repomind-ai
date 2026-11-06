# Two short trials on questions already read

Fixed before either trial ran. These questions have all been read, so nothing here can show how the system does on new questions. No answer benchmark runs, and no new question set is written. Nothing in `repomind/` changes because of these trials until the owner decides.

Both use `qwen2.5:7b` on the GPU, in Turkish, and start only with at least 12 GB of free physical memory and commit space. Each finished question goes to a checkpoint at once, and a run stops between questions below 4 GB.

## Trial 1: the evidence rule

The strict rule, used today, asks for 2 terms shared with the model's English translation. It decides before the model whether to ask at all, and afterwards whether to keep what the model chose.

The flexible rule asks for 2 shared concepts. A concept is either one term of that translation, or all the senses in the glossary of one Turkish word in the question, so that several senses of one word count once. It was chosen before this trial, over a wider rule that also counted the local translator's wording.

The model sees the same passages under both rules, so one request per question serves both, and both are compared on the same replies. Two more rules are worked out from the same replies, for reference: either translation counts, and no rule at all, which is the model alone.

**21 questions**, slightly more than the 18 proposed, because groups C and D are where the flexible rule can do harm:

| Group | Questions | Why |
| --- | --- | --- |
| A, the right lines are offered, and the strict rule would drop them | `sg-oversell`, `sn-offline`, `sn-ticket`, `sn-version-restore`, `sn-presence`, `v2-sg-negative-stock-db`, `v2-sg-reservation-defaults`, `v2-sg-add-stock-limits`, `v2-sn-collab-instances`, `v2-sn-note-search`, `v4-sg-username-format`, `v4-sn-postgres-port` | What the flexible rule could win |
| B, unanswerable, and only the flexible rule asks the model | `v2-sg-neg-transfer`, `v2-sn-neg-tags`, `v2-sn-neg-comments`, `v3-sg-neg-low-stock-alert`, `v4-sg-neg-reduce-quantity` | What it could let through |
| C, the right lines are not offered, and only the flexible rule asks the model | `sn-redis-outage`, `sn-session-refresh` | A wrong source on an answerable question |
| D, unanswerable, and in stored runs 7B chose lines that the strict rule dropped | `v4-sn-neg-view-log`, `v3-sn-neg-sunset-theme` | The rule's second check |

Measured for each rule: correct sources, wrong sources, unanswerable questions accepted, and right lines that the model chose and the rule dropped. Also how often 7B replies "none", and how often it chose lines sharing fewer than 2 terms with the question that still answer it.

**A line being real and a line answering the question are different things.** Every line offered is compared with Git. Whether the chosen lines answer the question is graded separately, without labels, under the rules in [`GRADING.md`](tools/GRADING.md). A mechanical "correct source" means only that the lines touch the gold span.

**Bar, in advance.** The flexible rule is recommended only if, over these 21 questions, it gives at least 4 more correct sources than the strict rule, and at most 1 more wrong source, counting accepted lines on an unanswerable question as wrong. Otherwise the strict rule stays.

**Limit of this design.** Groups A to D are the questions most likely to change. Others also reach the model under both rules: 10 unanswerable and 70 answerable. There the flexible rule could keep a wrong choice that the strict rule would drop, and this trial does not measure that. A recommendation for the flexible rule would need that check first.

## Trial 2: glossary terms in the translation

The same translation prompt, with one addition when the glossary has terms for the question:

> These words occur in this repository's code, and some Turkish words in the question may mean one of them: ... Prefer one of these words when it fits the question's meaning. Do not add a word the question does not ask about.

**Step 1, on a limited set.** Both prompts run on:

- **20 questions with a known translation error**, each with a check fixed here, in [`translation_trial.py`](tools/translation_trial.py): words the right meaning needs, and the wrong words seen so far. For example `v4-sn-refresh-token-storage` must contain "refresh", and `v4-sn-guard-order` must contain "order" or "sequence" and not "queue".
- **Technical names:** up to 20 taken from the index by a fixed random choice, in a Turkish sentence, and 5 real questions naming `Idempotency-Key`, `JWT`, `CSRF`, `Refresh token` and `PostgreSQL`. A name must survive as written, in the model's own reply, before the code adds back anything it dropped.

Also measured: how many of a translation's terms exist in the repository's vocabulary, how many it shares with the gold anchor, and, with no model, how far the gold code gets.

**Step 2 runs only if step 1 clears this bar:** hints fix at least 5 more of the 20 errors than they break, lose no technical name, and take gold in the passages up by at least 2 of the 20. Then the hints prompt translates the 85 answerable questions, and search is measured with no model. No question is answered.

**Bar for recommending the hints:** gold in the passages and gold passing the evidence rule each rise by at least 3 of 85. The 38 unanswerable questions are not translated in step 2, so its effect on them stays unmeasured.
