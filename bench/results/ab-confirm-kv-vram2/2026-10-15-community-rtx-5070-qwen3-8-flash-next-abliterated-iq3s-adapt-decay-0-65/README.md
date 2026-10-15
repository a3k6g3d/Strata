# Community benchmark on NVIDIA GeForce RTX 5070 — adapt-decay 0.65

Measured on 2026-10-15 with `tools/strata_bench.py` (suite 1, 3 runs per test, medians with ranges). Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no correctness check was run.

**This run changed one thing on purpose**: `adapt-decay 0.65`.

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
strata.exe --pack sc117-iq3s --native Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00001-of-00002.gguf --ple-gguf Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ3_S-00002-of-00002.gguf --resident-budget-gib 36 --expert-profile expert-profile-qwen38.bin --expert-cache auto --prefill auto --spec 4 --spec-min-p 0.5 --mtp mtp-rt-basalt --max-context 65536 --kv int8 --ple-io direct --vram-reserve-mib 640 --vision --adapt-swaps 8 --adapt-decay 0.65
```

## Method

Run by `python tools/strata_bench.py --tests agent --config strata.json --engine-arg "--adapt-decay 0.65" --arm adapt-decay 0.65` against the HTTP API of a Strata server (started by the benchmark from the install configuration); model loading is not timed. One warm-up chat and a 1K prompt are discarded first. Every prompt starts with a unique tag, so the conversation cache cannot help the fresh-prompt tests.

The tests run in a fixed order (short chat, fresh prompts, agent session) and the model is **deliberately not reset** between them: this is how a working machine looks, warm and with its caches filled, which is what Strata is built for. A later test can therefore be helped by an earlier one, so compare one result with another only test by test, and never read the first tests as cold-start figures.

- short chat: the three questions of `tools/ab_engine.py`, up to 100 new tokens, 3 runs
- 4K / 16K prompts: generated word lists sized by a calibration request, 24 new tokens, 3 fresh prompts each
- agent session: system prompt and four tools, then 5 turns that each add about 1500 words of a project file and continue from the model's own earlier answers (the way agent clients resend a conversation), 64 new tokens per turn, 3 sessions; the cache numbers come from the answer's usage (`cached_tokens`) and timings (`prompt_n`)
- concurrency (when run): N requests sent at the same moment, total tokens over the wall time of the burst, latency over all requests

## Results

| Configuration | Actual prompt tokens | Reused tokens | Generated tokens | Runs | Prompt tok/s median and range | Decode tok/s median and range | TTFT seconds median and range |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| agent session, follow-up turns | 14762 (14752-14765) at the last turn | see below | up to 64 | 3 | see below | 26.6 (23.3-36.8) | 7.52 (7.16-7.85) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 69.1 % (69.0-69.1); earlier-turn tokens read again per session: 74 (72-74) (earlier turns that the cache did not hold; the 13849 (13848-13850) new tokens of the files are read once); first turn (nothing cached) first token: 4.08 s (4.06-5.61).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 647 | 0 | 647 | 116.5 | 5.606 | 22.5 |
| 1 | 3962 | 667 | 3295 | 434.2 | 7.647 | 23.9 |
| 2 | 5495 | 4025 | 1470 | 205.0 | 7.221 | 23.7 |
| 3 | 8939 | 5488 | 3451 | 458.5 | 7.581 | 25.1 |
| 4 | 11858 | 8998 | 2860 | 385.0 | 7.497 | 27.7 |
| 5 | 14762 | 11916 | 2846 | 382.8 | 7.495 | 26.6 |

### The expert cache and the drafts (the engine's own figures, not measured by this client)

Over the whole run: 22 requests, the expert cache found 48.6% of the experts it looked for already in VRAM, sent 6.1% of the routed experts over PCIe, and the prompt cache reused 66.9% of the prompt tokens; the drafts were accepted 62.0% of the time offered (619 of 999).

A larger PCIe share is NOT a better run: it is the share of the routed experts that had to be fetched over the bus.
The two cache columns do not add up to 100: the expert cache's hit rate counts its own lookups, its PCIe share counts
the experts that came over the bus, and everything else was already resident in system RAM - which is what a cache
smaller than the model is for.  The prompt cache reusing 70% of a prompt says nothing about the expert cache.

| Test | Requests | Expert cache hit % | Over PCIe % | Prompt reuse % | Drafts accepted % | Decode tok/s (engine) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| agent session | 12 | 48.6 | 6.1 | 68.1 | 60.7 | 26.85 |

These come from the server's own per-request history, matched to each test by the seconds that test ran; the whole-run line is the difference of the engine's cumulative counters, so work the server did before this run cannot enter it. A dense model, or an engine too old to report them, leaves these columns empty.

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS; background load and power limits are not recorded. Total time of the suite: 397 s.
