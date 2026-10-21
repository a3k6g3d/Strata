@echo off
rem Start the cache-off run.  START THIS FILE YOURSELF (double-click, or your own
rem terminal) - do not let a Claude tool runner launch it, for the reason written in
rem tools\noise_floor.bat: a driver launched by a tool runner on this machine is inside
rem the reach of the process subtree it kills, and the run that died last time died that
rem way.  The driver itself is the same one; only the harness command differs.
rem
rem What this run is for.  The question the cache-vs-drafts set was built to ask - does
rem the expert cache carry the speed, or the drafts - was answered for the drafts and is
rem still open for the cache, because the arm meant to measure the cache measured nothing:
rem both arms labelled cache-off report a 1,951 / 1,954-expert cache in their own engine
rem logs.  generate.cpp:3499 turns a 0 back into auto while a profile is in use, so on
rem this rig off has no spelling, and the flag asked for the baseline under a second label.
rem This run asks for ONE expert instead, which the parser does honour - an explicit number
rem truncates the ranked list - so the cache holds a single pair and everything else is
rem served off the fast path.
rem
rem What the run cannot separate, said here as well as in the note it writes: a 1-expert
rem cache hands ~3.7 GiB of VRAM back, and the KV and prompt path will take it.  So the arm
rem is "cache off AND that memory given back", not cache off alone.  And the config has
rem moved since the October arms ran on it - strata.json now says --adapt-swaps 48 where
rem the doc records 8, and names the swift15 pack - so this run's baseline is its own
rem reference, comparable to its own arm and NOT to the table in CACHE_VS_DRAFTS.md.
rem
rem Read the engine's own log line in each arm before believing any of it: it must say
rem 1951 experts in the baseline and 1 in the arm.  That line is the check the last arm
rem failed, and it is the only thing that can tell a real off-arm from a labelled one.
rem
rem Why --noise-floor 0 is spelled out below.  The driver's default harness command asks for
rem the noise floor, and --harness-args only appends, so a command that means the sweep has
rem to cancel it - otherwise the harness gives the noise floor precedence, launches the
rem baseline arm alone three times, never reaches the arm, and prints the sweep's note above
rem data the sweep did not produce.  That is exactly how the first interleaved run died; the
rem harness now refuses a command that asks for both, and this file says plainly which one
rem it means.
rem
rem What it does, in the same order as the other batch file: stop the live server and its
rem children, wait for the GPU to be genuinely empty, run the six launches, then restart
rem the server as the exact command that was running.
rem
rem About 35 minutes: six launches (three rounds x two arms) at the ~5 min a launch this
rem file's own run took, plus the 20 s gaps.  The chat you are in does not just drop when
rem the server stops, it closes: the --check pass below names the vision sidecar - this
rem window - in the kill list, and only the server is restarted afterwards.

cd /d J:\Strata-work\Strata-0140

echo.
echo This is what will be stopped. Nothing is killed yet.
echo.
python tools\noise_floor_unattended.py --check --log bench\results\cache-off.log

echo.
pause

python tools\noise_floor_unattended.py --log bench\results\cache-off.log ^
  --harness-args "--sweep cache-off --rounds 3 --noise-floor 0 --out bench/results/ab-cache-off"

echo.
echo Run finished.  The log is:
echo   J:\Strata-work\Strata-0140\bench\results\cache-off.log
echo   J:\Strata-work\Strata-0140\bench\results\cache-off-harness-out.log
echo Results: J:\Strata-work\Strata-0140\bench\results\ab-cache-off\
echo.
pause
