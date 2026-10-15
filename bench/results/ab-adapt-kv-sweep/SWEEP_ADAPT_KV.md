# Adaptive-swap / KV-residency sweep — 2026-10-15 19:12 → 21:05 (`ab-adapt-kv-sweep`)

Twelve engine restarts, one per arm, each running the `agent` test 3× (6 turns, tools,
temperature 0, thinking on), on the **live** config: sc117-iq3s pack, Qwen3.8-Flash-Next
IQ3_S, 35.4 GiB of experts in system RAM, RTX 5070 12 GiB.

Started detached through WMI (`tools/run_ab_offline.ps1 -sweep`, arms in
`tools/strata_ab.py`), runner log `<work>/ab-offline-agent.log`. All twelve arms
finished, all 36 sessions per arm reported `ok`.

## What was swept, and what was already pinned

| swept | values |
|---|---|
| `--adapt-swaps` | 0, 4, **8** (8 is what the config already passes) |
| `--adapt-decay` | 0.65, **0.70** (default), 0.80 |
| `--kv-resident` | **16384** (default), 20480, 24576, 32768 |
| `--vram-reserve-mib` | 512, 600, **640** (config), 768 |

The invariants the sweep was supposed to hold — `--mtp`, `--spec 4`,
`--resident-budget-gib 36`, `--expert-profile`, `--kv int8`, `--kv-grow`, temperature 0 —
were all in the config and none of them were touched by any arm. `--pcie-frac`,
`--pool-workers` and `--expert-cache` were left unset (auto) in every arm, as planned.

**But two of the "new" values were not new**: the config already passed `--adapt-swaps 8`
and `--vram-reserve-mib 640`, so those two arms are the baseline, not candidates. Recorded
here because the plan listed them as arms.

## The numbers

Decode tok/s is the mean of the per-session means over the 3 runs (median in the last
column, to show spread); `hit %` is the expert-cache hit rate the engine reports, `swaps`
the number of experts it swapped in, `ovh` the swap overhead it reports.

| arm | decode tok/s (mean) | range | median | hit % | swaps | ovh |
|---|---|---|---|---|---|---|
| **as-configured** (swaps 8, decay 0.70, kv 16384, vram 640) | **24.72** | 24.44–25.17 | 24.75 | 6.6 % | 1951 | 0.0 % |
| adapt-swaps 0 | 23.26 | 22.96–23.64 | 23.23 | 6.5 % | 0 | 0.0 % |
| adapt-swaps 4 | 26.56 | 25.92–27.24 | 26.56 | 6.5 % | 976 | 0.0 % |
| adapt-swaps 16 | 23.47 | 23.24–23.65 | 23.51 | 6.6 % | 3902 | 0.0 % |
| adapt-decay 0.65 | 26.85 | 26.60–27.09 | 26.97 | 6.6 % | 1951 | 0.0 % |
| adapt-decay 0.80 | 27.37 | 26.95–27.78 | 27.36 | 6.6 % | 1951 | 0.0 % |
| kv-resident 20480 | 27.46 | 27.06–27.90 | 27.41 | 6.6 % | 1951 | 0.0 % |
| kv-resident 24576 | 28.06 | 27.84–28.28 | 28.08 | 6.6 % | 1951 | 0.0 % |
| kv-resident 32768 | 28.05 | 27.80–28.33 | 28.09 | 6.6 % | 1951 | 0.0 % |
| vram-reserve 512 | 28.31 | 27.99–28.59 | 28.41 | 6.6 % | 1951 | 0.0 % |
| vram-reserve 600 | 26.68 | 26.47–26.90 | 26.64 | 6.6 % | 1951 | 0.0 % |
| vram-reserve 768 | 26.82 | 26.50–27.13 | 26.86 | 6.6 % | 1951 | 0.0 % |

**The reported swap overhead is 0.0 % in every single arm**, including the arm that swapped
zero experts and the one that swapped 3902. Hit rate is 6.6 % everywhere (±0.1). So the two
figures the sweep was going to judge swap cost on carry no signal at all in this harness —
the cost shows up only in the decode rate, if at all.

## Why these numbers cannot be read as a ranking

The baseline was the **first** arm of the run and the slowest of all twelve; the last six
arms — which changed nothing about the cache policy — all landed 7–14 % above it. That is
the signature of the machine warming up (page-cache, GPU clocks, other tenants), not of the
knobs. Two further problems:

- **Token parity was not verified and cannot be, with this harness.** The bench puts a
  per-run nonce in the prompts (`Suite.nonce = str(int(time.time()))`) to defeat caching, so
  every arm sees different prompts, different continuations, and different `STATE_HASH`
  values by construction. The README's parity rule ("the `STATE_HASH` line must match the
  baseline for every arm") is unsatisfiable here; a parity check needs a fixed-prompt
  harness, which the sweep plan assumed existed and does not.
- **The arms ran in a fixed order**, so arm position and arm identity are confounded. The
  only pairs that are comparable are ones measured next to each other:
  `swaps 0 vs 4 vs 16` (23.3 / 26.6 / 23.5 — 4 looks better, but 0 and 16 straddle it),
  and `kv 20480 / 24576 / 32768` (27.5 / 28.1 / 28.1 — flat), and
  `vram 512 / 600 / 768` (28.3 / 26.7 / 26.8 — 512 looks better, but it is the first of the
  three and drift runs the other way in this run).

## What is safe to conclude, and what happens next

- **`--adapt-swaps 0` and `16` are not wins.** Swapping 0 experts and swapping 3902 both
  measured *below* the middle of the run, and both measured the same within noise; the
  reported swap overhead cannot tell them apart. Nothing to change.
- **`--kv-resident` above the default looks promising**: 20480/24576/32768 measured 11–13 %
  over the default 16384, and they were three adjacent arms, all consistent. This is the
  one candidate worth a clean test. **But the comparison is not one flag**: the config
  passes `--kv-grow`, and `generate.cpp:4228` gates the elastic KV on the KV being wholly in
  VRAM, so every `--kv-resident` arm also ran *without* `--kv-grow`. The gain belongs to the
  pair — a whole KV in VRAM with no growth — and not to `--kv-resident` on its own. (The
  default is also not really 16384: `layer.cpp:667` floors it at 20480, so the baseline and
  the `20480` arm ask for the same thing; the only real step is 24576/32768.)
- **`--vram-reserve-mib 512`** (giving the VRAM pool ~200 MiB more experts) is the other
  candidate.
- **`--adapt-decay`** is unreadable: 0.65 and 0.80 both beat the 0.70 baseline, which cannot
  both be true — one of them is drift.

A **confirmation run** follows this sweep, interleaving the baseline with the two live
candidates (`kv-resident 24576`, `vram-reserve 512`, `adapt-decay 0.65`) twice each in the
same order, so machine drift hits all four arms equally. Until that comes back, **no config
change has been made**: the live config still runs `--adapt-swaps 8`, `--vram-reserve-mib
640`, default `--kv-resident`, default `--adapt-decay`.
