# Changelog (the `speed-work` fork)

Changes on top of upstream's engine (0.1.39). The engine's own version number stays upstream's; these entries are
dated, and the fork's releases follow [SemVer](https://semver.org) once it tags them (features = minor, fixes = patch).

## Unreleased - 2026-10-09

### Ported to upstream 0.1.40.2 (branch `speed-work-0.1.40.2`)
- Everything below except the speed commit's engine changes, which were **not** carried over: upstream already has
  `kv_append_q4_steps` and the prompt-attention switch, and it rewrote `qsa_prompt_attn.cu`, `generate.cpp`,
  `verify.cpp` and the tool-call parser. Left behind, to be re-ported only if measured worth it: decode attention on
  tensor cores (`qsa_decode_attn_tc`, about 2% of a decode window here), `--spec-adaptive` (off in the config
  anyway), the Q4_0 prompt-attention kernel (the config uses `--kv int8`) and the linear tool-call scan.
- Kept from it: `tools/mtp_from_gguf.py` and `docs/SPEED_BRANCH.md`.
- The engine must be rebuilt from this branch before its server is used: its Python expects the 0.1.40.2 engine.
- Measured (RTX 5070 on PCIe 3.0 x16, 13 GB/s; IQ4_XS, 44 GiB pinned; held-out decode, two alternating runs each):
  0.1.40.2 engine 16.61 tok/s vs the 0.1.39 build 16.06 (+3.4%). Upstream options, no gain, left off:
  `--expert-cache-per-layer` 16.68, `STRATA_PREFILL_CPU_SHARE=auto` 16.62 (prompt speed unchanged).
  Second sweep (all runs slowed ~1 tok/s by an `nvidia-smi` crash guard polling every 3 s; compare within it):
  auto `--pcie-frac` (0.35 at 13 GB/s) 15.6; 0.20 14.6; 0.50 14.2; 0.70 12.5 - keep auto. `--pool-tasks` 12 / 30:
  14.8 / 15.0, `--host-core last` 15.0 - ties, left at their defaults. The PC hard-hung during the second 0.50 run
  (the PCIe 3.0 link logs ~2,000 replays/s under load; no warning sign before the hang).

### Added
- Chat: a message typed while the model is answering is queued and sent when the answer ends; **Send now** stops the
  answer and sends it at once.
- Long tool chats no longer overflow the context: older tool results are shortened, and if that is not enough the model
  summarizes the older conversation (cached; shown in the answer's footer). See DETAILS.md, "Long chats".
- Image support for the uncensored model: a `vision` entry in the run config with the model's own mmproj file
  (mradermacher `...Uncensored.mmproj-f16.gguf`) and a CUDA build of `strata-vision`.

### Changed
- Commands from the exec server (`run_command`, `run_python`) run by themselves in Full access and after "Always allow".
  A command that names a protected path (including through `%USERPROFILE%`, `~`, `$env:`) still asks every time.
- Exec timeouts: default 600 s, at most 3,600 s (was 60 / 300). Documented a longer `max_rounds`, `timeout_s` and
  `approval_timeout_s` for long jobs.

### Fixed
- Docs: `STRATA_RESIDENT_HEADROOM_GIB=2` with a 48 GiB budget is unsafe (froze the PC at 0.4 GiB free RAM): keep the
  default 4 GiB.

## 2026-10-08

### Added
- MCP permission modes (No tools / Read-only / Ask before changes / Allow edits / Full access), protected paths, the
  no-read list (`blocked_paths`, `@defaults`), an internet tool, grep/glob/read_lines, `ask_user`, a headless browser
  and exec tools (`docs/DETAILS.md`, "Tools from MCP servers").
- Decode-window profile and the settings that moved it (`docs/SPEED_BRANCH.md`).

## 2026-10-07

### Added
- Learned expert profiles for the uncensored Q4_K_M and i1-IQ4_XS packs, plus the eval and sweep scripts.
