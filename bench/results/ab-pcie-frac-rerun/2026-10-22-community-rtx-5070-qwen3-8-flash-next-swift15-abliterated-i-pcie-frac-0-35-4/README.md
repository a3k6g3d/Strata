# Community benchmark on NVIDIA GeForce RTX 5070 — pcie-frac 0.35

Measured on 2026-10-22 with `tools/strata_bench.py` (suite 1, 2 runs per test, medians with ranges). Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no correctness check was run.

**This run changed one thing on purpose**: `pcie-frac 0.35`.

Does the PCIe share of the missed experts shorten the decode window, on the arm the last batch led on? The automatic share against a fixed 0.35, five rounds, the arms taking turns, both arms with the profiler stamps on (so read the shares, not the absolute milliseconds, and compare arms to arms only). The baseline's five launches are the batch's own noise.

Everything else - model, quantization, prompts, number of runs - is the same as the other arms, which is what makes the comparison mean something. Put two arms beside each other with `python tools/strata_bench.py --compare-only <this folder> --compare <the other folder>`.

## Hardware and software

- GPU and VRAM: NVIDIA GeForce RTX 5070, 12 GiB VRAM
- CPU: 11th Gen Intel(R) Core(TM) i7-11700K @ 3.60GHz (8 cores, 16 threads); installed RAM: 63.8 GiB; storage: not measured
- OS: Windows 10 (10.0.26220); GPU driver: 617.42; CUDA: not read
- Strata: project 0.1.43, commit 72bf08fc; engine 0.1.45
- What the machine was doing during the run: below

## What the machine was doing

- at the start: up 37.1 h, 9.28 GiB free RAM; GPU 330 MHz SM, 0% busy, 50 °C, 23.1 W, P1, throttled by: sw power cap
- at the end: up 37.2 h, 6.52 GiB free RAM; GPU 180 MHz SM, 11% busy, 50 °C, 10.3 W, P8
- during: 25 samples, one every 10 s - GPU clock 180-2910 MHz (mean 1500), utilisation mean 41.8%, hottest 61 °C, most power 117.7 W, least free RAM 6.52 GiB; throttling seen: sw power cap; background work that took a core: searchindexer.exe 34%

The clock band and the utilisation are the two that matter: docs/BENCH_NOISE.md found the machine drifts on a schedule, and an arm whose samples sit in a different band from its comparison arm is not the same machine state, whatever the speeds say.

## Model and configuration

- Model and quantization: qwen3.8-flash-next-swift15-abliterated-iq3s-IQ3_S
- Context: 65536 tokens; parallel slots: 1
- Settings: the install's own configuration, unchanged; thinking off for the suite; temperature 0

```text
strata.exe --pack pack --native Swift-Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00001-of-00002.gguf --ple-gguf Swift-Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00001-of-00002.gguf --resident-budget-gib 36 --expert-profile expert-profile-swift-mixed-20261018.bin --expert-cache auto --prefill auto --spec 4 --spec-min-p 0.7 --mtp mtp-basalt --max-context 65536 --kv int8 --ple-io direct --vram-reserve-mib 640 --vision --adapt-swaps 48 --pcie-frac 0.35
```

Environment of the config: `STRATA_VERIFY_PROFILE=1`, `STRATA_DECODE_TIMING=1`

## Method

Run by `python tools/strata_bench.py --runs 2 --tests agent --config strata-pcieprof.json --engine-arg "--pcie-frac 0.35" --arm pcie-frac 0.35` against the HTTP API of a Strata server (started by the benchmark from the install configuration); model loading is not timed. One warm-up chat and a 1K prompt are discarded first. Every prompt starts with a unique tag, so the conversation cache cannot help the fresh-prompt tests.

The tests run in a fixed order (short chat, fresh prompts, agent session) and the model is **deliberately not reset** between them: this is how a working machine looks, warm and with its caches filled, which is what Strata is built for. A later test can therefore be helped by an earlier one, so compare one result with another only test by test, and never read the first tests as cold-start figures.

- short chat: the three questions of `tools/ab_engine.py`, up to 100 new tokens, 2 runs
- 4K / 16K prompts: generated word lists sized by a calibration request, 24 new tokens, 2 fresh prompts each
- agent session: system prompt and four tools, then 5 turns that each add about 1500 words of a project file and continue from the model's own earlier answers (the way agent clients resend a conversation), 64 new tokens per turn, 2 sessions; the cache numbers come from the answer's usage (`cached_tokens`) and timings (`prompt_n`)
- concurrency (when run): N requests sent at the same moment, total tokens over the wall time of the burst, latency over all requests

## Results

| Configuration | Actual prompt tokens | Reused tokens | Generated tokens | Runs | Prompt tok/s median and range | Decode tok/s median and range | TTFT seconds median and range |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| agent session, follow-up turns | 14722 (14698-14745) at the last turn | see below | up to 64 | 2 | see below | 40.0 (27.5-49.6) | 5.17 (4.37-10.01) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 68.3 % (67.5-69.0); earlier-turn tokens read again per session: 398 (72-725) (earlier turns that the cache did not hold; the 13850 (13849-13850) new tokens of the files are read once); first turn (nothing cached) first token: 7.00 s (4.49-9.51).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 647 | 0 | 647 | 164.5 | 9.511 | 27.9 |
| 1 | 3948 | 0 | 3948 | 700.9 | 10.008 | 27.5 |
| 2 | 5481 | 3941 | 1540 | 351.5 | 4.417 | 31.6 |
| 3 | 8925 | 5545 | 3380 | 373.3 | 9.1 | 35.8 |
| 4 | 11811 | 8951 | 2860 | 557.7 | 5.186 | 38.5 |
| 5 | 14698 | 11852 | 2846 | 557.3 | 5.154 | 42.1 |

### The expert cache and the drafts (the engine's own figures, not measured by this client)

Over the whole run: 21 requests, the expert cache found 59.6% of the experts it looked for already in VRAM, sent 12.0% of the routed experts over PCIe, and the prompt cache reused 65.3% of the prompt tokens; the drafts were accepted 79.7% of the time offered (637 of 799).

A larger PCIe share is NOT a better run: it is the share of the routed experts that had to be fetched over the bus.
The two cache columns do not add up to 100: the expert cache's hit rate counts its own lookups, its PCIe share counts
the experts that came over the bus, and everything else was already resident in system RAM - which is what a cache
smaller than the model is for.  The prompt cache reusing 70% of a prompt says nothing about the expert cache.

| Test | Requests | Expert cache hit % | Over PCIe % | Prompt reuse % | Drafts accepted % | Decode tok/s (engine) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| agent session | 12 | 59.6 | 12.0 | 67.7 | 74.1 | 37.6 |

These come from the server's own per-request history, matched to each test by the seconds that test ran; the whole-run line is the difference of the engine's cumulative counters, so work the server did before this run cannot enter it. A dense model, or an engine too old to report them, leaves these columns empty.

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS, and what the machine was doing during the run is the section above. Total time of the suite: 256 s.
