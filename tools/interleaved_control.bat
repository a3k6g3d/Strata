@echo off
rem Start the interleaved control run.  START THIS FILE YOURSELF (double-click, or your
rem own terminal) - do not let a Claude tool runner launch it, for the reason written in
rem tools\noise_floor.bat: a driver launched by a tool runner on this machine is inside
rem the reach of the process subtree it kills, and the run that died last time died that
rem way.  The driver itself is the same one; only the harness command differs.
rem
rem What this run is for.  The three batches on disk launched the SAME arm over and over
rem in the SAME position, so their spread can never separate the machine's drift from an
rem arm's position - they are the same variable.  This run launches the same arm twice,
rem in a different order each round (A B, B A, A B), two runs each, so the position
rem effect shows up as disagreement between rounds instead of as a result.  Both arms are
rem byte-identical, so every difference between them IS the noise.
rem
rem What it does, in the same order as the other batch file: stop the live server and its
rem children, wait for the GPU to be genuinely empty, run the six launches, then restart
rem the server as the exact command that was running.
rem
rem About 25 minutes at the pace the last batch ran at (3.5 min a launch, plus the 20 s
rem gaps).  The chat you are in will drop when the server stops, because the agent talking
rem to you is talking through it.

cd /d "%~dp0.."

echo.
echo This is what will be stopped. Nothing is killed yet.
echo.
python tools\noise_floor_unattended.py --check --log bench\results\noise-interleaved.log

echo.
pause

python tools\noise_floor_unattended.py --log bench\results\noise-interleaved.log ^
  --harness-args "--sweep noise-interleaved --rounds 3 --runs 6 --out bench/results/noise-interleaved"

echo.
echo Run finished.  The log is:
echo   bench\results\noise-interleaved.log
echo   bench\results\noise-interleaved-harness-out.log
echo Results: bench\results\noise-interleaved\
echo.
pause
