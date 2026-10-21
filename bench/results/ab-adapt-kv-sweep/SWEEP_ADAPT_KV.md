# Adaptive-swap / KV-residency sweep — 2026-10-15 19:12 → 21:05 (`ab-adapt-kv-sweep`)

Twelve engine restarts, one per arm, each running the `agent` test 3× (6 turns, tools,
temperature 0, thinking on), on the **live** config: sc117-iq3s pack, Qwen3.8-Flash-Next
IQ3_S, 35.4 GiB of experts in system RAM, RTX 5070 12 GiB.

Started detached through WMI (`tools/run_ab_offline.ps1 -sweep`, arms in
`tools/strata_ab.py`), runner log `J:\Strata-work\ab-offline-agent.log`. All twelve arms
finished, all 18 sessions per arm (3 runs x 6 turns) reported `ok`.

> **Corrected 2026-10-21.** The decode columns below were re-derived from the
> `results.json` files in this folder — mean, range and median over the 18 samples per arm —
> and the numbers written at the time did not reproduce: the baseline row was off by eight
> points, one other row (`adapt-swaps 16`) by fourteen per cent, and the rest by less than the
> noise floor but not by nothing. The medians agree with what
> `tools/summarize_ab_sweep.py` prints for the same arms, which is the check. The `hit %`,
> `swaps` and `ovh` columns are **as recorded at the time**: nothing on disk carries them (the
> engine-log excerpts are five lines each), so they cannot be re-checked.
> `docs/SWEEP_ADAPT_KV.md` is the writeup that reproduces from the data; read it as the
> primary and this file as the running commentary.

## What was swept, and what was already pinned

| swept | values |
|---|---|
| `--adapt-swaps` | 0, 4, **8** (8 is what the config already passes) |
| `--adapt-decay` | 0.65, **0.70** (default), 0.80 |
| `--kv-resident` | **0** (the engine's default — every cell in VRAM), 20480, 24576, 32768 |
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
| **as-configured** (swaps 8, decay 0.70, kv 0, vram 640) | **16.51** | 12.10–23.40 | 16.45 | 6.6 % | 1951 | 0.0 % |
| adapt-swaps 0 | 23.82 | 20.20–27.70 | 23.40 | 6.5 % | 0 | 0.0 % |
| adapt-swaps 4 | 24.59 | 18.30–29.60 | 24.15 | 6.5 % | 976 | 0.0 % |
| adapt-swaps 16 | 26.82 | 17.30–38.80 | 26.85 | 6.6 % | 3902 | 0.0 % |
| adapt-decay 0.65 | 26.17 | 19.50–31.20 | 26.45 | 6.6 % | 1951 | 0.0 % |
| adapt-decay 0.80 | 25.97 | 18.80–33.20 | 24.50 | 6.6 % | 1951 | 0.0 % |
| kv-resident 20480 | 26.80 | 20.10–38.60 | 26.25 | 6.6 % | 1951 | 0.0 % |
| kv-resident 24576 | 26.83 | 14.80–31.10 | 27.65 | 6.6 % | 1951 | 0.0 % |
| kv-resident 32768 | 27.72 | 22.60–33.20 | 27.55 | 6.6 % | 1951 | 0.0 % |
| vram-reserve 512 | 26.67 | 21.30–35.80 | 26.55 | 6.6 % | 1951 | 0.0 % |
| vram-reserve 600 | 26.95 | 19.10–36.10 | 26.90 | 6.6 % | 1951 | 0.0 % |
| vram-reserve 768 | 26.91 | 22.30–35.40 | 26.15 | 6.6 % | 1951 | 0.0 % |
| as-configured (repeat) | 26.58 | 18.90–36.10 | 25.90 | 6.6 % | 1951 | 0.0 % |

**The reported swap overhead is 0.0 % in every single arm**, including the arm that swapped
zero experts and the one that swapped 3902. Hit rate is 6.6 % everywhere (±0.1). So the two
figures the sweep was going to judge swap cost on carry no signal at all in this harness —
the cost shows up only in the decode rate, if at all.

*(The hit column is the part of that sentence I can no longer stand behind: the `results.json`
files report a `hit_pct` of ~69 % for every arm — that is prompt-cache reuse — and
`tools/summarize_ab_sweep.py` prints ~48 % for the same arms from the per-turn records. Three
numbers, none of them the 6.6 written here. The `swaps` column at least is internally
consistent — 0 at swaps 0, 976 at swaps 4, 1951 at swaps 8, 3902 at swaps 16 — so it is
probably right; it is just not recoverable from anything now on disk.)*

## Why these numbers cannot be read as a ranking

The baseline was the **first** arm of the run and the slowest of all twelve; every other arm —
including the one that changed nothing, run last — landed 44–68 % above it. That is the
signature of the machine warming up (page-cache, GPU clocks, other tenants), not of the
knobs. The corrected table makes that plain in a way the one written at the time did not: the
spread inside a single arm is 7.5–21.5 tok/s — the baseline's own range is 11.3 wide — which
is larger than the gap between most arms. Two further problems:

- **Token parity was not verified and cannot be, with this harness.** The bench puts a
  per-run nonce in the prompts (`Suite.nonce = str(int(time.time()))`) to defeat caching, so
  every arm sees different prompts, different continuations, and different `STATE_HASH`
  values by construction. The README's parity rule ("the `STATE_HASH` line must match the
  baseline for every arm") is unsatisfiable here; a parity check needs a fixed-prompt
  harness, which the sweep plan assumed existed and does not.
- **The arms ran in a fixed order**, so arm position and arm identity are confounded. The
  only pairs that are comparable are ones measured next to each other:
  `swaps 0 vs 4 vs 16` (23.8 / 24.6 / 26.8 — monotone in swaps, which is the first time this
  run shows a trend that isn't the baseline's), and `kv 20480 / 24576 / 32768`
  (26.8 / 26.8 / 27.7 — flat), and `vram 512 / 600 / 768` (26.7 / 27.0 / 26.9 — flat, and
  512 no longer looks like anything once the numbers come back from disk).

## What is safe to conclude, and what happens next

- **`--adapt-swaps 0` and `16` are not wins.** Against the run's own repeated baseline they
  come in at −10.2 % and −4.7 % (`tools/summarize_ab_sweep.py`), and both are inside the
  noise floor that tool computes — n=78 same-turn pairs, median 10.1 %. Swapping nothing and
  swapping 3902 experts measure the same, which is its own answer: nothing to change.
- **`--kv-resident` above the default looked promising and is not**: 20480/24576/32768
  measured 62–68 % over the baseline as written here, but the baseline was cold and the
  default it runs is **0 — every cell in VRAM** (`layer.cpp:517`), so all three arms are real
  changes and all three land at −1.9 / +3.2 / +6.5 % against the warm repeat. Inside noise.
  The comparison is also *not* one flag: the config passes `--kv-grow`, and
  `generate.cpp:4228` gates the elastic KV on the KV being wholly in VRAM, so every
  `--kv-resident` arm also ran *without* `--kv-grow`. Any gain belongs to the pair — a whole
  KV in VRAM with no growth — and not to `--kv-resident` on its own. (What the sweep-parameters
  table said before this correction — that the default is 16384 and so the baseline and the
  `20480` arm ask for the same thing — was wrong in both halves: the default is 0, and 16384
  is not a value the sweep can be run at, because `layer.cpp:667` floors it to 20480.)
- **`--vram-reserve-mib 512`** (giving the VRAM pool ~200 MiB more experts) was the other
  candidate. It is the last of its own three (26.67 against 26.95 and 26.91) and −2.5 %
  against the warm repeat. Nothing there either.
- **`--adapt-decay`** is unreadable in a different way than it was written: 0.65 and 0.80
  both beat the 0.70 baseline by 57–59 %, which is exactly what a cold first arm collects,
  and against the warm repeat they are +1.2 % and +0.6 %.

A **confirmation run** follows this sweep, interleaving the baseline with the two live
candidates (`kv-resident 24576`, `vram-reserve 512`, `adapt-decay 0.65`) twice each in the
same order, so machine drift hits all four arms equally. Until that comes back, **no config
change has been made**: the live config still runs `--adapt-swaps 8`, `--vram-reserve-mib
640`, default `--kv-resident`, default `--adapt-decay`.

It has come back, and it is in `docs/SWEEP_ADAPT_KV.md`: two identical baselines eight
minutes apart sit 4.6 % apart, every arm is inside that, and the two candidates above are
inside it too. The live config is still unchanged, which is the correct outcome — and the
path that got there ran through this file's table being wrong, the cold-start effect being
measured properly in `docs/BENCH_NOISE.md`, and the tool of record learning to flag — not
score — an arm that ran inside the machine's first twenty minutes.

## Verdict (2026-10-21): nothing separated

The same story as `docs/SWEEP_ADAPT_KV.md`, from the same data and the same tool:
`python tools/read_sweep_warm.py bench/results/ab-adapt-kv-sweep` pairs every arm with the baseline turn by turn
and puts its per-turn gaps beside those of the baseline's own repeat. The largest arm gap, `adapt-swaps 16` at
72.6 %, has per-turn gaps of 43.4–96.9 %; the repeat of the unchanged configuration has 53.6–98.4 %. The arm sits
inside the spread of one command run twice, so this data cannot separate any knob from the configuration — not as a
win, and not as a loss. The ranking in the table above was the machine warming up (the baseline ran first, cold), and
the repeat row is the measurement that says so. For contrast, the cache-off batch read the same way puts its arm at
28.1–44.4 % against repeats at 1.1–24.1 %: separated, and that is the one result in this line that is.

Since 2026-10-18 the live configuration runs `--adapt-swaps 48`, after a later interleaved sweep on a different CPU,
model and link (recorded in the main line's `docs/TUNING_LOG.md`); that does not change what this sweep's data says.
