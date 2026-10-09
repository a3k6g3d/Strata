# Changelog (the `speed-work` fork)

Changes on top of upstream's engine (0.1.39). The engine's own version number stays upstream's; these entries are
dated, and the fork's releases follow [SemVer](https://semver.org) once it tags them (features = minor, fixes = patch).

## Unreleased - 2026-10-09

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
