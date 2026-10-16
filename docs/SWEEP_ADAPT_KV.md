# Sweeping the adaptation and KV-streaming knobs (2026-10-14)

The audit in `REJECTED_PROPOSALS.md` left three real levers: the bus share, the pool width and the KV/expert VRAM split.
The first two are automatic and stay automatic. The third is what this sweep varies.

Run it with the server out of the way, one PowerShell window, ~40 min per arm:

```
Run-AB.cmd  (or:)  powershell -File tools\run_ab_offline.ps1 -armName "<arm;arm;...>" -runs 3 `
                       -outOverride bench\results\ab-adapt-kv-sweep
```

Arms are in `tools/strata_ab.py` next to the cache-vs-drafts ones. The suite is the `agent` suite (`run_ab_offline.ps1:22`),
because a prompt that never repeats gives the cache nothing to hit - the first run of this found a 0.0% hit rate for
exactly that reason.

## Pinned in every arm

The configuration's own: `--mtp J:/Strata-work/mtp-rt-basalt`, `--spec 4`, `--spec-min-p 0.5`, `--resident-budget-gib 36`,
`--expert-profile .../expert-profile-qwen38.bin`, `--kv int8`, `--kv-grow`, `--max-context 65536`, `--prefill auto`,
`--ple-io direct`, temperature 0. Nothing else changes between arms; that is the whole point of an A/B.

Left unset so the engine decides for this machine: `--pcie-frac` (link probe + PcieAuto), `--pool-workers` (topology +
affinity), `--expert-cache auto` (fit calculation). See `REJECTED_PROPOSALS.md` items 2 and 3.

## Varied, one at a time

| arm | changes | against |
| --- | --- | --- |
| `as configured` | nothing - the baseline | - |
| `adapt-swaps 0` / `4` / `16` | `--adapt-swaps` | baseline's 8 |
| `adapt-decay 0.65` / `0.80` | `--adapt-decay` | baseline's 0.7 (the engine's default) |
| `kv-resident 20480` / `24576` / `32768` | `--kv-resident` | baseline's 0 (all KV in VRAM) |
| `vram-reserve 512` / `600` / `768` | `--vram-reserve-mib` | baseline's 640 |
| `as configured (repeat)` | nothing, run last | the baseline, hours later |

Two of the requested values turned out to already be the baseline, so they are not run twice: `strata.json` already sets
`--adapt-swaps 8` and `--vram-reserve-mib 640`, and `--adapt-decay` is not in the configuration at all, where the engine's
own default is 0.7 (`generate.cpp:582`). Sweeping 0.70 would have measured the baseline a second time under a different
label. The last arm measures it on purpose: the earlier arms on this machine showed decode medians swinging 13-35 tok/s
*within* one arm, so an arm is only worth believing if a same-configuration arm says how much of that was noise.

## Two interactions the sweep has to be honest about

These are the reason this file exists. Both were found in the source before the first arm ran; both would have quietly
invalidated a conclusion.

**1. `--kv-resident` switches `--kv-grow` off.** The elastic K/V is gated on the KV being entirely in VRAM:

```cpp
// generate.cpp:4228
const bool on = asked && !multi_gpu && o.kv_resident <= 0 && !o.expert_profile.empty() && ...
```

So `--kv-grow` is passed in all arms, as pinned above, but in the three `--kv-resident` arms the engine turns it off by
itself. Those arms are therefore *not* single-variable changes: streaming the KV and losing the elastic KV arrive
together. Any speed difference there belongs to the pair, not to `--kv-resident`. The INFO line
(`kv=%s kv_resident=%lld ... expert_slots=%lld`, `generate.cpp:8975`) shows which arms are affected, so this is checkable
after the run rather than something to take on trust.

**2. `--kv-resident 16384` does not mean 16384.** The floor is a constant, and the plan clamps to it:

```cpp
// layer.cpp:667
int64_t qsa_kv_resident_min() { return 20480; }
// layer.cpp:648
const int64_t r = (std::max(g_kv_resident, qsa_kv_resident_min()) + s.page_size - 1) / s.page_size;
```

The requested 16384 would have been silently raised to 20480 (`--help` says so too: "keep N cells of each QSA layer in
VRAM (min 20480)"), so that arm would have measured 20480 while being labelled 16384. The sweep starts at the floor
instead. Note `generate.cpp:2462` only rejects negative values, so nothing would have complained.

## What is being measured

- **decode tok/s** - the engine's own figure, the headline.
- **hit rate** - the expert cache's, from the engine's per-request records, not the client's prompt reuse. (The reporting
  bug that made these read 0.0% is fixed; see `CACHE_VS_DRAFTS.md`.)
- **swap overhead** - `--adapt-swaps` is "every `adapt_every` rounds, swap up to `adapt_swaps` of the most-routed missing
  experts into the cache" (`generate.cpp:579`). Its cost shows as the PCIe share and as decode dips between adaptation
  rounds, not as a field of its own.
- **token parity** - `STRATA_STATE_HASH=1` is set for the whole run, so each arm's engine log carries
  `STATE_HASH L=... gdn=... ple=... tail=... pooled=... kv=... stale=... ple_prev=...`. Two caveats, and the second one
  killed this measure. `pooled`, `pooled_full` and `kv` are *expected* to differ in the arms that change the VRAM budget
  or stream the KV - that is the experiment, not a regression. And the deeper problem: the generated tokens do not match
  between arms **or between two runs of the same arm**, at temperature 0, so the fingerprints have nothing to line up
  against. `docs/REJECTED_PROPOSALS.md` item 6 shows the numbers. What was written here first - "these knobs move where
  bytes live, not how arithmetic is done, so equal tokens is the expectation" - was true of the arithmetic and wrong of
  the sampling. Token parity is not a gate for this sweep; the noise floor below is.

## The result, and the number that decides it (2026-10-15)

The twelve arms above were run, and a shortened re-cut of four of them was run afterwards (`ab-confirm-kv-vram` for the
baseline twice, `ab-confirm-kv-vram2` for three arms plus the baseline again). Read any arm with
`python tools/summarize_ab_sweep.py bench/results/<folder>`, which prints one row per arm and finds the noise floor
itself:

| arm | decode tok/s | engine's own | hit % | PCIe % | draft accepted % |
| --- | --- | --- | --- | --- | --- |
| `as configured` (7:54) | 25.7 | 27.1 | 48.2 | 6.2 | 61.7 |
| `as configured` (8:02) | 26.9 | 28.55 | 46.2 | 6.3 | 68.6 |
| `adapt-decay 0.65` | 26.55 | 26.85 | 48.6 | 6.1 | 60.7 |
| `kv-resident 24576` | 27.45 | 28.45 | 51.4 | 5.7 | 62.2 |
| `vram-reserve 512` | 26.95 | 28.45 | 48.5 | 6.0 | 63.7 |

**The noise floor is 4.6% of decode** - that is the spread between the two identical baselines (25.7 and 26.9 tok/s,
8 minutes apart, GPU otherwise empty). Every arm above is inside it. `kv-resident 24576` looks 5.2% over the first
baseline and 1.9% over the second; with a floor of 4.6% those two readings of the same arm mean the same thing, which is
nothing. The same holds for the twelve-arm sweep, where `adapt-swaps 16` (26.85) and `adapt-decay 0.65` (26.45) led the
field and the baseline's own repeat scored 25.9 - and where the baseline's *first* run came in at 16.45 tok/s with a
10.12 s first token, 10 points below its own repeat, for a reason nothing in the logs explains. It was the *first arm
of the session*: the machine has a cold region for its first ~20 minutes of benching, and the 2nd and 3rd arms of this
sweep (`adapt-swaps 0` at 23.4, `adapt-swaps 4` at 24.15) sit in it too. Everything started after minute 20 lands in
24.5-27.65 (`BENCH_NOISE.md`).

So the honest conclusion, twice over: **none of these four knobs carries the speed on this machine**, and the ones that
move the hit rate visibly (`--adapt-swaps 0` drops the expert cache from ~48% to 24.9%, `16` raises it to 56.9%) do not
move the speed in a way these arms can attribute to the flag. That is worth knowing before anyone tunes them: the
engine's own defaults are already at a flat part of the curve, which is the same conclusion `CACHE_VS_DRAFTS.md` reached
about drafts. Weaker, and stated here deliberately: the `--adapt-swaps 0` and `4` speeds above cannot be read as an
effect at all, in either direction, because those two arms were the 2nd and 3rd of a session that behaves differently
for its first 20 minutes.

Two things this run could not settle, kept here so they are not mistaken for settled: the `--kv-resident` arms are not
single-variable (interaction 1 above), and one decode median per arm over 18 turns is not enough to resolve anything
under ~5%. Settling it would mean 8-10 runs per arm and hours per arm, on a machine whose own baseline moved 10 tok/s
between runs. If anyone re-runs this, run the baseline twice and subtract what it says.

That subtraction now has numbers attached to it, read out of the 414 pairs of same-turn runs already sitting in these
folders (3-run arms) without re-running anything: two runs of one arm's one turn differ by **12.1% at the median**, 29.3%
at the 90th percentile, 71% at the worst; on the two 8-run arms, which sample the tail properly, 20.3% / 57.0% / 110.1%.
A 4-5% gap between two arm medians is therefore not a finding, and neither is the 10-point baseline movement above.
Worse than noise, the arms in this sweep are not independent: position in the session correlates with decode at +0.40
and with hit rate at +0.46, and the drift stops after ~20 minutes. See `BENCH_NOISE.md` for the cold region, for what
does carry the speed instead (draft acceptance, +0.44 within an arm where the flags are identical; hit rate +0.11 across
arms), and for the five things to change so the next sweep can resolve what this one could not.
