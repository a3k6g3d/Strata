# Changelog (the `speed-work` fork)

Changes on top of [Niko1221/Strata](https://github.com/Niko1221/Strata) (the original project, MIT) as carried by
[architectds/Strata](https://github.com/architectds/Strata), whose engine and server this builds on (0.1.39, then
0.1.40.2); all of their work and history are in this repository. The engine's own version number stays upstream's; these entries are
dated, and the fork's releases follow [SemVer](https://semver.org) once it tags them (features = minor, fixes = patch).

## Unreleased - 2026-10-10

### Added (2026-10-13, Monitor shows the expert cache hit rate)
- The Monitor page now shows the expert cache as a line of its own: hit rate since start, hits of looked-up layers over the layers that ask for
  them, hit/miss/evicted/loaded counts, offloaded experts, pinned layers, a layer resident in VRAM and the budget it spends. The numbers come from
  the engine over `/metrics`, and need `--api-monitor` (they read "not exposed" without it). The same numbers go into each recorded request as
  `cache`, so the request table can be added up by hand. A note in the docs says what to look for and what budget to give.

### Added (2026-10-16, the tuning log, learning a profile from your chats, and a README of our own)
- `docs/TUNING_LOG.md`: every measurement from tuning one 12 GB machine (quantizations, PCIe, the settings sweeps, draft layer and speculation, three engine
  versions, expert profiles, the stacking test, the RAM incident, the cold start), with the dead ends and how noisy the numbers are.
- `tools/learn_profile_from_chats.py` and `docs/LEARN_A_PROFILE.md`: learn an expert profile from saved chats (+11% decode and 56% fewer file reads on agent
  chats, no loss on general prompts when general prompts are in the mix). 6 new tests.
- `README.md` now tells this fork's story; the text it started from is kept as `README.upstream.md`. `docs/BENCH_NOISE.md` joins the sweep write-ups, and the
  fork's own scripts and docs no longer name this PC's folders (`tools/run_ab_offline.ps1` finds its repository, config and Python by itself).

### Fixed (2026-10-16, browse_url returned no page on Edge 156)
- Edge 156's headless mode exits at once with no output for every flag combination we tried (`--dump-dom`, `--screenshot`, old and new headless, a
  file or a pipe for stdout), so `browse_url` always failed with "the browser returned no page"; Chrome 155 renders the same page with the same flags.
  The tool now tries every installed browser (the one in `STRATA_BROWSER` first), skips one that returns no page when another renders it and tries that
  one last from then on, and keeps what the browser printed for the error message. 1 new test; the two real-browser tests that failed now pass.

### Added (2026-10-16, benchmark tooling: interleaved arms, a noise verdict, machine state, a record per run, a wait)
- `tools/strata_ab.py --rounds K` launches every arm K times with the arms taking turns (the order reversed every second round) and `--noise-floor N`
  launches the baseline alone N times; `tools/summarize_ab_sweep.py` compares each launch with its own round's reference, leaves cold-region launches out
  of the medians and prints a noise floor and an "above noise / inside noise" verdict (`--noise DIR` uses a noise-floor run; `runs.jsonl` is read in place
  of the folders). A tie in "most company" now goes to the arm that started first.
- `tools/bench_state.py`: uptime, GPU clocks, temperature, power, throttling, free RAM and busy background processes, read in-process through NVML, in
  every `results.json` (`state`); `tools/bench_record.py`: one line per run in `runs.jsonl`; `tools/wait_for.py`: block until a log pattern, a process
  exit or a timeout. 8 + 5 + 11 + 10 + 7 new tests; the benchmark's own tests now look at result folders only.

### Changed (2026-10-18, the conversation store checks RAM and remembers which setup saved a file)
- A save or a restore now needs `min_free_mib` (default 2560, 0 = off) of RAM to stay free after it: the PC runs close to full with the model's pinned
  experts, and a state write that pushes it into paging costs more than reading the conversation again. When there is no room the request reads its
  prompt as usual and the save is tried again on the next request (logged once per ten minutes). Idea taken from the park gate of upstream PR #2915.
- Every file carries a fingerprint of the model files, the pack and draft folders, the context size, the KV format and the engine build's folder; files
  saved under another setup are removed at start, older files without one are kept, and a file the engine refuses twice is dropped. 10 new tests.

### Added (2026-10-16, an opt-in conversation store on the disk)
- `"conversation_store": {"enabled": true}` (`serve/conversation_store.py`) saves the engine's conversation to disk when a request for another conversation
  arrives and again as it grows (every 6,000 tokens), and restores it when a prompt continues a saved one, using the engine's `SAVE` / `RESTORE`. A 25K-token
  conversation: 34 s to read again, 0.6 s to restore (585 MB on disk). After a server restart: 0.7 s instead of 18.6 s. Options: `folder`, `max_gib` (20),
  `min_tokens` (4,000), `grow_tokens` (6,000). Off by default; not available with parallel requests. 14 new tests.

### Added (2026-10-16, an opt-in warm-up after the model loads)
- `"warmup": {"enabled": true}` in the config sends a few short typical requests (or the `"prompts"` you list, at most 12, `"max_tokens"` 16-512) once the
  model is loaded (`serve/warmup.py`). Measured on this PC: the first answers after a start ran at about half speed (13.9, 18.8, 20.5, 21.8 tok/s on four
  identical requests, expert-cache hit rate 31% -> 58%) because the GPU expert cache adapts to what is asked; the warm-up moves that before the first real
  question. Off by default. 4 new tests.

### Added (2026-10-15, files from Discord's side: `!files`, `!send`, `[[send: path]]`)
- The Discord bot can send a file, from the workspace folder only (`discord.send_folder`, default `workspace` next to the config): `!files` lists it,
  `!send NAME` sends one, and the model can offer one with `[[send: path]]`, which waits for a "Send it" button (3 per answer at most). Paths are
  resolved first, so `..`, absolute paths and links out of the folder are refused, as are files over 8 MB and anything on the no-read list. 9 new
  tests (`serve/test_discord_send.py`; the symbolic-link case is skipped on an account that cannot create links).

### Fixed (2026-10-15, Discord attachments were dropped silently)
- The Discord bot used only pictures and ignored every other attachment without saying so, so the model answered "I cannot see that". Text and code
  files are now read into the prompt (200 KB each, 120,000 characters per message), and video, audio, PDFs and oversized files are named back in the
  channel with the reason and to the model as a note. The bot still sends text only. 2 new tests.

### Changed (2026-10-12, Full access no longer asks before a web call)
- In Full access a web search or fetch after a local tool runs without the leak click, so Full access behaves like a bypass mode: commands, file
  changes and web calls run by themselves. Still refused or asked in every mode: the no-read list (refused), the protected Windows folders (asked). Allow
  edits, Ask before changes and Auto keep the leak guard. 1 test changed, 1 added.

### Fixed (2026-10-12, web_search returned unrelated pages)
- `web_search` sent `setlang=en` to Bing, which made it answer a different query for uncommon terms ("SATAFIRM S11 fix" returned airline pages, a
  long MPTool query returned Instagram and news sites). The query now goes alone and returns the right pages.

### Fixed (2026-10-12, disk tools were refused as "no-read list")
- A command that names a raw device (`smartctl -a "\\.\PhysicalDrive4"`) was refused by the no-read scan: a device name cannot be resolved to a path and
  an unresolvable path counts as blocked. The scan of a command's text now lets a device name pass (it is not a file); in auto mode a command that reads a
  raw disk or volume device asks, since a raw read skips the file rules. File-tool arguments are unchanged (still fail closed). 3 new tests
  (`serve/test_device_paths.py`).

### Changed (2026-10-12, Discord chats are Strata chats)
- A Discord conversation is now saved as a chat of the Strata app ("Discord · ..."): it appears in the chat list and can be continued from
  the app or from Discord. The bot starts each answer from the saved chat (so what was added in the app is included); `!new` starts a new chat,
  `!chats` lists the latest ones and `!open N` continues one here. The app polls the chat list every 8 s (when idle) and reloads the open chat
  when the server has a newer copy. 8 new tests.

### Added (2026-10-11, side panel: Browser and Gallery)
- A **side panel** on the right of the chat (toggle in the chat bar). **Browser**: a link in the chat opens there instead of a separate
  window (Ctrl-click still opens your own browser), with an address bar, reload and "Open in browser". Before showing a page the server
  checks whether the site allows being framed (`GET /panel/frameable`, `serve/panel.py`: X-Frame-Options and CSP frame-ancestors) and
  says so, with a button for your own browser, when it does not. Only http(s) pages of the internet are shown: this server's own page and
  anything on this PC or the local network are refused. **Gallery**: the pictures sent in the chat as thumbnails and a viewer
  (arrow keys, Save); clicking a picture in the chat opens it there. The panel can be resized by dragging its left edge. 4 tests.

### Added (2026-10-11, chat list order and message width)
- The chats in the sidebar can be **dragged into your own order** (or moved with Alt+Up / Alt+Down); the order is kept by the server
  (`POST /sessions-order`, `sessions/_order.json`), so every window shows it. While you have an order the day groups give way to a
  "Your order" heading with a **Sort by date** link; a new chat goes on top. The desktop app's own file-drop handler is switched off
  (`disable_drag_drop_handler`) so the page's drag and drop works in it.
- The messages are now exactly as wide as the message box under them (they were narrower).

### Added (2026-10-11, Auto permission mode)
- A new **Auto** mode in the permission drop-down (between "Allow edits" and "Full access"): reads and ordinary edits run by
  themselves, and a command or program is read first (`serve/auto_mode.py`). Routine ones (listing, git status, GPU queries, running
  a script, deleting a temp file) run without a click; the ones that delete outside the temp folders, change the registry or
  system/security settings, run as administrator, run decoded or downloaded code, upload data, install or remove software, push
  git changes or stop programs ask, and the approval card says why. The protected folders and the no-read list still apply first.
  Plain text rules, not a model: fast and predictable. `mcp.auto_scratch_paths` adds folders where deleting is routine. 7 new tests.

### Added (2026-10-11, Discord front end)
- `serve/discord_bot.py`: talk to the model, and let it use its tools, from the Discord app (similar to Claude's remote sessions).
  Optional, off until the config has a `discord` section. Only the accounts in `allowed_user_ids` are answered; tools never go above
  `ask` (changes and commands show Allow / Deny buttons; the protected folders and the no-read list apply as in the web app).
  Streams the answer into one message, keeps one conversation per DM or channel, takes pictures, `!new !stop !status !mode !think`.
  Setup in `docs/DISCORD.md`; needs `pip install discord.py httpx`. 14 tests.

### Added (2026-10-11, memory commit warning)
- The server and the web app now watch Windows' **commit headroom** (RAM + pagefile minus what is promised). Under 8 GiB or 10% left
  the app shows a warning, under 3 GiB an error, and the server prints one at start-up; `/metrics` has a `commit` block. It says
  to enlarge the pagefile on the fast drive or lower `--resident-budget-gib`. New `serve/commit.py` with tests.

### Changed (2026-10-10, merged Niko1221/Strata v0.1.42)
- Merged `niko/main` (engine 0.1.42, 386 commits) onto the fork; conflicts in `serve/server.py` kept both sides, the prefill/generate
  CPU-share conflicts took Niko's version. All 652 server tests pass. On IQ3_S at a 32 GiB RAM budget: decode 17.6 -> 21.8 tok/s,
  SSD reads/token 26.8 -> 15.2, GPU-cache hit 50.8% -> 59%, prompt 31 -> 44 tok/s; hard set 46/47 unchanged.
- The live model is now SC117's GSQ-RCO IQ3_S (was mradermacher i1-IQ4_XS): same hard-set score, ~35% faster.

## Unreleased - 2026-10-09

### Fixed (2026-10-10, sessions survive a hard reset)
- A chat file could come back **all zeros** after the PC reset while it was being saved: the file was written to a
  temporary name and renamed, but its data was never forced to the disk, so Windows kept the rename and lost the data.
  Saves now fsync before the rename (`atomic_write_json`, also used for the summary cache), the previous version is
  kept as `<id>.json.bak` (at most once a minute), a damaged chat is restored from it, a damaged list entry is rebuilt
  from the conversation, and a file nothing can be read from is renamed `.damaged`, not deleted.

### Fixed (2026-10-10, chat page)
- A chat is saved while an answer is being written (every 4 s, and the question at once), not only when the answer
  ends: a long tool turn that was cut off (the app quit, the model stopped) used to be lost, and the model redid
  its work. What it had said and the tool calls it had finished are kept, the answer is marked "Stopped before it
  finished", and the next message carries them back to the model, so "continue" picks up where it was.
- The summary of a long chat is kept in `<sessions folder>/_summary-cache.json`: a restart no longer summarizes
  the same chat again (about a minute).

### Changed (2026-10-10, desktop app)
- The Tauri app is the one installed ("Strata", per user, shortcuts on the Desktop and in the Start Menu). The Electron
  app was uninstalled and its build files deleted; its source stays in `desktop/electron` and still builds.

### Added (2026-10-09, desktop app)
- `desktop/tauri` 0.1.0: the same app on Tauri 2 (Rust, the system's WebView2): 1.2 MB installer against the
  Electron app's 111 MB, 3.5 MB installed against 369 MB; memory about the same (120 MB against 106 MB, WebView2's
  processes). The chat page has no access to the app's commands (checked). It shares `desktop.json` with the Electron app.
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
