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
