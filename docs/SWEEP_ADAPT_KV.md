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
  `STATE_HASH L=... gdn=... ple=... tail=... pooled=... kv=... stale=... ple_prev=...`. Caveat, so nobody over-reads it:
  `pooled`, `pooled_full` and `kv` are *expected* to differ in the arms that change the VRAM budget or stream the KV -
  that is the experiment, not a regression. The check that means something here is the generated token ids matching at
  temperature 0, which is what `tools/conversation_cache_parity.py:112-116` compares. These knobs move where bytes live,
  not how arithmetic is done, so equal tokens is the expectation; if they differ, the arm is not measuring what it claims.
