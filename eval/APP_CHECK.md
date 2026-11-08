# A small check of the real flow with qwen2.5-coder:7b

Written before the run, with the questions in [`app_check.yaml`](app_check.yaml) fixed and committed first. The tool is [`app_check.py`](tools/app_check.py).

## Why

The [model trial](MODEL_TRIAL.md) gave both models the stored qwen2.5:7b translations, so that only the choice of lines differed. In the application, the configured model also translates every Turkish question. The local opus-mt translator adds a second wording, whatever the model. So with `qwen2.5-coder:7b` configured, the Turkish translation changes too, and the trial did not measure that.

## What runs

- **Six new questions** through `POST /api/ask` on a server started for this check, with `REPOMIND_LLM_MODEL=qwen2.5-coder:7b` in its own environment only. `.env` and the default model stay as they are. There are three questions in Turkish and three in English, and one in each language is unanswerable. They were written by reading the code at the pinned commits, in regions no earlier question accepts.
- **The negative-stock question**, which has been read many times. It is reported apart, and not counted with the six.
- **The 32 development questions of the trials**, translated by the same model, still loaded, with the engine's own translation prompt and parsing. They are compared with the stored qwen2.5:7b translations. Then, with no model, each translation is searched once, to see whether the right code still reaches the candidates, the passages and the evidence rule.

## What is reported

For each of the six:

- the English wording the model made;
- whether the answer is a correct source, a wrong one or none;
- for an unanswerable question, whether it was given a source;
- each returned line checked against Git at its commit, and the GitHub link built the way the web application builds it, with whether GitHub has that file at that commit;
- seconds, tokens, peak GPU memory, and the lowest free memory and commit space.

Six questions prove nothing statistically, and this is not an evaluation. It is a check that the flow works with this model, and of what its translations change.

## Safety

The check starts only when two readings a second apart each show 12 GB of free physical memory and 12 GB of free commit space, and only when no other model is in the model server's memory. Only `qwen2.5-coder:7b` is loaded, and it is unloaded at the end. The application records each request's token use in its own usage table, as it does for any question.
