@echo off
rem Start the noise-floor run. START THIS FILE YOURSELF (double-click, or your own
rem terminal) - do not let a Claude tool runner launch it.
rem
rem Why: the driver stops the live server on 8080 and kills that server's process
rem subtree. A driver launched by a Claude tool runner on this machine is inside the
rem reach of that subtree, and the last run died exactly that way - it listed pid
rem 25444, the tool runner that had launched it, and then died in the same second.
rem Started by you, the driver is outside the tree it kills.
rem
rem What it does, in this order: stop the server and its children (engine, vision
rem sidecar, workers), wait for the GPU to be genuinely empty, run three launches of
rem the baseline on the agent suite (two runs each), then restart the server as the
rem exact command that was running.
rem
rem About 50 minutes. The chat you are in will drop when the server stops, because the
rem agent talking to you is talking through it.

cd /d "%~dp0.."

echo.
echo This is what will be stopped. Nothing is killed yet.
echo.
python tools\noise_floor_unattended.py --check

echo.
pause

python tools\noise_floor_unattended.py

echo.
echo Run finished. The log is:
echo   bench\results\noise-floor-unattended.log
echo   bench\results\noise-floor-harness-out.log
echo Results: bench\results\noise-floor\
echo.
pause
