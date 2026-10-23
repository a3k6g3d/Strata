# Is the CPU expert pool compute-bound? AVX-512 against AVX2 i-quant rows

**Answer: no.** Making the CPU's expert math cheaper does not make decode faster on this machine. The engine's
AVX-512 i-quant rows (the default on a CPU that has AVX-512) and its AVX2 rows (`STRATA_NO_IQ512=1`) decode at the
same speed, inside the noise. The CPU pool is bound by memory, not by arithmetic, so a CPU kernel tuned for this
processor would buy nothing measurable here; faster RAM is the lever.

Measured 2026-10-23 on an i7-11700K (AVX-512), RTX 5070 12 GB, 64 GB DDR4-2666 (XMP), PCIe Gen4 x16, engine 0.1.45
(`build-0145`), Swift 1.5 GSQ-RCO abliterated IQ3_S with the Basalt MTP draft (`--spec 4 --spec-min-p 0.7`),
`--adapt-swaps 48`, `--resident-budget-gib 36`, 64K context.

## Why ask

The window profile (`docs/WINDOW_PROFILE_CUDA.md`) puts the per-layer host time at 31.4 ms of a 59.4 ms window, 28.2
ms of it the CPU pool - and that pool reads about 17 GB/s of expert bytes against roughly 42 GB/s theoretical for
dual-channel DDR4-2666. Half the bandwidth unused could mean the pool is compute-bound (then a better kernel helps) or
latency-bound on memory (then it does not). Swapping the kernel for a slower one tells the two apart: if the pool
were compute-bound, the AVX2 rows would be clearly slower.

## Method

`tools/strata_ab.py --sweep iq512 --rounds 3 --runs 6 --tests chat,agent` - two arms taking turns, one engine launch
each, six launches: the configured server, and the same server with `--engine-env=STRATA_NO_IQ512=1`. Nothing else
differs. The engine does not log which kernel it chose; the AVX2 arm is known to have run with the variable set
(the arm's results and folder name carry it), not that the selection inside the engine was observed.

## Result

| launch | arm | decode tok/s | VRAM hit % |
|---|---|---|---|
| 1 | AVX-512 (default) | 34.85\* | 61.8 |
| 2 | AVX2 | 35.25\* | 64.3 |
| 3 | AVX2 | 32.70\* | 61.4 |
| 4 | AVX-512 | 35.70\* | 61.1 |
| 5 | AVX-512 | 35.80 | 62.6 |
| 6 | AVX2 | 35.15 | 63.2 |

\* started inside the machine's first 20 minutes (`docs/BENCH_NOISE.md`); the round median leaves those out when
others remain.

Per round, AVX2 against AVX-512: +0.4\*, -4.1\*, -0.1 - **median -0.1%, inside the noise** (the reference arm's own
launches spread 2.7%). The one slow launch (32.7) is a cold-window AVX2 launch; the warm pair agrees to 0.1%.

Raw: `bench/results/ab-iq512/`. Read with `python tools/summarize_ab_sweep.py bench/results/ab-iq512`.

## What it leaves open

- Whether a different memory access pattern in the pool (prefetching, larger batches per expert) would use more of
  the bandwidth. This test only says the arithmetic is not the limit.
- Three rounds is enough to rule out a large effect, not a small one.
