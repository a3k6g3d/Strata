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

---

## Item 3's sub-step (b), the tier split, run (2026-10-22)

The same sweep, the same arms, five rounds, two runs each, agent test only - `bench/results/ab-tier-split-rerun`,
launched by `tools/tier_split_rerun.bat`. The question this time is not whether the arm is faster; that gate was met
on the section above. The question is **where the time it saved comes from**: waitB is the wait for a window's blobs,
and a blob arrives by one of two routes - it is already resident (the RAM tier) or it is read off the drive (the file
tier). The rule was written into the bat before the first launch, and it is the rule below.

The batch wrote one shared engine log, so it was segmented per launch (`tools/_tier_seg.py`) and each segment saved in
its own launch folder as `engine-log.txt`, as the previous batch's folders do; the reader was then run folder by
folder, as the rule names it, and `tools/_tier_share.py` does all ten and prints the table below. The RAM tier's clock
was on (`STRATA_RAM_TIER_CLOCK=1`, in the run config's env block), and the
instrumentation that gives it a `ram_ms` beside the `file_ms` is the change in `expert_source.{hpp,cpp}` and
`generate.cpp` that came with this batch. All ten launches printed their tier lines; none is void.

Per round-pair (baseline then arm), medians over the window counts, and the share the reader prints:

| round | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| waitB share, baseline | 61.2 | 61.6 | 59.1 | 61.0 | 59.3 |
| waitB share, arm | 54.7 | 57.0 | 56.3 | 56.7 | 55.7 |
| file tier blobs/window, baseline | 3.00 | 3.00 | 3.00 | 3.00 | 3.00 |
| file tier blobs/window, arm | 3.00 | 2.50 | 3.00 | 3.00 | 2.00 |
| RAM tier blobs/window, baseline | 433 | 454 | 428 | 446 | 409 |
| RAM tier blobs/window, arm | 429.5 | 451.5 | 431 | 439.5 | 423 |
| file tier ms/window, baseline | 2.17 | 2.23 | 2.10 | 2.28 | 2.39 |
| file tier ms/window, arm | 2.26 | 2.29 | 2.00 | 2.43 | 2.10 |
| RAM tier ms/window, both arms | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |

**The mechanism test fails.** The rule asked for the arm's file-tier count to be lower in at least four of five
round-pairs, beating the baseline's own spread on that counter. It is lower in two of five, by half a blob and by one
blob, on a counter whose per-window range is 0 to 30; the baseline's five medians are all 3.00, so its spread is zero
and any movement would have counted. The two that moved are not separable from noise, and three did not move at all.
The prediction that the RAM tier would move the other way - blobs crossing over from one tier to the other - also
fails: no consistent direction, and the baseline's own range (409 to 454) swamps every pair.

The share check passes, which is what makes the failure mean something. Each arm says the share it should: the
baseline records 16.2 / 16.4 / 16.3 / 16.4 / 15.8 percent, the arm 11.5 / 13.1 / 12.0 / 11.8 / 11.9 - a ratio of 0.74
against the 0.78 the flag implies. The arms did what they were told, both arms stamped, both printed. So the flag
moved the bus share, the share moved waitB (5/5 pairs, no overlap, reproducing the previous batch's 61.2-61.9 against
54.1-56.2 almost exactly) - and **the source side of the wait did not move.**

**So, as the bat put it: the gate was met, but this did not come from the bus share.** The source is the two tiers
together, and all of it that is clockable is the file tier's ~2.2 ms a window, unchanged between the arms (2.10-2.39
against 2.00-2.43, no direction). The RAM tier, clocked for the first time here, reads 0.00 ms a window on every
launch on both arms: the ~430 resident blobs a window cost nothing measurable. Against a waitB of 33-41 ms a window,
the whole source side is under 6 percent of it, and it is the part that did not move.

Speed, as the rule reports it: `read_sweep_warm.py` - the tool the bat names - cannot separate the arms. Its own
batch floor is 3.7% (four repeats of one command), the largest gap among the arms is 5.2%, and the per-turn
distributions overlap; its verdict stands as printed. The tool of record (`summarize_ab_sweep.py`) gives +2.7 / +3.2 /
+1.8 / +2.2 / +4.2 per round, median +2.2%, inside its own floor of 3.2%; the sign holds in five of five, which is the
part of the rule that can be satisfied here. This batch is not the gate and does not re-met it; it replicates the
mechanism's direction and not its magnitude.

**What (c) asks, and what the data points at.** The plan made the code change conditional on (a)-(b) pointing at one.
They do point at one, and not at the one the mechanism named: not the drive, not the read path, not the source tier -
the copy itself. Fewer blobs cross the bus (3.66 to 2.58 per layer-window), each of them already resident, each
costing zero at the source; the time saved is in the submitting and copying of them, not in finding them. And for
exactly that there is already a knob, built and documented and never run as a proper batch: `STRATA_DMA_BATCH` in
`docs/BATCHED_DMA.md`, batched submission through `cudaMemcpyBatchAsync`, recorded there as "an experiment to reduce
host submission overhead, not a demonstrated increase". The next step is to run it as an arm the same way - it is a
measurement, not a code change.

## Item 3's re-run, the one the section above asked for (2026-10-22)

The automatic share against 0.35 alone, five rounds, two runs each, agent test only, profiler on
(`bench/results/ab-pcie-frac-rerun`, launched by `tools/pcie_rerun.bat`, which points at `strata-pcieprof.json` - the live
`strata.json` plus `STRATA_VERIFY_PROFILE=1` and `STRATA_DECODE_TIMING=1`). Each launch's engine log is saved in its own
folder as `engine-log.txt`, so the pre-registered commands work as written, folder by folder.

**The mechanism moved, cleanly.** `python tools/read_window_profile.py <folder>/engine-log.txt`, `waitB`'s share of the window:

| launch | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| as configured (automatic, settles at 0.45) | 61.2 | 59.9 | 59.2 | 61.9 | 59.7 |
| `--pcie-frac 0.35` | 55.4 | 55.5 | 56.2 | 54.1 | 56.2 |

No overlap between the arms - the share falls as the flag says it should, which is what the gate asks on this half.
Corroborated by `PCIe experts / layer-window`: 3.18-3.67 on the baseline against 2.48-2.62 on the arm, a ratio of 0.72
against the 0.78 the two shares predict.

**The share check, half of it.** All five baseline logs print the refinement (`-> pcie_frac 0.45`, from the 0.55 the probe
picks), consistently; the arm logs print no share line at all, because an explicit `--pcie-frac` switches the refinement off.
So the arm's half of the check can only be read through the experts ratio above, and the comparison this batch actually makes
is 0.45 against 0.35, not the default against 0.35.

**The speed: the two readers disagree, and the plan names the arbiter.** `summarize_ab_sweep.py` (the tool of record) pairs
each arm launch with the baseline at the same turn: +6.7\*, +2.9\*, +4.5, +10.4, +6.0 - median **+6.0%**, marked above noise,
the floor being the reference arm's own launches (2.3% over the three warm ones, 6.0% over all five). The sign holds in five
of five in both readers: every arm launch positive, every baseline launch negative. `read_sweep_warm.py` puts the biggest arm
gap at 12.7% against its own noise row of 5.6%, and says the two distributions overlap, so that data cannot separate them -
it answers a different question (whether the per-turn distributions separate) and its verdict is recorded here as a dissent.

**Verdict: the gate is met** - `waitB`'s share falls, and decode rises by more than the noise this batch measures for
itself, on the tool the plan names. The lever is live, so the next step is the plan's (b): split `waitB` into copy time and
source time (file tier versus the page-locked RAM tier). No configuration change is proposed yet - 0.35 is one point on a
curve whose neighbours (0.25, 0.45) the October sweep found flat, so the gain is the mechanism's, not the setting's.
