@echo off
rem Start the dma-vs-dma batch test.  START THIS FILE YOURSELF (double-click, or your own
rem terminal) - do not let a Claude tool runner launch it, for the reason written in
rem tools\noise_floor.bat: a driver launched by a tool runner on this machine is inside the
rem reach of the process subtree it kills, and the run that died last time died that way.
rem The driver itself is the same one; only the harness command differs.
rem
rem What this run is for.  The run this file made on 2026-10-22 (19:28-20:10, six launches,
rem exit 0 - its results are in bench/results/ab-dma-batch and are NOT overwritten by the one
rem below) did NOT answer the question, and said why in its own counters: the batched arm's
rem stamps ran - blobs-per-submission 4.22 / 4.47 / 4.57, batched share 78-79% - so the batch
rem did engage there; but the baseline's counters read 0 submissions on every window of all
rem three launches, because it left STRATA_DMA_BATCH unset and never reached the submission
rem path at all.  So the two arms were not the same code path: what was measured was the copy
rem kernel against the batch, not the loop against the batch.  Its speed column (the batched
rem arm BEHIND, +7.8% for the loop on the one clean pair, sign failing in 2 of 3) is recorded
rem but is not a verdict on the batch, for that reason.
rem
rem The fix, and it is small: both arms now set the variable explicitly - the baseline says
rem STRATA_DMA_BATCH=0 (the loop of one copy each), the arm says =1 (one cudaMemcpyBatchAsync).
rem Both then reach the submission path, which is what the sweep's own note has always said the
rem knob needs (--pcie-mode dma stays on both arms for the same reason).
rem
rem What was written down before the first launch, so it cannot be decided afterwards.
rem --- updated 2026-10-23 for the run below, still before any of it was launched ---
rem The third run (this one) is FIVE rounds x two arms = TEN launches, into
rem bench/results/ab-dma-batch-3 (the two earlier outputs, ab-dma-batch and -2, are kept and
rem are NOT overwritten).  It is five rounds for a reason: the two runs that failed the
rem mechanism rule each had three pairings, which is no power at all against a 4.4% floor; ten
rem pairings is the first version of this question that could answer the speed half.
rem The other thing that changed is the reason the rule could not pass.  Mode 0 runs as a plain
rem cudaMemcpyAsync loop (verify.cpp:3050) and had no counter there, so the loop arm read 0 no
rem matter what it did - both prior runs failed on exactly that.  Two atomic increments are now
rem in that loop, in place, behaviour untouched (the loop ignores errors, so it cannot route
rem through the batched path), and strata-instr.exe was rebuilt on 2026-10-23 07:55:38.  So for
rem the first time both arms print, and the rule below can actually pass.
rem Write the asymmetry down now, before the numbers: the loop arm pays ~156 atomic increments a
rem window (one per blob) and the batched arm a handful (one per group) - about 3 us inside a
rem 60 ms window, so well under the floor, but it is not zero and it is not the thing being
rem tested.  Say so in the writeup rather than letting it look like a discovered effect.
rem The floor is not a guess any more: the live config, three launches, 2026-10-22 17:57-18:12,
rem warm medians 35.0 / 36.55 / 35.4 tps - about 4.4%, and the launches logged a power cap and
rem mean clocks of 1882/1686/1570 MHz, so that is the GPU coming off idle, not a broken meter.
rem Write down what that means before running: against a 4.4% floor, three pairings settle
rem nothing (both prior runs proved that: gaps of 7.8%/6.3%/5.5% against repeat-noise of
rem 1.2%/5.7%, sign flipping in 2 of 3).  TEN pairings is where a speed claim can start to be
rem made, so the speed rule below is written for ten - the COUNTERS are still the primary claim
rem (exact, no noise floor applies to them), and the speed column is a direction, reported as
rem one, unless it clears the floor.
rem Five rounds, three runs a launch, fifteen runs an arm.
rem The speed rule: the arm's gap has to beat the 4.4% floor (the three-launch noise measurement
rem above - the same command run three times), the sign has to hold in at least 8 of the 10
rem round-pairs - and the tool's own distribution test gets the last word: the warm column, run
rem 2 of each launch, since the tool cannot separate arms inside its own 3.7% batch floor.  A
rem gap under the floor is no finding, not a small finding.
rem The mechanism rule is the new one, and it is the one that failed last time, so it is the
rem one to look at first: the instrumented build stamps the submissions per window in BOTH
rem arms (two atomics at the submission point, so the counts are exact - no noise floor
rem applies to them).  The baseline's blobs-per-submission has to sit at ~1 (the loop: one
rem submission per blob) and the arm's has to be far above it (the batch: one per group).  If
rem the arm's ratio comes out at ~1 too, the batch never ran - the knob needs --pcie-mode dma
rem and the stamp says whether it did - and then whatever the speed says, it did NOT come from
rem the batch, and say so.
rem New for this run, from what the last one showed: BOTH arms' counters have to be non-zero.
rem A baseline reading 0 means it never reached the submission path - the exact way the last run
rem failed - and the run is void for the mechanism question, whatever the speed says.  That rule
rem is now actually satisfiable: the loop path is counted (see above), so a 0 on the loop side is
rem a real failure to run, not the instrumentation's blind spot as it was in the two prior runs.
rem And one asymmetry is the code's own, not an arm, so write it down: mode != 0 turns adaptive
rem overlap off (generate.cpp:9501 prints "adaptive overlap: off: not with STRATA_DMA_BATCH").
rem So the arm is the flag as shipped - batch AND overlap off - against the loop WITH overlap.
rem That is the product question and it is fine to answer it, but the counters say whether the
rem batch ran and the overlap line says what else moved; do not report the gap as the batch's.
rem One more, mechanical: a launch whose engine-log has no "strata decode submissions" line is
rem void for this question - it did not print, so it measured nothing.  The binary that stamps
rem it is build-0145/strata-instr.exe, rebuilt on 2026-10-23 07:55:38 (the 2026-10-22 16:20 build
rem stamped only the batched path); the previous batches' logs predate both builds and carry the
rem tier line but not this one.
rem
rem About an hour now: ten launches (five rounds x two arms) at the ~5 min a launch this
rem run takes, plus the 20 s gaps.  The chat you are in does not just drop when the server
rem stops, it closes: the --check pass below names strata-vision.exe - this window - in the
rem kill list, and only the server is restarted afterwards.
rem
rem WHAT IT SAID (2026-10-23, ran 08:05:56-08:57:5x, exit 0, 51.9 min, ten launches, output
rem bench/results/ab-dma-batch-3).  Full writeup: docs/BATCHED_DMA.md.
rem   The mechanism rule PASSED, on both arms, for the first time across all three runs.  Loop
rem   1.00 blobs per submission on all five of its launches - read that for what it is: the
rem   counter there increments once per blob, so 1.00 is true by construction and what it
rem   establishes is that the loop path ran and was counted, which the two prior runs could not
rem   show.  Batch 4.37-4.74 at 78-80% batched on all five, the same as the prior runs' eleven.
rem   The overlap caveat above DID NOT FIRE: the loop arm prints "adaptive overlap: off: not in
rem   the resident RAM mode unless STRATA_ADAPT_OVERLAP_RESIDENT=1", so overlap was off on BOTH
rem   arms in this config and these ten launches differ by the batch alone.  The counter cost
rem   came in at ~196 increments a window on the loop arm (not the ~156 written above), batched
rem   ~45 submissions plus ~35 batch calls - still under the floor, still not the thing tested.
rem   Speed: the two statistics disagree, and that is the finding.  The tool's own warm column (a
rem   mean) puts the loop +7.5%, over the 4.4% floor; per-run medians put it +4.9%, on the floor and
rem   not over it - and the cold phase points the other way on both bases (per-run medians -2.3%,
rem   the tool's own column +5.7%), so neither phase settles it.  Sign + for the loop in 4
rem   of 5 pairs either way, and the tool's own distribution test says the arms' per-turn distributions
rem   overlap its repeat rows (largest gap 13.1% against 11.5% batch noise), so on its own terms
rem   it cannot separate them.  A verdict that turns on mean versus median of the same ten numbers
rem   has no power for the question.  The sign clause asked for 8 of 10 round-pairs when ten launches
rem   make five: unsatisfiable as written, same species of slip as the rule that failed the two
rem   prior runs, and reported as it stands.  Verdict: the batch runs and buys nothing
rem   measurable.  Default stays 0.

cd /d J:\Strata-work\Strata-0140

echo.
echo This is what will be stopped. Nothing is killed yet.
echo.
echo The run uses J:\Strata-work\strata-instr.json - the live config, the INSTRUMENTED exe
echo   (J:/Strata-work/build-0145/strata-instr.exe, rebuilt 2026-10-23 07:55 with the
echo   submission counter) and the env block (STRATA_VERIFY_PROFILE, STRATA_DECODE_TIMING,
echo   STRATA_RAM_TIER_CLOCK), so both arms print the per-window stamps.  If that file is
echo   gone, the run reads the live config instead - the uninstrumented exe, no env block -
echo   and loses all of it.
echo.
python tools\noise_floor_unattended.py --check --log bench\results\dma-batch.log

echo.
pause

python tools\noise_floor_unattended.py --log bench\results\dma-batch.log ^
  --harness-args "--config J:/Strata-work/strata-instr.json --sweep dma-batch --rounds 5 --runs 9 --noise-floor 0 --out bench/results/ab-dma-batch-3"

echo.
echo Run finished.  The log is:
echo   J:\Strata-work\Strata-0140\bench\results\dma-batch.log
echo   J:\Strata-work\Strata-0140\bench\results\dma-batch-harness-out.log
echo Results: J:\Strata-work\Strata-0140\bench\results\ab-dma-batch-3\
echo   read the counters with:  python tools\read_window_profile.py ^<launch^>\engine-log.txt
echo   and the speed with:      python tools\read_sweep_warm.py bench/results/ab-dma-batch-3
echo.
pause
