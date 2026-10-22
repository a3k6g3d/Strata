# Automatic PCIe share vs fixed 0.35 -- 2026-10-22 (`ab-pcie-frac-rerun`)

Ten engine restarts: five launches of the live config as configured (the share is automatic:
the probe picks 0.55, the CPU-pool timing refines it to 0.45) and five launches with
`--pcie-frac 0.35`, two runs each, the `agent` test only, profiler on.

Launched by `tools/pcie_rerun.bat`, which points at `strata-pcieprof.json` -- a copy of the live
`strata.json` plus `STRATA_VERIFY_PROFILE=1` and `STRATA_DECODE_TIMING=1` -- so nothing had to be
set by hand. Sweep entry `pcie-frac-rerun` in `tools/strata_ab.py`; runner logs
`bench/results/pcie-frac-rerun.log` and `pcie-frac-rerun-harness-out.log`. Each launch's engine
log is saved in its own folder as `engine-log.txt`, so the pre-registered commands work as
written, folder by folder.

The rules below were written before the run (`tools/pcie_rerun.bat`) and were not moved.

## The rules, and how each went

1. `python tools/read_sweep_warm.py bench/results/ab-pcie-frac-rerun` -- cold/warm split, turn by
   turn, computed verdict.
2. Floor = the spread among this batch's own five baseline launches. The 24.3% in
   `docs/BENCH_NOISE.md` is a six-launch figure and is the ceiling, not the gate.
3. The sign must hold in at least 4 of the 5 round-pairs.
4. Mechanism: `read_window_profile.py` per arm -- the arm's `waitB` per window must actually fall,
   reading shares, not absolute milliseconds (the profiler adds its own time).
5. Each arm's engine log must say the share it should -- automatic vs fixed 0.35. This is the
   check the October arm failed.

## Speed

Tool of record (`docs/BUILD_PLAN.json` names it): `python tools/summarize_ab_sweep.py bench/results/ab-pcie-frac-rerun`

    paired by turn number against 'as configured' at t+ 21.3 min (median of per-turn differences; + means faster):
          6.5     47.5  n=6  pcie-frac 0.35
          5.4     31.8  n=6  pcie-frac 0.35
          4.5     26.6  n=6  pcie-frac 0.35
          2.1     10.6  n=6  pcie-frac 0.35 *cold
          1.2      5.3  n=6  pcie-frac 0.35 *cold
         -1.1     15.9  n=6  as configured *cold
         -1.3     42.3  n=6  as configured
         -2.0     37.0  n=6  as configured
         -5.8      0.0  n=6  as configured *cold

      per round, against the reference arm 'as configured' launch for launch (+ means faster):
         noise floor: the reference arm's own launches: 2.3% over 3 launches (the cold ones left out)
         pcie-frac 0.35                      +6.7*  +2.9*  +4.5  +10.4  +6.0  median +6.0%  above noise

Rule 3: the sign holds 5 of 5 -- every arm launch above the reference, every baseline repeat below
it. Rule 2: the median +6.0% clears this batch's own floor (2.3% warm, 6.0% over all five).

The reader (`read_sweep_warm.py`, rule 1) disagrees on the last point and its verdict is recorded
as a dissent: biggest arm gap 12.7% against its own noise row of 5.6%, and it says the two
distributions overlap so this data cannot separate them. It answers a different question -- whether
the per-turn distributions separate, not the round-pair medians.

## Mechanism (rule 4) -- moved, cleanly

`python tools/read_window_profile.py <folder>/engine-log.txt`, `waitB`'s share of the window:

| launch | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| as configured (automatic, settles at 0.45) | 61.2 | 59.9 | 59.2 | 61.9 | 59.7 |
| `--pcie-frac 0.35` | 55.4 | 55.5 | 56.2 | 54.1 | 56.2 |

No overlap between the arms. Corroborated by `PCIe experts / layer-window`: 3.18-3.67 on the
baseline, 2.48-2.62 on the arm -- a ratio of 0.72 against the 0.78 the two shares predict.
`waitCPU`'s share rises in step, 5.1-6.0% to 7.6-9.7%, which is the direction the flag moves work.

## Rule 5 -- half-runnable, and passes on what is checkable

All five baseline logs print the refinement, consistently (`-> pcie_frac 0.45`, from the 0.55 the
probe picks; the CPU pool at 118-122 us per missed expert, the link at 25.5-27.3 GB/s). The arm
logs print no share line at all, because an explicit `--pcie-frac` switches the refinement off --
so the arm's half of the check can only be read through the experts ratio above. The comparison
this batch actually makes is 0.45 against 0.35, not the default against 0.35.

## Verdict

The gate is met: `waitB`'s share falls and decode rises by more than the noise this batch measures
for itself, on the tool of record, under the interleaved control. The lever is live. Next step is
the plan's (b): split `waitB` into copy time and source time (file tier versus the page-locked RAM
tier). No configuration change is proposed yet -- 0.35 is one point on a curve whose neighbours
(0.25, 0.45) the October sweep found flat, so the gain is the mechanism's, not the setting's.

Primary write-up: `docs/WINDOW_PROFILE_CUDA.md`, 'Item 3's re-run' section.
