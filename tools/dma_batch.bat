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
rem The floor is not a guess any more: the live config, three launches, 2026-10-22 17:57-18:12,
rem warm medians 35.0 / 36.55 / 35.4 tps - about 4.4%, and the launches logged a power cap and
rem mean clocks of 1882/1686/1570 MHz, so that is the GPU coming off idle, not a broken meter.
rem Write down what that means before running: nine runs an arm cannot settle a ~2% question
rem against a 4.4% floor.  So the COUNTERS are the claim - they are exact, no noise floor
rem applies to them, and this is the rule that failed last time - and the speed column is a
rem direction, reported as one, not a result.  If the speed does clear 4.4% that is news, but
rem it was not what this batch was built to decide.
rem Three rounds, three runs a launch, nine runs an arm.
rem The speed rule: the arm's gap has to beat the spread of the three baseline launches (the
rem same command run three times - the batch's own noise), the sign has to hold in ALL THREE
rem round-pairs - with three rounds there is no room for a dissent, and one would stop the
rem claim - and the tool's own distribution test gets the last word: the warm column, run 2
rem of each launch, since the tool cannot separate arms inside its own 3.7% batch floor.
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
rem failed - and the run is void for the mechanism question, whatever the speed says.
rem And one asymmetry is the code's own, not an arm, so write it down: mode != 0 turns adaptive
rem overlap off (generate.cpp:9501 prints "adaptive overlap: off: not with STRATA_DMA_BATCH").
rem So the arm is the flag as shipped - batch AND overlap off - against the loop WITH overlap.
rem That is the product question and it is fine to answer it, but the counters say whether the
rem batch ran and the overlap line says what else moved; do not report the gap as the batch's.
rem One more, mechanical: a launch whose engine-log has no "strata decode submissions" line is
rem void for this question - it did not print, so it measured nothing.  The binary that stamps
rem it is build-0145/strata-instr.exe, rebuilt on 2026-10-22 16:20; the previous batch's logs
rem predate that build and carry the tier line but not this one.
rem
rem About 35 minutes now: six launches (three rounds x two arms) at the ~5 min a launch this
rem run takes, plus the 20 s gaps.  The chat you are in does not just drop when the server
rem stops, it closes: the --check pass below names strata-vision.exe - this window - in the
rem kill list, and only the server is restarted afterwards.

cd /d J:\Strata-work\Strata-0140

echo.
echo This is what will be stopped. Nothing is killed yet.
echo.
echo The run uses J:\Strata-work\strata-instr.json - the live config, the INSTRUMENTED exe
echo   (J:/Strata-work/build-0145/strata-instr.exe, rebuilt 2026-10-22 16:20 with the
echo   submission counter) and the env block (STRATA_VERIFY_PROFILE, STRATA_DECODE_TIMING,
echo   STRATA_RAM_TIER_CLOCK), so both arms print the per-window stamps.  If that file is
echo   gone, the run reads the live config instead - the uninstrumented exe, no env block -
echo   and loses all of it.
echo.
python tools\noise_floor_unattended.py --check --log bench\results\dma-batch.log

echo.
pause

python tools\noise_floor_unattended.py --log bench\results\dma-batch.log ^
  --harness-args "--config J:/Strata-work/strata-instr.json --sweep dma-batch --rounds 3 --runs 9 --noise-floor 0 --out bench/results/ab-dma-batch-2"

echo.
echo Run finished.  The log is:
echo   J:\Strata-work\Strata-0140\bench\results\dma-batch.log
echo   J:\Strata-work\Strata-0140\bench\results\dma-batch-harness-out.log
echo Results: J:\Strata-work\Strata-0140\bench\results\ab-dma-batch-2\
echo   read the counters with:  python tools\read_window_profile.py ^<launch^>\engine-log.txt
echo   and the speed with:      python tools\read_sweep_warm.py bench/results/ab-dma-batch-2
echo.
pause
