# Changelog (the `speed-work` fork)

Changes on top of [Niko1221/Strata](https://github.com/Niko1221/Strata) (the original project, MIT) as carried by
[architectds/Strata](https://github.com/architectds/Strata), whose engine and server this builds on (0.1.39, then
0.1.40.2); all of their work and history are in this repository. The engine's own version number stays upstream's; these entries are
dated, and the fork's releases follow [SemVer](https://semver.org) once it tags them (features = minor, fixes = patch).

## Unreleased - 2026-10-09

### Added (2026-10-09, desktop app)
- `desktop/electron` 0.1.0: Strata in its own window (Electron 44): starts the model if it is not running and shows
  how the loading goes, tray icon (restart / stop the model, open the log), window place remembered, quitting stops
  the model it started. Installer and portable exe from `npm run dist`. A Tauri build is planned after this one.

### Added (2026-10-09, chat page)
- Separate chats, listed on the left like Claude's: new chat, open, rename (double-click), delete, grouped by day.
  The server keeps them as files next to the run config (`serve/sessions.py`, `GET /sessions`, `POST /sessions/<id>`,
  `POST /sessions/<id>/delete`; own page only, ids `a-z0-9-` only); the browser's old single chat becomes the first
  session. Without a config the page keeps one chat in the browser as before.

### Changed (2026-10-09, chat page)
- Tool calls that follow each other are one collapsible group, like the tool lines in Claude Code: a closed
  summary ("Ran 3 commands, searched 5 times (1 failed)"), what is running or waiting now, and the single calls
  inside. A group opens by itself while a call waits for your click or asks you something.

### Fixed (2026-10-09, after first use of the port)
- Long-chat summarizing no longer leaves the page blank: it runs on a thread with keep-alives, the page shows
  "Summarizing the earlier conversation...", **Stop cancels it** (it used to run on, blocking the next request), a
  summary is cached by the digest of the messages it covers so the next turn of the same chat reuses it (it was
  redone every turn), and it is capped at 800 tokens (was 1,500).
- The chat now follows the newest text however much a frame adds (it only followed when within 120 px of the
  bottom, so a tool block or long paragraph left it behind): pinned to the bottom until you scroll up to read,
  pinned again when you scroll back down or send a message.
- The "Summarizing..." banner showed on every message of a long chat: shortening old tool results tokenized the
  whole chat once per message (seconds), and anything slow was announced as a summary. It now tokenizes a few
  times in total, the banner appears only when the model is really writing a summary, and each fit logs its time.
- Image encoding moved to the CPU in this PC's config (`vision.gpu` false, 768 tokens), so the encoder takes no
  VRAM from the expert cache (pictures take 10-30 s). The engine's "LOW: 160 MiB of VRAM free" start-up warning
  stays: it comes from `--vram-reserve-mib 640`, chosen on purpose (+5% decode over the default 1022).

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
