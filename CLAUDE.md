# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Read [AGENTS.md](AGENTS.md) first for upstream's install and contribution rules (opt-in features, one change per PR, measured numbers only). This file covers what is specific to this fork and this machine.

## What this repo is

A fork (`a3k6g3d/Strata`, working branch `speed-work-0.1.43`) of Niko1221/Strata: a CUDA mixture-of-experts inference engine (`src/`, `include/`, built as `strata.exe`) plus a Python server and web app (`serve/`) that exposes OpenAI- and Anthropic-compatible APIs. Upstream merges are done with `git merge --no-ff <tag>` onto a new `speed-work-<version>` branch. The fork's own changes are documented in `CHANGELOG.md` (dated entries under "Unreleased") and `docs/SPEED_BRANCH.md`.

The live install is **outside the repo**, in `J:\Strata-work` (NVMe): `strata.json` (the server config), `build-0143\` (engine build), `models-sc117\`, `packs\sc117-iq3s`, `mtp-rt-basalt\` (draft layer), `sessions\` (saved web chats), `bench\` (benchmark scripts and results) and `_archive\config-backups\` (copy `strata.json` there before every edit). Model: SC117 Qwen3.8-Flash-Next abliterated, IQ3_S. Hardware: RTX 5070 12 GB, i5-10600K (AVX2, no AVX-512), 64 GB DDR4-2666, PCIe running at Gen2 (Gen3 is unstable on this board).

## Commands

Python tests (no GPU needed), from the repo root. Run one module at a time; `test_mcp` takes about a minute:

```
set PYTHONUTF8=1
python -m unittest serve.test_mcp
python -m unittest serve.test_device_paths serve.test_discord_sessions
python tools/test_setup_choices.py          # setup's own tests
```

Engine build (Windows, Ninja, CUDA 13.4, RTX 5070 = sm_120). Run inside a shell that has called `vcvars64.bat`:

```
cmake -S J:/Strata-work/Strata-0140 -B J:/Strata-work/build-0143 -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES=120 -DSTRATA_ENABLE_CUDA=ON -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_BUILD_TESTS=OFF
cmake --build J:/Strata-work/build-0143 --target strata -j 8
```

`strata-vision.exe` (image encoder) is a separate CMake project in `tools/vision`, not a target of the main build; copy the existing one into a new build folder. For the C++ test binaries configure with `-DSTRATA_BUILD_TESTS=ON` and run `ctest`.

Run the server (about 4 minutes to load; it pins RAM and can make the PC unresponsive meanwhile):

```
cd J:\Strata-work\Strata-0140
python -m serve.server --engine strata --config J:/Strata-work/strata.json --port 8080
```

Normal start is `J:\Strata-work\Start-Strata.bat` (calls `run-strata.ps1`, logs to `strata-console.txt`). To stop it use `J:\Strata-work\bench\stop_strata.ps1` through `powershell -File`; `Stop-Strata.bat` from Git Bash does not stop it.

## Architecture (the parts that span files)

- **`serve/server.py`** (very large) owns the HTTP API, the engine process (`strata --serve`, driven by `GEN <max_new> <ids>` lines), request preparation (`prepare()` clamps `max_tokens` to the room left in the context) and the tool loop (`_run` and the MCP loop near the approval code). The server reads `strata.json` once at start, so **any change to the config, to `serve/*.py` or to a tool server under `tools/` needs a restart** (about 4 minutes, and it cancels any request in flight; check `/v1/status` -> `concurrency.serving` first).
- **`serve/mcp.py`** is the tool permission model. Tool kinds are read / write / danger / net / exec / ask; modes are off / read / ask / edit / auto / full. `decide(mode, kind)` gives the base verdict; the tool loop in `server.py` then layers on: the no-read list (`BlockList`, `mcp.blocked_paths`, refused in every mode), protected paths (`mcp.protected_paths`, asks in every mode), the network "leak guard" (a web call after a local tool asks, except in Full access) and, in Auto mode, `serve/auto_mode.py` (`assess_exec` / `assess_tool`), which lets routine calls run and asks for risky ones. Device paths such as `\\.\PhysicalDrive4` are not files: the command-text scan ignores them and `auto_mode` asks for them.
- **`tools/strata_*_mcp.py`** are the stdio tool servers (files, web, dev, exec) started from the `mcp_servers` block of `strata.json`. `strata_web_mcp.py`'s `web_search` goes to Bing with the query only; extra parameters (`setlang`, `mkt`) make Bing answer a different query.
- **Sessions and chat clients:** `serve/sessions.py` stores web chats as files in `J:\Strata-work\sessions` (with a custom order file); `serve/discord_bot.py` talks to the local server over HTTP and saves each Discord chat as a session through the same `/sessions` routes, so chats are shared with the web and desktop apps (`desktop/tauri`). The web UI is `serve/web/{index.html,app.css,app.js}`.
- **Expert tiers (engine):** GPU expert cache -> pinned RAM tier (`--resident-budget-gib`) -> model file on NVMe. Decode misses are split between a CPU pool and PCIe (`pcie_frac`, automatic). MTP speculative decoding (`--spec 4 --spec-min-p 0.5`) uses the draft layer in `--mtp`. Prompt reading streams every expert layer by layer, so it is bound by PCIe and NVMe speed.

## Working rules for the agent

- **A compaction is not the end of the turn.** When the history gets summarized mid-turn, the summary lands as the model's
  own last message and reads like "report where you got to", so the model answers with the summary and stops - which is
  how a session left running overnight spends the night stopped with a Continue button under it. The tool loop nudges
  once in that case (`needs_nudge`, `NUDGE_AFTER_SUMMARY`); the matching habit is to re-issue the pending lookups after
  a compaction boundary rather than answer from the summary, and to keep a research turn to a few tool calls so the
  ceiling is reached less often.
- **Write state to disk, not to context.** Findings go into `docs/` and `CHANGELOG.md` as they are found, and a
  completed piece of work is committed before starting the next - a session that loses its context can then be picked
  up cold, which is the only defence that has ever worked here.

## Gotchas learned on this machine

- **RAM is the limit.** With a 65,536-token context a 40 GiB resident budget left under 1 GiB free and Windows thrashed (page-ins over 8,000/s, generation stalled). The config uses 36 GiB. Check `\Memory\Pages Input/sec` and free RAM before raising the budget or the context; a larger context also shrinks the GPU expert cache (2,205 -> 1,951 slots at 64K) with no measurable decode cost, because decode here is CPU-bound.
- **Benchmarking:** use the `bench\overnight*.py` pattern (restart the server per variant, three passes with the first dropped, repeat the control at the end). Run-to-run noise is about 1%; do not call a smaller difference a win. Never run two benchmarks or heavy tests at once, and keep the RAM and page-in guard in any new script.
- **Tuning already settled** (see `CHANGELOG.md` and `bench\results\`): `--adapt-swaps 8`, `--ple-io direct`, `--vram-reserve-mib 640`, spec 4 / min-p 0.5. No gain from `--kv k8v4`, `--prefill auto:16384`, a smaller VRAM reserve, or `--adapt-async`.
- **Editing on Windows:** Git Bash heredocs and Python patch strings mangle backslashes; use the Edit or Write tools. In PowerShell, `-replace` with `$_` in the replacement expands to the whole input. Antivirus can lock a test file that contains shell-command strings; split such strings in tests.
- **Secrets:** never print the contents of `J:\Strata-work\discord-token.txt` or any token.
- **Git and downloads:** push only when asked (never `--force`); downloads need the user's explicit OK with filename, source and size.
