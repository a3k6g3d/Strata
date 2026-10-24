# Web UI: bringing the good parts of upstream's app into ours

Status: **design, 2026-10-24.** Nothing here is built yet except select-to-reply and side chats (`03f1a99a`, `3669dd5f`).

## The goal, and the rule that shapes it

Upstream's desktop app (Niko1221/Strata, `apps/host`, MIT) has a layout and a set of small behaviours that feel good to
use. Ours is a Python server with a plain web page (`serve/web`), and the same page is what the Discord bot's chats, the
desktop shell (`desktop/tauri`) and - with `host: 0.0.0.0` and an API key - a phone all use.

**We bring the behaviours into our page and do not port their app.** Measured 2026-10-24: their UI calls 102 backend
endpoints (`/api/chat`, `/api/tasks`, `/api/workspaces`, `/api/agent`, ...) served by their own Node host, about 12,000
lines, with its own agent loop. Putting their UI on our server would mean re-implementing that backend, and would lose
what only our server has (the conversation store, `eos_resample`, the Discord bot, the tool permission model, the fixes
tracked in `CHANGELOG.md`). One page, one server, so every client benefits.

## What we have, what we lack

Read from `serve/web` on 2026-10-24, against the screenshot of upstream's app (`docs/media/app-chat.png` there):

| Upstream's screen | Ours today | Gap |
|---|---|---|
| Chat list on the left, New chat, search | `sessions` pane: New chat, search, drag to reorder | none |
| Side panel (preview) on the right | `sidepanel`: a browser tab and a gallery tab, resizable | it shows pages and pictures, not the files the model just wrote |
| "Ran 2 commands, read a file, edited 3 files +5 -5" | `groupLabel()`: "Ran 3 commands, searched 5 times", opens to the calls | no `+5 -5`, and "changed N things" instead of "edited 3 files" |
| A card for a created file: type, name, size, **Open** | nothing | **new** |
| The preview opens by itself on the file it built | nothing | **new** (needs the file cards) |
| Chats grouped under a project/folder | a flat list | **new** |
| Model name and GPU in a card, bottom left | the header pill says only Idle / Writing | **new**, small |
| Thinking level as a chip in the composer | "Sampling and thinking" dialog behind a gear | surface it |
| Phone layout | some `@media` rules (640, 900, 1000 px) and `pointer: coarse`; **never looked at on a phone** | needs a pass |
| Microphone, "Subagents" menu | none | see "Not doing" |

## The pieces, in the order I would build them

### A. File cards, `+/-` counts, and the preview pane (biggest win, most self-contained)

**Built and checked 2026-10-24** (`ddc2dfe8`): a test chat on a throwaway mock server on another port, in the web app - 3 cards,
"Ran a command, edited 3 files +18 -2", Open showing an HTML page whose script ran and reported "parent page blocked: yes |
storage: blocked", the Source switch, a text file, and the card for a file edited from an unseen version disabled. Not
built: syntax highlighting, and the phase-2 route below.

*What the user sees.* After a turn that wrote or edited files, under the reply: the collapsed line reads "Ran 2
commands, edited 3 files +5 -5", and a card per file the model created - type badge, name, size, **Open**. **Open**
shows the file in the side panel: an HTML file runs (with a Source switch); anything else is shown as plain monospace
text - **no syntax highlighting yet**. It opens on that click only, never by itself.

*Data we already have.* The files tool server names its calls `write_file(path, content)` and `edit_file(path, old_text,
new_text, replace_all)`; both arrive in the chat as tool calls with their arguments. So:

- `+/-`: for `edit_file`, the lines of `new_text` and `old_text`; for `write_file`, `+` the lines written. (A `write_file`
  over an existing file is shown as `+N` only, because the page does not know what it replaced.)
- A card for each path in a `write_file` call, and each `edit_file` path not already carded.
- The preview of a `write_file` file is its `content` argument: **no new server route**.

*Security, because this runs model-written code.* The preview is an `<iframe sandbox="allow-scripts">` with **no**
`allow-same-origin`, fed by `srcdoc`. It has no access to Strata's page, its storage or its API key, and cannot make
requests as the page. (The page's own CSP, if any, is kept; I would add `frame-src` for it.) Nothing is fetched from disk
by path in this version.

*Phase 2 (not in the first cut).* Editing a file after it was written leaves the card's `content` stale. Fixing that needs
`POST /files/read {path}`, which would apply the same no-read list and allowed folders as the files tool server, accept
only paths this chat's tool calls touched, cap the size, and need the API key. I would not build it until the first cut
is in use.

*Tests.* The label/`+-` logic is a pure function: node tests in the style of `serve/test_web_queue.py`. The preview is
checked in the web app against the live server, as for select-to-reply, and the sandbox is asserted by a test that
reads the attribute off the rendered iframe.

*Size.* About 250 lines of JS, 60 of CSS.

### B. Model card and a thinking chip (small, visible)

Bottom of the chat list: the model name, quantization and GPU, with a green dot while the engine is up - from `/health`
and `/v1/status` (`model`, `machine.gpu`), which the page already polls. In the composer, a chip for the thinking level
(Off / Low / Medium / High) beside the permission selector, driving the same value the gear dialog sets; the request
already carries `reasoning_effort`. *Size:* about 80 lines. No server change.

### C. Projects (chats grouped under a name)

Upstream's "workspaces" are folders the agent works in. We do not have per-chat working folders: the exec tool server
has one (`workspace`), so a folder per chat would be a server feature (per-session `cwd` for the exec and files servers).
**First cut: projects as plain groups** - a name you give a set of chats, collapsible in the list, drag a chat onto one.
It needs one optional field (`project`) on the saved session, which the sessions store already round-trips as part of the
chat's metadata (to be checked before building: if it rewrites unknown fields away, the field goes into the order file
instead). Per-chat working folders can come later, on top. *Size:* about 200 lines of JS, 20 of Python.

### D. A phone pass (this is what makes "away from home" pleasant)

Not a redesign: a day with the page on a real phone. Expected list, to be confirmed by looking: the chat list as a
slide-in drawer, the side panel as a full-screen sheet with a back button, the composer pinned above the keyboard with
safe-area padding, 44 px touch targets (the `pointer: coarse` rule exists - to be checked), no hover-only controls (the
select-to-reply bar has to work with a long-press selection: it listens for `mouseup` and `keyup`, **not** for touch, so
**it does not appear on a phone yet**; the fix is `selectionchange` with a short debounce).

*Install like an app.* A web app manifest and icons let a phone put Strata on the home screen. Over plain `http` on a
home network that gives a shortcut that opens full-screen; the offline and push features need HTTPS, which `tailscale
serve` provides for the away-from-home setup.

### E. Not doing, and why

- **Microphone.** The browser's speech-to-text (`SpeechRecognition`) sends the audio to Google's or Microsoft's servers in
  Chrome and Edge. Strata's promise is that nothing leaves the PC. It could be done with a local Whisper model; that is a
  feature of its own (a model, a download, a CPU/GPU cost beside the chat model).
- **Subagents.** Upstream's agent loop spawns helper agents; ours is one loop with one model on one GPU, and a second
  concurrent request halves the speed of both. Not worth the complexity here.
- **Tasks / schedules.** Ours has none; upstream's task board belongs to its Node host.

## Build order and what each costs

| # | Piece | Needs a restart? | Touches the server? |
|---|---|---|---|
| A | file cards, `+/-`, preview pane | no (page files are read per request) | no |
| B | model card, thinking chip | no | no |
| D | phone pass (incl. the select-to-reply fix) | no | no |
| C | projects | yes (about 40 s) | a field on saved sessions |

My order: **A, then D, then B, then C.** A is the thing in the screenshot that makes the UI feel like an agent's workspace;
D matters most once the phone works from outside the house; B is small and can go any time; C is the one that touches
saved data and needs the most care.

## Decisions (owner, 2026-10-24)

1. **The preview opens on a click**, never by itself. (So the file card's **Open** is the only trigger; the "opens by itself"
   setting in piece A is dropped.)
2. **Projects get a working folder** - but per *project*, not per chat. A project is a name, a folder and the chats under
   it (upstream's "workspace": its screenshot has one folder, `pagoda-garden`, with its chats beneath). Checked: the
   command tool takes a `cwd` for every call (`run_command(command, shell, cwd, timeout_s)`, `run_python(code, cwd,
   timeout_s)` in `tools/strata_exec_mcp.py`), so no second tool server is needed - the server fills in the project's
   folder as the default `cwd` for a chat in that project and tells the model which folder it is working in. The files
   tool's allowed folders (the drives it lists) stay as configured; a project folder outside them is refused with the files
   tool's own message. This replaces "plain groups first": piece C is now **project = name + folder + chats**, about
   300 lines of JS and 80 of Python, and needs a restart (about 40 s).
3. **Installable on the phone, so HTTPS.** That needs Tailscale (`tailscale serve` gives the PC a real certificate on a
   private address; a home-screen app, service workers and clipboard access all need it). Not installed here yet; the
   installer is Tailscale's own `tailscale-setup-1.104.2.exe`, 1.4 MB from pkgs.tailscale.com. Installing it adds a Windows
   service and a network adapter, and signing in is to the owner's own Tailscale account, so those two steps are the
   owner's. Piece D's manifest and icons can be built first and tested over plain `http` as a shortcut.
