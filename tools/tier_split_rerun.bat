@echo off
rem ---------------------------------------------------------------------------
rem Sub-step (b) of the plan: split waitB into the copy and the source.  The same
rem five-round batch as tools\pcie_rerun.bat - same sweep (pcie-frac-rerun), same
rem arms, same rules - run again with the two tiers counted, so the question this
rem time is not whether the arm is faster (that gate was met on 2026-10-22, on the
rem named arbiter) but WHERE the time it saved comes from.
rem
rem What is counted, and what is not.  The file tier already had both a count and a
rem clock (file_reads / file_ms, printed per request on the serve path).  The RAM
rem tier had only a count, so it got the missing clock (ram_ms, printed beside the
rem stage table).  The blob COUNTS are the strong half of this: they cost nothing
rem and are not affected by the clock.  The millisecond columns are the weak half -
rem the RAM clock is a steady_clock::now() around every resident hit, thousands of
rem blobs a request, so it is partly measuring itself.  Read the counts as counts
rem and the times as shares only.
rem
rem Both arms are stamped, and both are stamped the same way: the clock is turned on
rem by the config's env block, which the arms share - an arm differs from the other
rem only by its command line.  So arms compare to arms, and neither compares to the
rem previous batch, which ran the uninstrumented binary.  That is why the run points
rem at strata-instr.json (the live config, the instrumented exe, the env block) and
rem not at strata.json: the live config is left alone, nothing to take out after.
rem
rem What was written down before the first launch, so it cannot be decided afterwards:
rem the mechanism claim is that the 0.35 arm reads fewer experts off the drive.  So
rem the arm's file-tier blobs per window have to be LOWER than the baseline's, in at
rem least four of the five round-pairs, and the gap has to beat the baseline's own
rem round-to-round spread on that same counter - the batch measures its own noise,
rem the same way the speed gate does.  If the file tier's count comes out the same on
rem both arms, the gate was met but this did not come from the bus share, and say so.
rem The RAM tier is expected to move the other way (blobs shifting from one tier to
rem the other); that half is a prediction, not the test.
rem One more, mechanical: a launch whose engine-log has no "strata decode tier split"
rem line is void for this question - it did not print, so it measured nothing.
rem
rem About 65 minutes: ten launches (five rounds x two arms) at the ~5 min a launch
rem this run takes, plus the 20 s gaps.  The chat you are in does not just drop when
rem the server stops, it closes: the --check pass below names strata-vision.exe -
rem this window - in the kill list, and only the server is restarted afterwards.

cd /d J:\Strata-work\Strata-0140

echo.
echo This is what will be stopped. Nothing is killed yet.
echo.
echo The run uses J:\Strata-work\strata-instr.json - the live config, the
echo   INSTRUMENTED exe (J:/Strata-work/build-0145/strata-instr.exe) and the env block
echo   (STRATA_VERIFY_PROFILE, STRATA_DECODE_TIMING, STRATA_RAM_TIER_CLOCK), so both
echo   arms print the per-window tier split.  If that file is gone, the run reads the
echo   live config instead - the uninstrumented exe, no env block - and loses all of it.
echo.
python tools\noise_floor_unattended.py --check --log bench\results\tier-split-rerun.log

echo.
pause

python tools\noise_floor_unattended.py --log bench\results\tier-split-rerun.log ^
  --harness-args "--config J:/Strata-work/strata-instr.json --sweep pcie-frac-rerun --rounds 5 --runs 10 --noise-floor 0 --out bench/results/ab-tier-split-rerun"

echo.
echo Run finished.  The log is:
echo   J:\Strata-work\Strata-0140\bench\results\tier-split-rerun.log
echo   J:\Strata-work\Strata-0140\bench\results\tier-split-rerun-harness-out.log
echo Results: J:\Strata-work\Strata-0140\bench\results\ab-tier-split-rerun\
echo   read the split with:  python tools\read_window_profile.py ^<launch^>\engine-log.txt
echo   and the speed with:   python tools\read_sweep_warm.py bench/results/ab-tier-split-rerun
echo.
pause
