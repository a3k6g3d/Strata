# Where a decode window goes on this CUDA build (2026-10-21)

Build-plan items 1 and 2 (`docs/BUILD_PLAN.json`), measured on this machine. Every number below reproduces with

```
python tools/read_window_profile.py bench/results/window-profile-2026-10-21/engine-log.txt
```

## How it was measured

- Machine: RTX 5070 12 GB on PCIe Gen4 x16 (the engine's own probe in this run: 27.2 GB/s host to device), i7-11700K,
  64 GB DDR4-2666. Model: Swift 1.5 GSQ-RCO abliterated IQ3_S, engine `build-0145` (upstream 0.1.45), the live arguments
  (`--adapt-swaps 48`, `--resident-budget-gib 36`, `--spec 4`, the Swift expert profile), `--pcie-frac` left automatic
  (it chose 0.55).
- The live configuration with Discord and the conversation store removed, plus `STRATA_VERIFY_PROFILE=1` and
  `STRATA_DECODE_TIMING=1` in its `env` block. With both set, `strata --serve` prints, per request, the window split
  host-side (`strata decode timing:`) and every GPU stage of the window (`strata decode GPU stages (ms/window):`).
- Workload: `tools/strata_bench.py --tests chat,agent --runs 2` against that server (thinking off, the suite's default),
  31 requests. One server launch: this is a measurement of where the time goes, not a comparison between settings, so
  no noise floor applies; the ranges below are the spread over the 31 requests.

## Item 1: the in-engine profiler works here

`STRATA_VERIFY_PROFILE=1` produced a stage table for all 31 requests, and the log has no
`layer 0 rang but its payload never arrived whole` (0 of 31). The failure in `A770_SPEED_SPEC.md` was recorded on the
A770/SYCL build, whose eager path (`STRATA_VERIFY_EAGER`) does not exist in the CUDA source; on this build the profiler
runs on the normal (graph) path and works. The bug is A770-only, as far as this build can show.

## Item 2: the doorbell is cheap; the GPU waits for the copies

| | ms per window (median, range) | share of the window |
|---|---|---|
| window | 59.37 (22.76-98.28), 2.21 tokens per window | |
| `waitA` - the doorbell round trip: the GPU rings, the host plans the group, flag A comes back | 0.32 (0.30-0.34) | 0.5% |
| `waitB` - the PCIe share of the missed experts landing in GPU staging (DMA; a host function raises flag B when the copies are done, `verify.cpp` `fetch_dma`) | 35.52 (22.17-66.06) | 59.2% |
| `waitCPU` - the CPU pool's results for its share | 3.07 (1.49-20.40) | 6.0% |
| all three waits | 38.93 (25.38-73.05) | 65.4% |

Per layer and window the experts split as 10.79 VRAM hits, 5.08 CPU-pool experts and 3.25 PCIe experts (medians). The
host side of the same windows: GPU-reach wait 19.85 ms, per-layer host 31.36 ms of which the CPU pool 28.20 ms, draft
1.98 ms.

**The doorbell round trip is 0.32 ms per window, about 7 microseconds per layer**: on this card and link the handoff
itself is not where the time goes. **The GPU waits about two thirds of each window, and nearly all of that is `waitB`**:
the GPU kernels for the VRAM experts finish and the GPU then waits for the PCIe share of the missed experts to arrive.
The CPU pool's own wait is 6% of the window.

## What this changes in the plan

The A770 conclusion does not transfer. There, the waits on the CPU-pool flags plus copy-and-combine were ~87% of the
window and the handoff latency was suspected; here the handoff is 0.5% and the CPU-pool wait 6%. Item 3 as written -
overlap the CPU expert pool with GPU work - could recover at most that 6% (3.07 ms of 59.37), which cannot clear the
plan's 24.3% gate even if it were hidden completely. The time is in `waitB`, so the lever is the bus share and the copy
path, not the CPU pool's scheduling.

One estimate, stated as such and not measured: 3.25 PCIe experts per layer-window over the 48 layers is about 156 blobs
per window, at most 2.66 MB each (the largest in this pack) - at most ~415 MB, which the probed 27.2 GB/s moves in at most
~15 ms. `waitB` is 35.5 ms. So the copies at link speed do not account for the whole wait; candidates are the part of the
PCIe share that comes from the model files rather than the 36 GiB page-locked RAM tier (the budget holds most experts, the
rest are read from the files), copies that start late in the window, or the GPU reaching the wait early because its own
kernels are short. Which of these it is has not been measured.

Next measurements that follow from this, none of them run yet:

1. `--pcie-frac` arms against the automatic 0.55 (for example 0.40 and 0.25, moving work from the copies to the CPU pool,
   whose wait is small), interleaved, read with this tool: `waitB` and decode per arm. The plan's open-knob note allows
   setting it when the experiment is about the bus share; this one is.
2. Split `waitB` into copy time and source time: how many of the PCIe-share blobs come from the file tier versus the
   page-locked RAM tier in these windows.

## Item 3's first step, run: the PCIe share (2026-10-21)

`python tools/strata_ab.py --sweep pcie-frac --rounds 3 --runs 6 --tests chat,agent` (the automatic share against fixed 0.25,
0.35, 0.45 and 0.60; 15 launches taking turns; clean runs, profiler off), read with
`python tools/summarize_ab_sweep.py bench/results/ab-pcie-frac` and `python tools/read_sweep_warm.py bench/results/ab-pcie-frac`.
The engine's own share starts at 0.55 and refines to 0.45 in the first decode windows (`PcieAuto`, steps of 0.05, clamped to
0.10-0.60); a fixed `--pcie-frac` switches the refinement off.

| arm | engine decode, tok/s, the three launches (the first is inside the machine's cold 20 minutes) | experts over PCIe | per-turn paired against automatic |
|---|---|---|---|
| automatic (settles at 0.45) | 40.45 / 37.80 / 37.45 | 11.5-16.1% | - |
| 0.25 | 39.40 / 38.25 / 35.65 | 7.7% | +1.9% |
| 0.35 | 40.95 / 38.75 / 40.40 | 11.5-12.2% | +7.2% (+4.8, +9.7) |
| 0.45 | 39.05 / 37.85 / 38.55 | 15.5-16.5% | +0.0% |
| 0.60 | 32.20 / 37.55 / 36.00 | 22.6-25.1% | -7.6% |

- **0.60 is worse, and that one is a result.** Every launch of it is below the automatic arm's warm launches, its per-turn
  gaps are -6% to -23%, and it sends 23-25% of the routed experts over PCIe against 12-16%. More work on the link does not
  help; the engine's own ceiling of 0.60 is not where its rule would ever land.
- **0.25 and 0.45 are the same as automatic.** 0.45 fixed from the start matching the automatic arm is the control: the refinement
  settles on 0.45 and costs nothing.
- **0.35 is the best arm and is not separated.** Both of its warm launches (38.75, 40.40) are above both warm launches of the
  automatic arm (37.80, 37.45), by +3% and +8%. `summarize_ab_sweep.py` marks it "above noise" on a floor taken from two
  baseline launches (0.9%). `read_sweep_warm.py` does not: the arm's per-turn gaps (median 6.8%, range 0.4-38.6%) overlap the
  baseline's repeats (median 6.7%, range 0.8-9.9%), and the automatic arm's own three launches span 7.8% (its best one was its
  cold first launch, 40.45). A gain of this size is inside what one command run twice does here.
- **What this says about item 3.** The bus share is not a lever worth 24% - nothing in the sweep moves decode by more than
  single digits, and only upward at 0.35 and downward at 0.60. The engine's rule (balance the link against the CPU pool's
  measured speed) lands in the flat top of the curve. The plan's gate (decode rising by more than the batch's own noise) is not
  met, so no configuration change is made. Not measured here: whether `waitB` falls at 0.35, which needs the profiler on, and
  whether 0.35 holds over more launches. The re-run that would settle it is the automatic arm against 0.35 alone, five rounds.
