@echo off
rem Start the automatic-vs-0.35 re-run.  START THIS FILE YOURSELF (double-click, or your
rem own terminal) - do not let a Claude tool runner launch it, for the reason written in
rem tools\noise_floor.bat: a driver launched by a tool runner on this machine is inside
rem the reach of the process subtree it kills, and the run that died last time died that
rem way.  The driver itself is the same one; only the harness command differs.
rem
rem What this run is for.  Build-plan item 3's open step.  The 2026-10-21 batch
rem (bench/results/ab-pcie-frac) put 0.35 ahead on the warm column in all three of its
rem launches (+5.3 / +7.4 / +11.6) while its own noise rows topped out at 3.5%, so the
rem tool's distribution test still said "not separated" - one per-turn outlier in the arm
rem (0.4-38.6%) was enough to overlap.  Three launches cannot settle that; five rounds can,
rem which is what the plan asked for.  0.25 and 0.45 are not here: they straddle zero, so
rem they are not candidates, and 0.60 is known worse (-7.6%).
rem
rem Why the run points at strata-pcieprof.json and not the live strata.json.  The window split
rem (waitB, waitCPU, doorbell) is printed by the engine only when STRATA_VERIFY_PROFILE=1 and
rem STRATA_DECODE_TIMING=1 are in the config's `env` block - bench/results/
rem window-profile-2026-10-21/ was read that way.  strata.json has no env block and nothing on
rem this machine had one until strata-profile.json, which is the October run's own config and
rem differs from the live one (no Discord, no conversation store, no vision), so its tok/s would
rem not be comparable to the batch this one is settling.  So the run gets a copy of the live
rem config with ONLY the env block added - byte-identical otherwise, checked - pinned below, and
rem the live config is left alone: nothing to remember to take out afterwards.
rem Both arms get the stamps, so the arms are measured the same way; the stamps add their own
rem time, so read the SHARES and not the absolute milliseconds, and compare arms to arms
rem only - these tok/s are not comparable to the October batch's, which ran unstamped.
rem
rem What was written down before the first launch, so it cannot be decided afterwards:
rem the arm's gap has to beat this batch's own noise: the five baseline launches are the
rem same command run five times, and their spread is the limit - the 24.3% floor in
rem docs/BENCH_NOISE.md is for a batch of six, so treat it as the ceiling.  The sign has
rem to hold in at least four of the five round-pairs.  And the arm's waitB per window has
rem to actually fall: a speed gap with waitB unchanged did not come from the bus share.
rem
rem About 65 minutes now: ten launches (five rounds x two arms) at the ~5 min a launch the
rem cache-off run took, plus the 20 s gaps.  The chat you are in does not just drop when
rem the server stops, it closes: the --check pass below names strata-vision.exe - this
rem window - in the kill list, and only the server is restarted afterwards.

cd /d J:\Strata-work\Strata-0140

echo.
echo This is what will be stopped. Nothing is killed yet.
echo.
echo The run uses J:\Strata-work\strata-pcieprof.json - a copy of the live config with
echo   the profiler env block added (STRATA_VERIFY_PROFILE, STRATA_DECODE_TIMING), so
echo   the engine prints the window split and the waitB half of this run is measured.
echo   If that file is gone, the run reads the live config instead and loses half of it.
echo.
python tools\noise_floor_unattended.py --check --log bench\results\pcie-frac-rerun.log

echo.
pause

python tools\noise_floor_unattended.py --log bench\results\pcie-frac-rerun.log ^
  --harness-args "--config J:/Strata-work/strata-pcieprof.json --sweep pcie-frac-rerun --rounds 5 --runs 10 --noise-floor 0 --out bench/results/ab-pcie-frac-rerun"

echo.
echo Run finished.  The log is:
echo   J:\Strata-work\Strata-0140\bench\results\pcie-frac-rerun.log
echo   J:\Strata-work\Strata-0140\bench\results\pcie-frac-rerun-harness-out.log
echo Results: J:\Strata-work\Strata-0140\bench\results\ab-pcie-frac-rerun\
echo   read it with:  python tools\read_sweep_warm.py bench/results/ab-pcie-frac-rerun
echo   and the window split with:  python tools\read_window_profile.py ^<arm^>\engine-log.txt
echo.
pause
