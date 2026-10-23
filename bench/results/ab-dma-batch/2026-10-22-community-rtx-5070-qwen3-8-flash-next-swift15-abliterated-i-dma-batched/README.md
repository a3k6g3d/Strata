# Community benchmark on NVIDIA GeForce RTX 5070 — dma batched

Measured on 2026-10-22 with `tools/strata_bench.py` (suite 1, 3 runs per test, medians with ranges). Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no correctness check was run.

**This run changed one thing on purpose**: `dma batched`.

Does one cudaMemcpyBatchAsync of the group's uploads beat the loop of one-copy-each the engine always ran (STRATA_DMA_BATCH, F12)?  Both arms run --pcie-mode dma, because the knob only has something to act on there - the configuration as it stands runs the copy kernel (auto), where the batch never runs, so a baseline left on auto would measure the same code twice.  The reading is the submission counter, which the instrumented build stamps per window in both arms and which is exact: the loop is one submission per blob (~1 blob per submission), the batch is one per group (far more).  A speed gap with that ratio unchanged on the arm did not come from the batch.

Everything else - model, quantization, prompts, number of runs - is the same as the other arms, which is what makes the comparison mean something. Put two arms beside each other with `python tools/strata_bench.py --compare-only <this folder> --compare <the other folder>`.

## Hardware and software

- GPU and VRAM: NVIDIA GeForce RTX 5070, 12 GiB VRAM
- CPU: 11th Gen Intel(R) Core(TM) i7-11700K @ 3.60GHz (8 cores, 16 threads); installed RAM: 63.8 GiB; storage: not measured
- OS: Windows 10 (10.0.26220); GPU driver: 617.42; CUDA: not read
- Strata: project 0.1.43, commit 9a2a6ca6; engine 0.1.45
- What the machine was doing during the run: below

## What the machine was doing

- at the start: up 47.1 h, 8.88 GiB free RAM; GPU 225 MHz SM, 0% busy, 51 °C, 22.5 W, P1, throttled by: sw power cap
- at the end: up 47.2 h, 6.36 GiB free RAM; GPU 180 MHz SM, 9% busy, 47 °C, 10.5 W, P8
- during: 34 samples, one every 10 s - GPU clock 180-2910 MHz (mean 1671), utilisation mean 28.1%, hottest 63 °C, most power 129.1 W, least free RAM 6.31 GiB; throttling seen: sw power cap; background work that took a core: searchindexer.exe 81%

The clock band and the utilisation are the two that matter: docs/BENCH_NOISE.md found the machine drifts on a schedule, and an arm whose samples sit in a different band from its comparison arm is not the same machine state, whatever the speeds say.

## Model and configuration

- Model and quantization: qwen3.8-flash-next-swift15-abliterated-iq3s-IQ3_S
- Context: 65536 tokens; parallel slots: 1
- Settings: the install's own configuration, unchanged; thinking off for the suite; temperature 0

```text
strata-instr.exe --pack pack --native Swift-Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00001-of-00002.gguf --ple-gguf Swift-Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00001-of-00002.gguf --resident-budget-gib 36 --expert-profile expert-profile-swift-mixed-20261018.bin --expert-cache auto --prefill auto --spec 4 --spec-min-p 0.7 --mtp mtp-basalt --max-context 65536 --kv int8 --ple-io direct --vram-reserve-mib 640 --vision --adapt-swaps 48 --pcie-mode dma
```

Environment of the config: `STRATA_VERIFY_PROFILE=1`, `STRATA_DECODE_TIMING=1`, `STRATA_RAM_TIER_CLOCK=1`, `STRATA_DMA_BATCH=1`

## Method

Run by `python tools/strata_bench.py --tests agent --config strata-instr.json --engine-arg "--pcie-mode dma" --engine-env STRATA_DMA_BATCH=1 --arm dma batched` against the HTTP API of a Strata server (started by the benchmark from the install configuration); model loading is not timed. One warm-up chat and a 1K prompt are discarded first. Every prompt starts with a unique tag, so the conversation cache cannot help the fresh-prompt tests.

The tests run in a fixed order (short chat, fresh prompts, agent session) and the model is **deliberately not reset** between them: this is how a working machine looks, warm and with its caches filled, which is what Strata is built for. A later test can therefore be helped by an earlier one, so compare one result with another only test by test, and never read the first tests as cold-start figures.

- short chat: the three questions of `tools/ab_engine.py`, up to 100 new tokens, 3 runs
- 4K / 16K prompts: generated word lists sized by a calibration request, 24 new tokens, 3 fresh prompts each
- agent session: system prompt and four tools, then 5 turns that each add about 1500 words of a project file and continue from the model's own earlier answers (the way agent clients resend a conversation), 64 new tokens per turn, 3 sessions; the cache numbers come from the answer's usage (`cached_tokens`) and timings (`prompt_n`)
- concurrency (when run): N requests sent at the same moment, total tokens over the wall time of the burst, latency over all requests

## Results

| Configuration | Actual prompt tokens | Reused tokens | Generated tokens | Runs | Prompt tok/s median and range | Decode tok/s median and range | TTFT seconds median and range |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| agent session, follow-up turns | 14728 (14712-14735) at the last turn | see below | up to 64 | 3 | see below | 41.0 (27.7-59.8) | 4.83 (4.12-9.33) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 69.0 % (67.6-69.2); earlier-turn tokens read again per session: 74 (3-745) (earlier turns that the cache did not hold; the 13847 (13847-13849) new tokens of the files are read once); first turn (nothing cached) first token: 6.97 s (6.18-8.94).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 647 | 0 | 647 | 183.3 | 8.941 | 30.3 |
| 1 | 3967 | 0 | 3967 | 750.8 | 9.326 | 30.8 |
| 2 | 5500 | 4030 | 1470 | 358.1 | 4.142 | 34.9 |
| 3 | 8944 | 5493 | 3451 | 587.9 | 5.911 | 40.9 |
| 4 | 11838 | 8978 | 2860 | 596.6 | 4.837 | 41.0 |
| 5 | 14735 | 11889 | 2846 | 595.4 | 4.823 | 43.9 |

### The expert cache and the drafts (the engine's own figures, not measured by this client)

Over the whole run: 27 requests, the expert cache found 65.9% of the experts it looked for already in VRAM, sent 14.6% of the routed experts over PCIe, and the prompt cache reused 66.3% of the prompt tokens; the drafts were accepted 74.1% of the time offered (747 of 1008).

A larger PCIe share is NOT a better run: it is the share of the routed experts that had to be fetched over the bus.
The two cache columns do not add up to 100: the expert cache's hit rate counts its own lookups, its PCIe share counts
the experts that came over the bus, and everything else was already resident in system RAM - which is what a cache
smaller than the model is for.  The prompt cache reusing 70% of a prompt says nothing about the expert cache.

| Test | Requests | Expert cache hit % | Over PCIe % | Prompt reuse % | Drafts accepted % | Decode tok/s (engine) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| agent session | 12 | 65.9 | 14.6 | 68.1 | 67.1 | 40.65 |

These come from the server's own per-request history, matched to each test by the seconds that test ran; the whole-run line is the difference of the engine's cumulative counters, so work the server did before this run cannot enter it. A dense model, or an engine too old to report them, leaves these columns empty.

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS, and what the machine was doing during the run is the section above. Total time of the suite: 355 s.
