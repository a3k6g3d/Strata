# Community benchmark on NVIDIA GeForce RTX 5070 — as configured

Measured on 2026-10-14 with `tools/strata_bench.py` (suite 1, 8 runs per test, medians with ranges). Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no correctness check was run.

**This run changed one thing on purpose**: `as configured`.

Does the expert cache carry the speed, or the drafts? Same model, same prompts, same runs; one engine argument changed per arm. Decode tok/s is the engine's own figure; the cache hit and PCIe share come from the engine's per-request records, matched to each test by their own seconds.

Everything else - model, quantization, prompts, number of runs - is the same as the other arms, which is what makes the comparison mean something. Put two arms beside each other with `python tools/strata_bench.py --compare-only <this folder> --compare <the other folder>`.

## Hardware and software

- GPU and VRAM: NVIDIA GeForce RTX 5070, 12 GiB VRAM
- CPU: Intel(R) Core(TM) i5-10600K CPU @ 4.10GHz (6 cores, 12 threads); installed RAM: 63.8 GiB; storage: not measured
- OS: Windows 10 (10.0.26220); GPU driver: 617.42; CUDA: not read
- Strata: project 0.1.43, commit d7cb238f; engine 0.1.43
- Background workloads and power limits: not recorded

## Model and configuration

- Model and quantization: qwen3.8-flash-next-abliterated-iq3s
- Context: 65536 tokens; parallel slots: 1
- Settings: the install's own configuration, unchanged; thinking off for the suite; temperature 0

```text
strata.exe --pack sc117-iq3s --native Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00001-of-00002.gguf --ple-gguf Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00002-of-00002.gguf --resident-budget-gib 36 --expert-profile expert-profile-qwen38.bin --expert-cache auto --prefill auto --spec 4 --spec-min-p 0.5 --mtp mtp-rt-basalt --max-context 65536 --kv int8 --ple-io direct --vram-reserve-mib 640 --vision --adapt-swaps 8
```

## Method

Run by `python tools/strata_bench.py --runs 8 --tests agent --config strata.json --arm as configured` against the HTTP API of a Strata server (started by the benchmark from the install configuration); model loading is not timed. One warm-up chat and a 1K prompt are discarded first. Every prompt starts with a unique tag, so the conversation cache cannot help the fresh-prompt tests.

The tests run in a fixed order (short chat, fresh prompts, agent session) and the model is **deliberately not reset** between them: this is how a working machine looks, warm and with its caches filled, which is what Strata is built for. A later test can therefore be helped by an earlier one, so compare one result with another only test by test, and never read the first tests as cold-start figures.

- short chat: the three questions of `tools/ab_engine.py`, up to 100 new tokens, 8 runs
- 4K / 16K prompts: generated word lists sized by a calibration request, 24 new tokens, 8 fresh prompts each
- agent session: system prompt and four tools, then 5 turns that each add about 1500 words of a project file and continue from the model's own earlier answers (the way agent clients resend a conversation), 64 new tokens per turn, 8 sessions; the cache numbers come from the answer's usage (`cached_tokens`) and timings (`prompt_n`)
- concurrency (when run): N requests sent at the same moment, total tokens over the wall time of the burst, latency over all requests

## Results

| Configuration | Actual prompt tokens | Reused tokens | Generated tokens | Runs | Prompt tok/s median and range | Decode tok/s median and range | TTFT seconds median and range |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| agent session, follow-up turns | 14766 (14735-14795) at the last turn | see below | up to 64 | 8 | see below | 24.0 (13.0-35.0) | 8.52 (7.68-10.19) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 69.2 % (68.9-69.4); earlier-turn tokens read again per session: 40 (3-143) (earlier turns that the cache did not hold; the 13849 (13847-13851) new tokens of the files are read once); first turn (nothing cached) first token: 5.81 s (5.54-7.41).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 647 | 0 | 647 | 112.2 | 5.819 | 21.3 |
| 1 | 4006 | 710 | 3296 | 391.7 | 8.476 | 20.4 |
| 2 | 5539 | 4069 | 1470 | 188.2 | 7.873 | 16.6 |
| 3 | 8983 | 5602 | 3381 | 354.2 | 9.646 | 13.5 |
| 4 | 11907 | 9047 | 2860 | 341.0 | 8.457 | 23.8 |
| 5 | 14795 | 11949 | 2846 | 287.2 | 10.186 | 13.0 |

### The expert cache and the drafts (the engine's own figures, not measured by this client)

Over the whole run: 52 requests, the expert cache found 0.0% of the routed experts already in VRAM and sent 9.1% of them over PCIe; the drafts were accepted 61.3% of the time offered (1608 of 2625).

A larger PCIe share is NOT a better run: it is the share of the routed experts that had to be fetched over the bus.

| Test | Requests | Cache hit % | Over PCIe % | Drafts accepted % | Decode tok/s (engine) |
| --- | ---: | ---: | ---: | ---: | ---: |
| agent session | 12 | 0.0 | 9.1 | 63.9 | 23.65 |

These come from the server's own per-request history, matched to each test by the seconds that test ran; the whole-run line is the difference of the engine's cumulative counters, so work the server did before this run cannot enter it. A dense model, or an engine too old to report them, leaves these columns empty.

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS; background load and power limits are not recorded. Total time of the suite: 527 s.
