# qwen2.5:7b against qwen2.5-coder:7b

Written before either run, and not changed after them. The tool is [`model_trial.py`](tools/model_trial.py).

## What is compared

Only the model that chooses the lines. `qwen2.5:7b` is the default, and stays the default whatever this shows: changing it is decided with the user afterwards. `qwen2.5-coder:7b` is the same size and the same Qwen2.5 base, trained further on code. It is released under Apache 2.0, read from its license file before it was downloaded.

Both models get:

- the same 32 development questions as the [selection trial](SELECTION_TRIAL.md), asked in Turkish;
- the same main index and pinned commits, and the same stored 7B translations, so the same search and the same five passages in the same order;
- the same `choose` prompt and evidence rule, the same token limits, and temperature 0;
- the same nonce in the prompt for each question, so the prompts are byte for byte identical. The comparison refuses to run if any prompt or any passage differs.

**All of these questions have been read, so this is development data and not an independent result.** The negative-stock question is reported on its own and left out of the totals, which cover 20 answerable and 11 unanswerable questions.

## Outcomes, for each model

Correct source, wrong source and no answer, of 20. Wrong choices and refusals while the right lines were offered. Unanswerable questions given a source, of 11. Invalid replies. "Wrong in all" is wrong sources plus unanswerable questions given a source. The questions each model failed.

Cost: seconds per question (median and highest), input and output tokens, peak GPU memory in use, how much of the model sits in GPU memory, and the lowest free physical memory and commit space during a question.

## What would count as better

`qwen2.5-coder:7b` counts as better only if all of these hold:

1. **No more wrong sources:** its wrong in all ≤ the default's.
2. **A clear gain:** either at least 2 more correct sources with no more wrong in all, or at least 2 fewer wrong in all with no fewer correct sources. A difference of one question on this many is noise.
3. **Refusals while the right lines were offered:** at most 1 more than the default's.
4. **Invalid replies:** at most 1 more than the default's.
5. **Cost:** a median of at most 1.5 times the default's seconds per question, peak GPU memory under 4096 MiB, and no larger share of the model outside GPU memory.

If 1 to 4 hold and 5 does not, that is reported, and the cost is weighed with the user. More answers alone is not better. More wrong sources is a serious loss whatever else changes. If a model spills onto the CPU and slows down, that is reported.

## Hardware

The machine has 32 GB of RAM and a 4 GB RTX 3050. Each run starts only when two readings a second apart each show 12 GB of free physical memory and 12 GB of free commit space, and only when no other model is in the model server's memory. The server keeps one model loaded at most (`OLLAMA_MAX_LOADED_MODELS=1`). Each run unloads its model at the end, before the next one starts. Every question is saved as it finishes, and a run stops between questions below 4 GB.

The default model's run is a new one, not the selection trial's rows. The same model can choose differently from run to run.

## Result, added after the runs

The rules above are as they were. Rows: [`dev-model-trial-qwen2.5-7b.json`](results/dev-model-trial-qwen2.5-7b.json), [`dev-model-trial-qwen2.5-coder-7b.json`](results/dev-model-trial-qwen2.5-coder-7b.json), [`dev-model-trial-comparison.json`](results/dev-model-trial-comparison.json).

The default model's run started at 15.19 and 15.21 GB of free physical memory and 22.5 GB of commit space. The coder model's first start was refused at 9.50 GB, with nothing loaded. It started seven minutes later, at 14.81 and 14.83 GB and 22.2 GB. The two models were never in memory together. The comparison found the prompts identical for all 31 questions that reached a model. The 32nd, `v5-sn-neg-photo`, was refused before any model for both.

| Of 20 answerable, 11 unanswerable | qwen2.5:7b | qwen2.5-coder:7b |
|---|---|---|
| Correct source | 9 | 10 |
| Wrong source | 3 | 1 |
| No answer | 7 | 9 |
| Wrong choice while the right lines were offered (of 15) | 2 | 0 |
| Refused while the right lines were offered (of 15) | 4 | 5 |
| Unanswerable given a source | 1 | 1 |
| Wrong in all | 4 | 2 |
| Invalid replies | 1 | 0 |
| Seconds per question, median (highest) | 5.1 (12.3) | 3.9 (10.3) |
| Input / output tokens per request | 1027 / 29 | 1062 / 20 |
| Peak GPU memory in use, by `nvidia-smi` | 2816 MiB | 2818 MiB |
| Model in GPU memory, as the server reports it | 3282 of 6082 MiB, 54% | 3282 of 6082 MiB, 54% |
| Lowest free physical memory / commit space | 8.33 / 12.88 GB | 8.90 / 13.38 GB |

| Rule | |
|---|---|
| 1. No more wrong sources | holds, 2 against 4 |
| 2. A clear gain | holds, by 2 fewer wrong in all with no fewer correct |
| 3. Refusals while the right lines were offered, at most 1 more | holds, 5 against 4 |
| 4. Invalid replies, at most 1 more | holds, 0 against 1 |
| 5. Cost | holds: 0.76 times the median, under 4096 MiB, the same share in GPU memory |

**All five hold on this run.** The default stays `qwen2.5:7b` until the user decides.

**How much of this is noise.** The default model's two runs on these questions, this one and the selection trial's, had the same passages for all 32. Its outcome still changed on 2: `v5-sg-movements-order` was correct before and an invalid reply now, and `v5-sn-neg-google` was given a source before and refused now. So the coder model's one more correct source comes from the default model's invalid reply this time. Where the default model's two runs agree and the coder model differs, there are 5 questions:

| Question | qwen2.5:7b, both runs | qwen2.5-coder:7b |
|---|---|---|
| `v2-sg-error-status-map` | wrong | correct |
| `v2-sn-redis-durability` | no answer | correct |
| `v5-sg-sku-locked` | wrong | no answer |
| `v5-sg-api-language` | correct | no answer |
| `v5-sn-version-list-limit` | correct | no answer |

On those, the coder model has 2 more and 2 fewer correct sources, and 2 fewer wrong ones. Rule 2 also holds against the earlier run: 10 correct against 10, and 2 wrong in all against 5. So the steadier difference is fewer wrong sources, at the cost of declining more often when the right lines are there. The gain in correct sources is not shown.

**Both models spill.** Each sits 54% in GPU memory and 46% on the CPU, because 4 GB of VRAM does not hold a 7B model with an 8192-token context. The coder model spills no more than the default, and is faster here because its replies are shorter.

The negative-stock question, apart from the totals: both chose the CHECK constraints in `V2__catalog_and_stock.sql`, neither chose `SimulationService`, and neither chose the guard in `StockLevelRepository.java`, which was offered to both.
