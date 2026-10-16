<h1 align="center">Strata, speed-work fork</h1>

<p align="center"><b>A 125-billion-parameter model that works for you on one mid-range gaming PC, tuned until the PC was the limit</b><br>
12 GB graphics card · 64 GB RAM · Windows · a local assistant with tools, chats, a Discord front end and desktop apps</p>

This is a personal fork of [Strata](https://github.com/Niko1221/Strata), the engine that runs **Qwen3.8-Flash-Next** on an ordinary PC. It
exists because we wanted one specific thing: to use that model every day as a coding and research assistant on the machine we actually own,
and to find out how fast that machine can really go. This page says what we changed, what we measured, and what did not work.

> **Credit where it is due.** The engine, the model support, the server and the web app are Niko1221's and the Strata contributors' work, under
> the MIT licence in [`LICENSE`](LICENSE). This fork builds on [architectds/Strata](https://github.com/architectds/Strata)'s speed fork, and every
> upstream commit is in this history. The install and usage instructions have not changed; they are in
> [README.upstream.md](README.upstream.md). Most of this fork's own code was written with Claude Code, and the changes come with tests.

## The machine, and what tuning did for it

An RTX 5070 with 12 GB, a six-core i5-10600K (AVX2 only), 64 GB of DDR4-2666 and an NVMe drive, on a PCIe link that only runs reliably at Gen 2.
The IQ3_S build of the model does not fit on that card, so most of its experts live in RAM and some in the model file, and the engine moves
them around. We spent a few weeks measuring where the time goes.

| | before | now |
|---|---|---|
| Decode, agent chats | 14.4 tok/s | about **31 tok/s** |
| Decode, general prompts | 14.4 tok/s | about **30 tok/s** |
| Prompt reading | 305 tok/s | about **610 tok/s** |
| Expert reads from the model file per generated token | 15.5 | 7.4 |

The biggest single change was **not a setting**: an expert profile learned from our own chats (+11% decode on agent work, file reads cut by
more than half, no loss on ordinary prompts when ordinary prompts are in the mix). Everything else we tried, including most of the usual
flags, was inside the measurement noise. The full story, with the dead ends, is in **[docs/TUNING_LOG.md](docs/TUNING_LOG.md)**.

## What this fork adds

**Learn your own expert profile.** [`tools/learn_profile_from_chats.py`](tools/learn_profile_from_chats.py) turns your saved chats into replay
prompts, replays them against your server and lets the engine write a profile ranked by the experts *you* need. How it works, how to
check it helped, and the mistakes to avoid (a profile learned on one kind of work hurts the others):
[docs/LEARN_A_PROFILE.md](docs/LEARN_A_PROFILE.md).

**A local assistant, with permissions that stay out of the way.** The model can use tools: look at and change files, search the code, search
the web, read pages in a headless browser, run commands, and ask you a question. What it may do by itself is one drop-down in the chat page:

| mode | what happens |
|---|---|
| Read-only | it may look, nothing that changes things runs |
| Ask before changes | every change and every command waits for your click |
| Allow edits | file edits run by themselves, commands and deletes still ask |
| **Auto** | routine calls run by themselves; a rule set asks only for the risky ones (formatting a disk, the registry, security settings, running downloaded code, sending data off the PC, deletes outside a scratch folder, raw disk access) |
| Full access | everything runs by itself except what touches the protected folders |

Two lists apply in every mode: a **no-read list** (browser profiles, SSH and cloud keys, `.env` files, password stores: refused outright, never
asked about) and **protected folders** (the Windows system folders: always ask). Paths are resolved first, so `..`, links and different
spellings are seen for what they lead to. Details: the MCP section of [docs/DETAILS.md](docs/DETAILS.md).

**Chats that behave like chats.** Conversations are saved as sessions with a sidebar, drag-to-reorder, and a side panel that can show a web
page or a gallery next to the chat. There are desktop apps for Windows ([`desktop/`](desktop), Tauri and Electron).

**Discord as a remote control.** The model answers in a private Discord channel, with tool approvals as buttons, and Discord chats are the same
chats as the app's (`!chats`, `!open`, `!new`). You can send it pictures and text or code files, and it can send files back from one folder you
choose, each one confirmed with a button. Setup in five minutes: [docs/DISCORD.md](docs/DISCORD.md).

**Conversations that survive a switch or a restart.** Reading a long conversation again takes 8 to 34 seconds on this machine. With
`"conversation_store": {"enabled": true}` the server keeps the engine's state of each conversation on the disk and restores it when you come back:
0.7 s after a full restart instead of 18.6 s (a 25K-token conversation: 34 s against 0.6 s). The numbers and the design are in the
[tuning log](docs/TUNING_LOG.md#conversations-on-the-nvme).

**Guard rails for a PC that is also your desktop.**
- A warning when the Windows commit limit is nearly used (the PC pages and the model stalls long before it says so).
- `fit_max_tokens`, a default `sampling` block (clients that send no settings otherwise get greedy output) and an opt-in warm-up
  (`"warmup": {"enabled": true}`) that removes most of the slow first answers after a start.

**Fixes we found on the way.** The web search tool sent a parameter that made Bing answer a different question (airline pages for a
disk-repair query). A raw device name in a command (`\\.\PhysicalDrive4`) was refused as an unreadable path. Full access asked before every web
call. All fixed, each with a test; see [`CHANGELOG.md`](CHANGELOG.md).

## Settings we run

For a 12 GB card with 64 GB of RAM (adjust the sizes to your machine; the order of the biggest effects is in the tuning log):

```
--pack <your pack>            --native <first IQ3_S shard>      --ple-gguf <second shard>
--expert-profile <your learned profile>                          --resident-budget-gib 36
--expert-cache auto           --prefill auto                    --ple-io direct
--spec 4                      --spec-min-p 0.7                  --mtp <the Basalt draft layer>
--max-context 65536           --kv int8                         --vram-reserve-mib 640
--adapt-swaps 8
```

The pinned budget is the one to be careful with: the machine's real limit is free RAM, not VRAM. With a 65,536-token window and 40 GiB pinned, a long
chat pushed free RAM under 1 GiB and Windows started paging; the generation stalled with the GPU idle. Set the budget from free RAM and
`Pages Input/sec`, not from what loads. [The RAM incident](docs/TUNING_LOG.md#the-ram-incident-and-the-64k-window) has the numbers.

## What did not help

Measured, and not worth repeating on a machine like this one: `--kv k8v4`, `--prefill auto:16384`, a smaller VRAM reserve, `--adapt-async`,
`STRATA_PIN_BY_NEED`, prompt-lookup drafting (`--suffix-draft`, `--lookup-chain`), an English or chat-counted draft vocabulary, higher draft
thresholds beyond 0.7, and stacking several of these (the stack was 12% *slower*). We also looked at a second PCIe slot (x4: no faster), a
Rust rewrite (the hot path is vector code and kernels either way) and several hardware upgrades. All of it, with the numbers, is in the
[tuning log](docs/TUNING_LOG.md), including how noisy these measurements are: identical settings differed by up to 10% from one start to the
next, which is why file reads per token, not decode speed alone, is the figure to trust.

## Branches

| branch | what it is |
|---|---|
| `main` | the current line, and the default: upstream 0.1.44.1 plus everything above |
| `speed-work-0.1.44.1` | the same commit as `main`, named by the upstream version it is built on |
| `best`, `main-old` | the speed fork this one started from, kept for reference |

Each new upstream release is merged into a branch named after it; `main` follows the newest.

## Install and use

Nothing about installing changed: follow [README.upstream.md](README.upstream.md) (it covers the model sizes, the one-click setup, the
chat page and the API), then add this fork's options to your config as described in the documents linked above. The fork's own changes are listed
in [`CHANGELOG.md`](CHANGELOG.md).

## Credits and licence

MIT, as upstream ([`LICENSE`](LICENSE)). The engine and server: [Niko1221/Strata](https://github.com/Niko1221/Strata) and its contributors. The
speed fork this one grew from: [architectds/Strata](https://github.com/architectds/Strata). The self-distilled draft layer: the Basalt project
([jesdga95/basalt](https://github.com/jesdga95/basalt)). Ideas we weighed against our own numbers came from the llama.cpp expert-cache work and
from [syv-ai/HyperQwen](https://github.com/syv-ai/HyperQwen).
