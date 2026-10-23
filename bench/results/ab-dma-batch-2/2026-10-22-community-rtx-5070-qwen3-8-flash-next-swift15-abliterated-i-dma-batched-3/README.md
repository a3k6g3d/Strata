# Community benchmark on NVIDIA GeForce RTX 5070 — dma batched

Measured on 2026-10-22 with `tools/strata_bench.py` (suite 1, 3 runs per test, medians with ranges). Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no correctness check was run.

**This run changed one thing on purpose**: `dma batched`.

Does one cudaMemcpyBatchAsync of the group's uploads beat the loop of one-copy-each (STRATA_DMA_BATCH, F12)?  The first run of this sweep (bench/results/ab-dma-batch, 2026-10-22) did NOT answer it: the baseline left the variable unset and its counters read 0 on every window of all three launches - the submission path never ran there, so what got measured was copy-kernel against batch, not loop against batch.  Both arms still run --pcie-mode dma, for the same reason as before.  So BOTH arms now set the variable explicitly - 0 for the loop, 1 for the batch - and the run is void unless both sets of counters are non-zero.  The counters are exact (two atomics at the submission point, no noise floor applies): the loop is one submission per blob (~1 blob per submission), the batch one per group (far more).  One asymmetry is the code's own and cannot be arm-ed away: mode != 0 turns adaptive overlap off (generate.cpp:9501), so the arm is the flag as shipped - batch AND overlap off - against the loop WITH overlap.  That is the product question; the counters only say whether the batch itself ran.

Everything else - model, quantization, prompts, number of runs - is the same as the other arms, which is what makes the comparison mean something. Put two arms beside each other with `python tools/strata_bench.py --compare-only <this folder> --compare <the other folder>`.

## Hardware and software

- GPU and VRAM: NVIDIA GeForce RTX 5070, 12 GiB VRAM
- CPU: 11th Gen Intel(R) Core(TM) i7-11700K @ 3.60GHz (8 cores, 16 threads); installed RAM: 63.8 GiB; storage: not measured
- OS: Windows 10 (10.0.26220); GPU driver: 617.42; CUDA: not read
- Strata: project 0.1.43, commit a56bb708; engine 0.1.45
- What the machine was doing during the run: below

## What the machine was doing

- at the start: up 49.4 h, 7.59 GiB free RAM; GPU 232 MHz SM, 0% busy, 48 °C, 23.1 W, P1, throttled by: sw power cap
- at the end: up 49.5 h, 5.02 GiB free RAM; GPU 180 MHz SM, 10% busy, 48 °C, 11.0 W, P8
- during: 34 samples, one every 10 s - GPU clock 180-2910 MHz (mean 1430), utilisation mean 31.2%, hottest 63 °C, most power 125.7 W, least free RAM 4.47 GiB; throttling seen: sw power cap; background work that took a core: searchindexer.exe 62%

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
| agent session, follow-up turns | 14765 (14729-14768) at the last turn | see below | up to 64 | 3 | see below | 41.9 (25.7-49.6) | 4.91 (4.12-9.25) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 69.1 % (67.7-69.1); earlier-turn tokens read again per session: 73 (73-654) (earlier turns that the cache did not hold; the 13849 (13849-13850) new tokens of the files are read once); first turn (nothing cached) first token: 7.35 s (4.75-9.10).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 647 | 0 | 647 | 179.4 | 9.1 | 31.4 |
| 1 | 3948 | 0 | 3948 | 744.9 | 9.252 | 32.2 |
| 2 | 5481 | 4011 | 1470 | 356.7 | 4.161 | 34.4 |
| 3 | 8925 | 5545 | 3380 | 667.9 | 5.103 | 39.6 |
| 4 | 11849 | 8989 | 2860 | 594.9 | 4.853 | 44.8 |
| 5 | 14729 | 11883 | 2846 | 555.4 | 5.172 | 42.5 |

### The expert cache and the drafts (the engine's own figures, not measured by this client)

Over the whole run: 27 requests, the expert cache found 64.6% of the experts it looked for already in VRAM, sent 15.3% of the routed experts over PCIe, and the prompt cache reused 66.3% of the prompt tokens; the drafts were accepted 72.6% of the time offered (799 of 1101).

A larger PCIe share is NOT a better run: it is the share of the routed experts that had to be fetched over the bus.
The two cache columns do not add up to 100: the expert cache's hit rate counts its own lookups, its PCIe share counts
the experts that came over the bus, and everything else was already resident in system RAM - which is what a cache
smaller than the model is for.  The prompt cache reusing 70% of a prompt says nothing about the expert cache.

| Test | Requests | Expert cache hit % | Over PCIe % | Prompt reuse % | Drafts accepted % | Decode tok/s (engine) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| agent session | 12 | 64.6 | 15.3 | 68.1 | 64.7 | 41.05 |

These come from the server's own per-request history, matched to each test by the seconds that test ran; the whole-run line is the difference of the engine's cumulative counters, so work the server did before this run cannot enter it. A dense model, or an engine too old to report them, leaves these columns empty.

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS, and what the machine was doing during the run is the section above. Total time of the suite: 357 s.
