# Community benchmark on NVIDIA GeForce RTX 5070 — as configured (repeat)

Measured on 2026-10-21 with `tools/strata_bench.py` (suite 1, 2 runs per test, medians with ranges). Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no correctness check was run.

**This run changed one thing on purpose**: `as configured (repeat)`.

The same arm twice, the two taking turns (round 1 A B, round 2 B A, round 3 A B) so the machine's drift is not the same variable as an arm's position.  Nothing about the engine changes between the two arms, so any difference between them - within a round, or between rounds - is the noise, measured with the position effect broken up rather than baked in.

Everything else - model, quantization, prompts, number of runs - is the same as the other arms, which is what makes the comparison mean something. Put two arms beside each other with `python tools/strata_bench.py --compare-only <this folder> --compare <the other folder>`.

## Hardware and software

- GPU and VRAM: NVIDIA GeForce RTX 5070, 12 GiB VRAM
- CPU: 11th Gen Intel(R) Core(TM) i7-11700K @ 3.60GHz (8 cores, 16 threads); installed RAM: 63.8 GiB; storage: not measured
- OS: Windows 10 (10.0.26220); GPU driver: 617.42; CUDA: not read
- Strata: project 0.1.43, commit eccb14e5; engine 0.1.45
- What the machine was doing during the run: below

## What the machine was doing

- at the start: up 12.0 h, 10.9 GiB free RAM; GPU 420 MHz SM, 0% busy, 45 °C, 27.4 W, P1
- at the end: up 12.0 h, 8.42 GiB free RAM; GPU 180 MHz SM, 13% busy, 53 °C, 10.6 W, P8
- during: 24 samples, one every 10 s - GPU clock 180-2910 MHz (mean 1888), utilisation mean 41.1%, hottest 64 °C, most power 125.0 W, least free RAM 8.43 GiB; throttling seen: sw power cap; background work that took a core: none

The clock band and the utilisation are the two that matter: docs/BENCH_NOISE.md found the machine drifts on a schedule, and an arm whose samples sit in a different band from its comparison arm is not the same machine state, whatever the speeds say.

## Model and configuration

- Model and quantization: qwen3.8-flash-next-swift15-abliterated-iq3s-IQ3_S
- Context: 65536 tokens; parallel slots: 1
- Settings: the install's own configuration, unchanged; thinking off for the suite; temperature 0

```text
strata.exe --pack pack --native Swift-Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00001-of-00002.gguf --ple-gguf Swift-Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00001-of-00002.gguf --resident-budget-gib 36 --expert-profile expert-profile-swift-mixed-20261018.bin --expert-cache auto --prefill auto --spec 4 --spec-min-p 0.7 --mtp mtp-basalt --max-context 65536 --kv int8 --ple-io direct --vram-reserve-mib 640 --vision --adapt-swaps 48
```

## Method

Run by `python tools/strata_bench.py --runs 2 --tests agent --config strata.json --arm as configured (repeat)` against the HTTP API of a Strata server (started by the benchmark from the install configuration); model loading is not timed. One warm-up chat and a 1K prompt are discarded first. Every prompt starts with a unique tag, so the conversation cache cannot help the fresh-prompt tests.

The tests run in a fixed order (short chat, fresh prompts, agent session) and the model is **deliberately not reset** between them: this is how a working machine looks, warm and with its caches filled, which is what Strata is built for. A later test can therefore be helped by an earlier one, so compare one result with another only test by test, and never read the first tests as cold-start figures.

- short chat: the three questions of `tools/ab_engine.py`, up to 100 new tokens, 2 runs
- 4K / 16K prompts: generated word lists sized by a calibration request, 24 new tokens, 2 fresh prompts each
- agent session: system prompt and four tools, then 5 turns that each add about 1500 words of a project file and continue from the model's own earlier answers (the way agent clients resend a conversation), 64 new tokens per turn, 2 sessions; the cache numbers come from the answer's usage (`cached_tokens`) and timings (`prompt_n`)
- concurrency (when run): N requests sent at the same moment, total tokens over the wall time of the burst, latency over all requests

## Results

| Configuration | Actual prompt tokens | Reused tokens | Generated tokens | Runs | Prompt tok/s median and range | Decode tok/s median and range | TTFT seconds median and range |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| agent session, follow-up turns | 14739 (14722-14756) at the last turn | see below | up to 64 | 2 | see below | 37.1 (29.0-47.8) | 5.20 (4.34-9.91) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 68.3 % (67.7-68.9); earlier-turn tokens read again per session: 405 (145-665) (earlier turns that the cache did not hold; the 13849 (13849-13849) new tokens of the files are read once); first turn (nothing cached) first token: 6.97 s (4.59-9.34).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 647 | 0 | 647 | 175.2 | 9.344 | 26.7 |
| 1 | 3957 | 0 | 3957 | 720.6 | 9.907 | 31.0 |
| 2 | 5490 | 4020 | 1470 | 341.2 | 4.345 | 29.1 |
| 3 | 8934 | 5553 | 3381 | 573.6 | 5.934 | 29.0 |
| 4 | 11848 | 8988 | 2860 | 562.5 | 5.129 | 40.6 |
| 5 | 14756 | 11910 | 2846 | 545.7 | 5.259 | 34.9 |

### The expert cache and the drafts (the engine's own figures, not measured by this client)

Over the whole run: 21 requests, the expert cache found 62.6% of the experts it looked for already in VRAM, sent 16.2% of the routed experts over PCIe, and the prompt cache reused 65.3% of the prompt tokens; the drafts were accepted 72.8% of the time offered (634 of 871).

A larger PCIe share is NOT a better run: it is the share of the routed experts that had to be fetched over the bus.
The two cache columns do not add up to 100: the expert cache's hit rate counts its own lookups, its PCIe share counts
the experts that came over the bus, and everything else was already resident in system RAM - which is what a cache
smaller than the model is for.  The prompt cache reusing 70% of a prompt says nothing about the expert cache.

| Test | Requests | Expert cache hit % | Over PCIe % | Prompt reuse % | Drafts accepted % | Decode tok/s (engine) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| agent session | 12 | 62.6 | 16.2 | 67.8 | 66.0 | 36.8 |

These come from the server's own per-request history, matched to each test by the seconds that test ran; the whole-run line is the difference of the engine's cumulative counters, so work the server did before this run cannot enter it. A dense model, or an engine too old to report them, leaves these columns empty.

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS, and what the machine was doing during the run is the section above. Total time of the suite: 248 s.
