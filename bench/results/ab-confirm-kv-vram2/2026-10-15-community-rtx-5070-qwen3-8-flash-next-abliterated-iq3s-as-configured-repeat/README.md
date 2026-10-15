# Community benchmark on NVIDIA GeForce RTX 5070 — as configured (repeat)

Measured on 2026-10-15 with `tools/strata_bench.py` (suite 1, 3 runs per test, medians with ranges). Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no correctness check was run.

**This run changed one thing on purpose**: `as configured (repeat)`.

Which adaptation / KV-streaming setting carries the speed?  Same model, same prompts, same runs; one engine argument changed per arm, and --pcie-frac / --pool-workers / --expert-cache left to the engine's own probes in every arm.  The three --kv-resident arms also lose --kv-grow on their own (generate.cpp:4228 gates the elastic KV on the KV being wholly in VRAM), so their difference belongs to the pair, not to one flag.  The last arm repeats the baseline to measure the noise floor.

Everything else - model, quantization, prompts, number of runs - is the same as the other arms, which is what makes the comparison mean something. Put two arms beside each other with `python tools/strata_bench.py --compare-only <this folder> --compare <the other folder>`.

## Hardware and software

- GPU and VRAM: NVIDIA GeForce RTX 5070, 12 GiB VRAM
- CPU: Intel(R) Core(TM) i5-10600K CPU @ 4.10GHz (6 cores, 12 threads); installed RAM: 63.8 GiB; storage: not measured
- OS: Windows 10 (10.0.26220); GPU driver: 617.42; CUDA: not read
- Strata: project 0.1.43, commit f7cda3a0; engine 0.1.43
- Background workloads and power limits: not recorded

## Model and configuration

- Model and quantization: qwen3.8-flash-next-abliterated-iq3s
- Context: 65536 tokens; parallel slots: 1
- Settings: the install's own configuration, unchanged; thinking off for the suite; temperature 0

```text
strata.exe --pack sc117-iq3s --native Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00001-of-00002.gguf --ple-gguf Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00002-of-00002.gguf --resident-budget-gib 36 --expert-profile expert-profile-qwen38.bin --expert-cache auto --prefill auto --spec 4 --spec-min-p 0.5 --mtp mtp-rt-basalt --max-context 65536 --kv int8 --ple-io direct --vram-reserve-mib 640 --vision --adapt-swaps 8
```

## Method

Run by `python tools/strata_bench.py --tests agent --config strata.json --arm as configured (repeat)` against the HTTP API of a Strata server (started by the benchmark from the install configuration); model loading is not timed. One warm-up chat and a 1K prompt are discarded first. Every prompt starts with a unique tag, so the conversation cache cannot help the fresh-prompt tests.

The tests run in a fixed order (short chat, fresh prompts, agent session) and the model is **deliberately not reset** between them: this is how a working machine looks, warm and with its caches filled, which is what Strata is built for. A later test can therefore be helped by an earlier one, so compare one result with another only test by test, and never read the first tests as cold-start figures.

- short chat: the three questions of `tools/ab_engine.py`, up to 100 new tokens, 3 runs
- 4K / 16K prompts: generated word lists sized by a calibration request, 24 new tokens, 3 fresh prompts each
- agent session: system prompt and four tools, then 5 turns that each add about 1500 words of a project file and continue from the model's own earlier answers (the way agent clients resend a conversation), 64 new tokens per turn, 3 sessions; the cache numbers come from the answer's usage (`cached_tokens`) and timings (`prompt_n`)
- concurrency (when run): N requests sent at the same moment, total tokens over the wall time of the burst, latency over all requests

## Results

| Configuration | Actual prompt tokens | Reused tokens | Generated tokens | Runs | Prompt tok/s median and range | Decode tok/s median and range | TTFT seconds median and range |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| agent session, follow-up turns | 14754 (14728-14754) at the last turn | see below | up to 64 | 3 | see below | 25.0 (21.1-29.2) | 7.52 (7.19-7.92) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 69.0 % (68.9-69.1); earlier-turn tokens read again per session: 119 (74-144) (earlier turns that the cache did not hold; the 13847 (13847-13848) new tokens of the files are read once); first turn (nothing cached) first token: 4.06 s (4.03-5.62).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 647 | 0 | 647 | 116.2 | 5.619 | 21.4 |
| 1 | 3959 | 665 | 3294 | 433.0 | 7.663 | 21.1 |
| 2 | 5492 | 3952 | 1540 | 211.3 | 7.344 | 25.0 |
| 3 | 8936 | 5555 | 3381 | 449.4 | 7.586 | 23.6 |
| 4 | 11859 | 8999 | 2860 | 381.7 | 7.553 | 28.4 |
| 5 | 14754 | 11908 | 2846 | 385.7 | 7.439 | 27.2 |

### The expert cache and the drafts (the engine's own figures, not measured by this client)

Over the whole run: 22 requests, the expert cache found 47.9% of the experts it looked for already in VRAM, sent 6.2% of the routed experts over PCIe, and the prompt cache reused 66.8% of the prompt tokens; the drafts were accepted 57.7% of the time offered (578 of 1002).

A larger PCIe share is NOT a better run: it is the share of the routed experts that had to be fetched over the bus.
The two cache columns do not add up to 100: the expert cache's hit rate counts its own lookups, its PCIe share counts
the experts that came over the bus, and everything else was already resident in system RAM - which is what a cache
smaller than the model is for.  The prompt cache reusing 70% of a prompt says nothing about the expert cache.

| Test | Requests | Expert cache hit % | Over PCIe % | Prompt reuse % | Drafts accepted % | Decode tok/s (engine) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| agent session | 12 | 47.9 | 6.2 | 67.9 | 55.3 | 24.5 |

These come from the server's own per-request history, matched to each test by the seconds that test ran; the whole-run line is the difference of the engine's cumulative counters, so work the server did before this run cannot enter it. A dense model, or an engine too old to report them, leaves these columns empty.

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS; background load and power limits are not recorded. Total time of the suite: 398 s.
