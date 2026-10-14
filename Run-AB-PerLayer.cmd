@echo off
rem the fifth arm: per-layer cache slots, 8 runs, agent suite.  Run-AB.cmd runs all four arms at 3 runs; this one
rem names a single arm, so the 13:33 run's "as configured" data (kept in results-baseline-8runs.json) is not redone.
rem Launched by scheduled task, never from inside the chat: this script stops the server that would be its parent.
powershell -NoProfile -ExecutionPolicy Bypass -File "J:\Strata-work\Strata-0140\tools\run_ab_offline.ps1" -armName "expert cache, per-layer slots" -runs 8 -outOverride "bench\results\ab-agent-perlayer"
