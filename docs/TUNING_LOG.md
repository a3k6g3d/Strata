# Tuning log: what we measured on a 12 GB card

Everything we tried while getting Strata's IQ3_S build of Qwen3.8-Flash-Next to run as fast as one mid-range PC allows, with the numbers,
the ones that did not hold up, and the method. Dates are 2026-10 unless stated. It is a log of one machine, not a benchmark suite:
read the numbers as "what this box did", and see [Noise](#noise-and-method) before trusting any difference under about 10%.

**The machine:** RTX 5070 12 GB on PCIe 2.0 x16 (about 7 GB/s measured), Intel Core i5-10600K (6 cores, AVX2, no AVX-512), 64 GB DDR4-2666
(four DIMMs), a SATA-class budget NVMe for the model files, Windows 11. The model is the IQ3_S GSQ-RCO build (two GGUF shards, about 78 GiB,
experts about 53 GiB) with its MTP draft layer, run through Strata 0.1.42, then 0.1.43 and 0.1.44.1. From 2026-10-18 the CPU is an i7-11700K with PCIe 4.0, the engine
0.1.45 and the model Swift 1.5; the first sections describe the i5 machine ([what changed](#a-faster-cpu-0145-and-swift-15)).

## The short version

| | start of the tuning | now |
|---|---|---|
| Decode, agent prompts | 14.4 tok/s | about 31 tok/s |
| Decode, general prompts | 14.4 tok/s | about 30 tok/s |
| Prompt reading | about 305 tok/s | about 610 tok/s |
| Expert reads from the model file, per generated token (agent prompts) | 15.5 | 7.4 |

What made the difference, in order of size:

1. **An expert profile learned from real use** (+11% decode on agent work, +8% on other software work, no loss on general prompts, file reads
   cut by more than half). [Learning a profile from your own chats](LEARN_A_PROFILE.md) is the one change on this page that is not a setting.
2. **The memory plan:** how many GiB of experts are pinned in RAM. About +0.7 tok/s for every GiB, which is also why the pinned size is the
   first thing to give back when the PC starts paging (see [the RAM incident](#the-ram-incident-and-the-64k-window)).
3. **Reading model files directly** (`--ple-io direct`): prompt reading about twice as fast on long prompts.
4. **A draft layer trained on the model's own output** (the Basalt layer) and a higher draft threshold (`--spec-min-p 0.7`): about +3% each.
5. The PCIe link. Gen 3 would be worth about +17% but is not stable on this board.

And one change that is not about decode at all: **keeping conversations on the NVMe** turns re-reading a long conversation after a chat switch or a restart
(8 to 34 seconds) into a restore of about a second ([Conversations on the NVMe](#conversations-on-the-nvme)).

Almost everything else we tried was inside the noise. Those results are listed too, so nobody has to repeat them.

## Contents

- [Quantizations](#quantizations)
- [Where a decode step's time goes](#where-a-decode-steps-time-goes)
- [PCIe](#pcie)
- [Engine settings that were swept](#engine-settings-that-were-swept)
- [Draft layer and speculation](#draft-layer-and-speculation)
- [Engine versions: 0.1.42, 0.1.43, 0.1.44.1](#engine-versions-0142-0143-01441)
- [Expert profiles](#expert-profiles)
- [Stacking small gains](#stacking-small-gains)
- [The RAM incident and the 64K window](#the-ram-incident-and-the-64k-window)
- [Cold start and the warm-up](#cold-start-and-the-warm-up)
- [Conversations on the NVMe](#conversations-on-the-nvme)
- [A faster CPU, 0.1.45 and Swift 1.5](#a-faster-cpu-0145-and-swift-15)
- [Ideas we looked at and did not do](#ideas-we-looked-at-and-did-not-do)
- [Noise and method](#noise-and-method)

## Quantizations

Compared on the same prompts (early in the tuning, before most settings below). The 4-bit file, the 3-bit file and the 2-bit file are
different fine-tunes, so quality differences are indicative, not clean.

| file | decode tok/s | 19K-token prompt | perplexity | 47-item hard set |
|---|---|---|---|---|
| IQ4_XS | about 14.9 | 31.6 s | 3.75 (reference) | 45 |
| IQ3_S | 16.8 to 17.0 | 25.0 s | 4.06 (+8%; top-1 agreement 81%) | 43 |
| IQ2_XS | 22.5 to 25.2 | 17.3 s | 3.76 (different fine-tune) | 44 |

Every miss on the hard set was a reply cut off at the 4,000-token limit, not a wrong answer. With the rest of the tuning applied, IQ3_S
decoded 21.8 tok/s against 14.4 for IQ4_XS on the same settings and answered 46 of the 47 items. We kept IQ3_S: IQ4_XS no longer fits
enough experts in RAM, and IQ2_XS lost more than we wanted for the speed. Upstream's own comparison against Q8_0 (4,995 positions) puts IQ3_S
at 87.7% same top token (mean KL 0.144), IQ4_XS at 88.9% and UD-Q4_K_XL at 93.1%.

## Where a decode step's time goes

Measured with Nsight Systems and the engine's decode timing (CUDA graphs need `--cuda-graph-trace=node` to show kernels). One decode window
of about 115 ms (about 2.2 tokens) split into: the CPU running the experts that live in RAM, about 52 ms; waiting for experts read from the
file, about 26 ms; waiting for the GPU, about 11 ms; the rest, about 25 ms. The CPU pool was saturated (six participating threads), the GPU
was mostly waiting. That is the picture behind almost every result here: **this machine is limited by its CPU and by PCIe, not by the GPU**,
so changes that save GPU time or VRAM mostly do nothing, and changes that move work off the CPU or avoid a transfer pay off.

## PCIe

The card trains at Gen 3 x16 but the link is not stable: the replay counter climbs about 100 per second at idle and 2,000 to 2,900 per second
under decode, and the PC hung three times under load before we stopped. At Gen 2 the counter stays flat. We tried the hidden BIOS
equalization settings (adaptive software, static and other modes) through the setup variable, one at a time, and none of them stabilised
Gen 3; each time the guard script stopped the engine on the error rate. The cost is real: held prompts decoded 15.6 to 16.6 tok/s at Gen 3
against 13.3 to 14.1 at Gen 2 on the same build, about +17%. The chipset limits this board to no CPU overclock and memory up to DDR4-2666 with
four DIMMs.

A card in the board's second x16 slot would run at x4 through the chipset (about 7.9 GB/s, shared with the NVMe and USB devices), which is no
better than the Gen 2 x16 link we already use. Third-party measurements of an x4 slot agree.

## Engine settings that were swept

Held prompts, three passes per setting with the first dropped, a repeated control at the end. "Flat" means inside the noise.

| setting | result |
|---|---|
| `--adapt-swaps` | 8 is best here; 24 and 48 are also better than the default 96; 0 and 4 only look slow in one sweep because they were run first (see the cold start) |
| `--adapt-decay` 0.65 / 0.80 | flat |
| `--adapt-async 1` | slower on 0.1.42; flat on 0.1.44.1 (23.72 against 23.77 tok/s) |
| `--ple-io direct` against `mmap` | long-prompt reading about twice as fast |
| `--resident-budget-gib` | about +0.7 tok/s per GiB between 32 and 40 GiB, limited by what Windows can spare |
| `--vram-reserve-mib` 640 / 384 / 256 / 128 | 28.8 / 29.2 / 29.0 / 28.4 tok/s: flat, kept at 640 for headroom (the display runs on the integrated GPU, so Windows holds only about 460 MiB of the card) |
| `--kv k8v4` | flat (+0.8% to +1.2%), and it makes the KV cache slightly lossy |
| `--prefill auto:16384` | flat alone (+0.6% to +1.5%), but worse in the stack below |
| `--kv-resident` | arms were not single-variable (it switches off the elastic KV), no gain |
| `pcie_frac` (the PCIe share) | the engine's own automatic value is best; 0.2, 0.5 and 0.7 were worse |
| CPU pool width and tasks | ties; the pool already uses all useful cores |
| `STRATA_PIN_BY_NEED=1` (0.1.44) | −4.6%, and −2.6% together with adapt-async |

## Draft layer and speculation

Speculation here is the engine's MTP draft layer, `--spec 4` and a draft threshold `--spec-min-p`. Verifying a longer window pulls in more
experts, and on a model whose experts are partly in RAM those cost real time, so longer drafts are not free the way they are on a dense model.

| change | result |
|---|---|
| Basalt draft layer (a self-distilled MTP layer for this model family) | +3.6% (29.18 against 28.26 tok/s), acceptance 69.4% to 72.1%, output unchanged. Adopted. |
| `--spec-min-p` 0.4 | −2.3% to −3.7%, acceptance 70% |
| `--spec-min-p` 0.6 / 0.7 | +2.9% / +3.1% on 0.1.44.1 (about +2% to +4% on 0.1.43), acceptance 75% to 86%. 0.7 adopted. |
| `--spec-min-p` 0.8 / 0.9 and `--spec 3` (on the agent profile) | all within noise of 0.7: acceptance reaches 96% but speed does not rise |
| `--mtp-hnorm stream` | acceptance up, speed flat |
| draft vocabulary: English subset, or counted on our own chats (42,000 ids, missing 0.18% of real tokens against 2.83%) | flat |
| `--suffix-draft 3`, `--lookup-chain 4` | one run gave +4.8%; the same flags on the next engine gave −1.4%. Not adopted. |
| `--spec-adaptive` | under-drafts on a noisy transfer cost; lost |

## Engine versions: 0.1.42, 0.1.43, 0.1.44.1

The three engines are neutral for this model (23.9, 23.2 and 23.8 tok/s on the same prompts). 0.1.44 and 0.1.44.1 change defaults only for
AMD and Unsloth files and add fixes; answers on this model are byte-identical to 0.1.43.1 in upstream's own identity gates. We merge each
release for the fixes, not for speed.

## Expert profiles

The engine fills its GPU cache and chooses which experts to pin in RAM from a **profile**: a ranking of the experts by how often a workload
needs them. The shipped profile is built from generic chat. A profile learned from the workload actually in use is a different ranking, and it
matters a lot, because on this machine a miss means reading an expert from the file. Procedure: [LEARN_A_PROFILE.md](LEARN_A_PROFILE.md).

Three profiles, each measured on three held sets of prompts that were not in the learning data. The agent set is turns from the author's own
coding-agent chats (with tool calls and results), the software set is turns from coding sessions in other projects, the general set is
ordinary chat, writing and questions. Decode in tok/s (the engine's own figure), and expert blobs read from files per generated token.

| profile | agent set | software set | general set |
|---|---|---|---|
| shipped | 28.4 tok/s, 15.5 reads | 27.1, 17.1 | 30.2, 7.7 |
| learned on agent chats only | 31.7, 6.5 | 30.3, 9.0 | 28.8, 16.5 |
| **learned on a mix** (agent + software + general) | **31.6, 7.4** | **29.5, 8.4** | **29.8, 6.7** |

- A profile learned on generic prompts (the one we made first) gave nothing on agent work: 28.0 tok/s and 16.0 reads against 28.6 and 14.5 for
  the shipped one. The gain comes from learning on the kind of work you do.
- Learned on agent chats alone, the profile cuts file reads by 56% on agent work but doubles them on general prompts. The mixed profile keeps
  nearly all of the gain and none of the loss. For a profile to be general, put general prompts in the mix.
- Decode figures carry the usual run-to-run noise (two identical shipped-profile starts differed by 9%). **File reads per token did not**: the two
  baselines read 14.5 and 14.0, which is why we trust the result.

## Stacking small gains

The idea: several changes that each look slightly positive but inside the noise might add up. We tested it, on the agent set with the learned
profile (passes 2 and 3 of three, same method as above):

| variant | decode tok/s | drafts accepted |
|---|---|---|
| core: learned profile + `--spec-min-p 0.7` | 29.5 and 31.8 (two runs) | 90% |
| core + `--prefill auto:16384` + `--vram-reserve-mib 384` | 26.3 | 89% |
| the above + suffix drafting + lookup chain + `--mtp-hnorm stream` | 27.6 | 86% |

They did not add up; the combination was about 12% slower than the core on its own. Small positives are not safe to stack unmeasured. We kept
only the core.

## The RAM incident and the 64K window

Chats that run many tool rounds fill the 32,768-token window (the log showed replies ending with `length` and the thinking budget cut to fit the
few thousand tokens left), so we raised the window to 65,536. That shrinks the GPU expert cache by about 12% (2,205 to 1,951 slots) with no
measurable decode cost, because decode here is CPU-bound. Decode stays level out to long contexts (27, 29, 30 and 34 tok/s at 8K, 24K, 48K and
58K tokens on repetitive prompts; reading a 58K prompt takes about 70 seconds).

The larger window also pushed free RAM down. With 40 GiB pinned, a 29K-token prompt left under 1 GiB free and Windows started paging at over 8,000
pages per second: generation stalled with the GPU idle. Lowering the pinned budget to 36 GiB gave about 4 GiB back (lowest free RAM 4.7 GiB with no
page-ins on a 22K-token prompt) at a cost of about 16% decode. Free RAM, not VRAM, is this machine's real limit; the pinned budget should be
set from `free RAM` and `Pages Input/sec`, not from what loads.

### The pinned budget, once the profile is learned

With the mixed profile in place we repeated the budget sweep (agent set and general set, three passes each with the first dropped, then a 22K-token
and a 29K-token prompt read while recording the lowest free RAM and the worst page-in rate; no other heavy program running):

| pinned GiB | agent set | general set | lowest free RAM | page-ins |
|---|---|---|---|---|
| 36 | 32.8 tok/s, 7.3 reads/token | 31.8, 6.2 | 5.3 GiB | none |
| 38 | 32.0, 6.6 | 33.6, 4.0 | 3.6 GiB | none |
| 40 | 32.1, 6.4 | 31.6, 2.1 | 2.7 GiB | none |

Decode speed is flat across the range (the differences are inside the run-to-run noise); only general prompts' file reads keep falling as more is
pinned. The old rule of thumb (+0.7 tok/s per GiB) belongs to the shipped profile: once the right experts are already in RAM, the extra GiB hold
experts that are rarely needed. The free-RAM margin, on the other hand, shrinks by about 1.3 GiB per 2 GiB pinned, and the earlier stall happened
at 40 GiB with a browser and other programs open. We kept 36 GiB.

## Cold start and the warm-up

The first answers after a start run at about half speed: the same prompt ran at 13.9, 18.8, 20.5 and 21.8 tok/s on four requests in a row, the
expert cache hit rate rising from 31% to 58% (the last figures are inflated by repetition; varied prompts settle at 40% to 50%). Across 16
server starts the first benchmark pass ran 6% to 9% slower than the later passes. An opt-in warm-up
(`"warmup": {"enabled": true}`, see the changelog) sends five short typical requests once the model is loaded. It costs 31 seconds and lifted
the first real request from 13.9 to 17.2 tok/s. Because the cache keeps learning for some time, **any benchmark must drop its first pass**; two
of our early sweeps read the cold start as a result.

## Conversations on the NVMe

In a long agent session prompt reading was 27% of the time (459 of 1,708 minutes in our logs; a median of 8 s per request, 30+ s whenever the
conversation was summarised, restarted or switched away from). The engine can already write the conversation it holds to a file and read it back
(`SAVE` / `RESTORE`, the `--slot-save-path` calls); we measured what that is worth, on conversations of random words:

| conversation | re-read after a switch | save | restore | next request after restoring |
|---|---|---|---|---|
| 3K tokens | 8.2 s | 0.42 s, 269 MB | 0.29 s | 0.8 s (26 new tokens read) |
| 12K | 17.8 s | 0.54 s, 400 MB | 0.44 s | 0.8 s |
| 25K | 33.9 s | 0.71 s, 585 MB | 0.60 s | 0.8 s |

A saved conversation costs about 270 MB plus 14 KB per token, and restoring it takes about as long as saving it: well under a second, against 8 to
34 seconds to read the prompt again. So `serve/conversation_store.py` does it by itself (`"conversation_store": {"enabled": true}` in the config):
the live conversation is saved when a request for another conversation arrives and again whenever it has grown by 6,000 tokens (a restart or a crash
loses only the newest part), and a request whose prompt starts with a saved conversation gets it restored first. Conversations are found by the
prompt tokens they were saved with, so any client's chats work; one file is kept per conversation, the oldest go first past 20 GiB, nothing is saved
under 5 GiB of free disk, for a request with pictures or for one that was cancelled, and any failure leaves the request to carry on as if the store
were off.

End to end, with the store on: conversation A (11,936 tokens) read in 18.7 s, conversation B in 21.1 s, then A again with a new question took **1.3 s**
(restore 0.4 s, 26 new tokens read). After a full server restart the same question took **0.7 s**, where it had taken 18.6 s. The first version
skipped the restore after a restart: the warm-up requests leave a short live conversation, and as every prompt starts with the same system prompt a
short live conversation looked like it was being continued. The rule is now to compare matches (restore whenever a saved conversation matches better
than the live one), and the store logs why a lookup missed. The files are the engine's state of your private chats, so keep the folder out of backups
and repositories.

## A faster CPU, 0.1.45 and Swift 1.5 (2026-10-18)

What changed on the machine: the i5-10600K became an **i7-11700K** (8 cores, 16 threads, same board and same four DIMMs), so the link to the
card is PCIe 4.0 x16 under load (`nvidia-smi` shows Gen1 at idle and Gen4 while the model runs; the i5 could not run Gen3 stably). The BIOS reset the
memory to 2133 MT/s after the swap; enabling the profile brought it back to 2666. Engine 0.1.45 was built and run on the same files, then the model
was changed to the Swift 1.5 GSQ-RCO abliterated IQ3_S (same size class, 77.8 GB), with the Basalt draft layer unchanged.

**The suite (`tools/strata_bench.py`, 3 runs per test, medians):**

| | chat decode | 4K prompt | 16K prompt | agent decode | agent first token |
|---|---|---|---|---|---|
| 0.1.43 engine, SC117 model | 33.7 tok/s | 254.7 | 330.1 | 32.9 | 14.59 s |
| 0.1.45 engine, same model | 33.4 | 254.8 | 329.4 | 33.9 | 14.63 s |
| 0.1.45 + `STRATA_PIN_BY_NEED=1` | 33.9 | 254.9 | 330.0 | 33.9 | 14.59 s |
| 0.1.45, **Swift 1.5** (the SC117 profile) | 36.0 | 263.3 | **472.3** | 35.4 | 13.97 s |

- **The engine alone moved nothing** that is outside the noise: 0.1.45 keeps fewer experts in VRAM (hit rate 53% against 59%) and sends fewer over PCIe
  (20% against 30%) for the same speed. `STRATA_PIN_BY_NEED=1` changed nothing either; its author's +4.4% came from a machine that could lock only
  about half its RAM, and the 36 GiB budget here is below that.
- **Prompt reading did not follow the link:** 255 tok/s at 4K and 330 tok/s at 16K, the same on Gen4 as on Gen2, so on this machine it is not limited by
  PCIe. Swift reads a 16K prompt 43% faster (472 tok/s); we did not find out why.
- **The measurement we can compare with the i5:** held-out agent prompts, greedy, the script that produced the profile table in
  [Expert profiles](#expert-profiles): about 40 tok/s at 6.0 file reads per token, against 31.6 tok/s at 7.4 before (a different engine, model and prompt
  count, so a rough comparison; the suite's lower figures are longer contexts).

**Expert profile for Swift** (learned on 125 replayed prompts, then three passes over held-out sets, the first dropped):

| profile | agent | Claude-style | general |
|---|---|---|---|
| the SC117 profile | 40.3 tok/s, 6.0 reads | 40.2, 7.6 | 40.0, 6.1 |
| learned on Swift | 42.2, 5.9 | 40.5, 8.1 | 40.0, 5.5 |

A small gain on agent prompts and a wash elsewhere (inside the 3-10% noise); both profiles' file reads are about the same, so the profile carries over
between two models of one base. We use the Swift one.

**Thinking tokens:** the Swift card claims 35-50% fewer on coding benchmarks. Ten requests (5 short prompts, 2 samples, thinking on): 6,425 tokens for
SC117 and 5,893 for Swift (-8%), medians 465 and 477. One model's own spread was larger than the gap (1,919 and 1,031 tokens for the same prompt), so
this test cannot see a difference under about 15-20%; it did not reproduce the claim on short prompts.

**`--adapt-swaps`, once the link was faster** (how many cache entries may change per adaptation step; the arms took turns and the order was reversed
in round 2; `strata_ab.py --sweep adapt-swaps` and `adapt-swaps-high`):

| `--adapt-swaps` | chat decode (launches) | cache hit | note |
|---|---|---|---|
| 4 | 31.0, 31.6, 33.7 | 38-40% | slower than 8 in 3 of 3 rounds; 4K prompt reading about 22% slower in its one launch |
| 8 (the setting until now) | 31.5, 30.3 and 32.7, 32.2, 35.2 | 42-45% | |
| 16 | 34.4, 35.4, 34.9 and 32.8, 30.2 | 48-50% | two sweeps disagree: +6% and +0.4% |
| 24 | 32.9, 35.5 | 50-53% | +17.6% in the one warm paired round |
| 32 | 33.8, 32.8 | 54% | +4.6% |
| 48 | 34.2, 33.9 | 58-60% | +15.0%, the tightest pair (0.9%) |

Decode rises up to about 24 and is flat between 24 and 48 (about +8-10% over 8); the cache hit rate keeps rising to 48. Two launches per arm and some
cold ones, so "24 to 48 beats 8" holds and "48 beats 24" does not. 48 is the setting.

**Above 48** (`adapt-swaps-top`, 48 as the baseline, chat and an agent session, two launches per arm):

| `--adapt-swaps` | chat decode (launches) | cache hit | against 48, paired per round |
|---|---|---|---|
| 48 | 35.3, 35.25 | 56-57% | the baseline's two launches agree to 0.1% |
| 64 | 34.0, 37.2 | 61-63% | -1.6% in the warm round, +8% in the cold one: a tie |
| 96 | 33.7, 30.9 | 64% | -8.8% |
| 128 | 33.1, 30.7 | 64-68% | -8.1% (-3% and -13% in the two rounds) |

The cache hit rate keeps rising with the setting (57% to 66%) while decode stops following at about 64 and falls beyond it, so more cache changes per step
cost more than the extra hits return. Our reading is that the extra copies over PCIe are the cost; we did not measure that. The curve on this machine:
decode rises from 8 to about 24, is flat to about 64 and falls after that.

**Method notes:** `tools/strata_bench.py` in v0.1.45 crashes with a `NameError` when it has to start the server (the change that added `server_base` removed
the `port` line and three uses remain); fixed here. The built-in summary of a sweep reads agent turns only, so a chat-only sweep comes out empty and has to
be read from the results files. Benchmark runs against a server with the conversation store on fill the store's folder with benchmark conversations;
the benchmark config copy turns it off.

Other machines' reports are not comparable: a Ryzen 5 7600 with DDR5-5200 and the same card is reported at 53 tok/s for short chat on IQ3_S (an older
engine's figures), and a 16 GB 5070 Ti with 80 GiB of DDR4-3200 at 78-80 tok/s on the same Swift file. They differ from this machine in memory size and
speed, VRAM and CPU together, so they do not say how much of the gap more RAM would close; the budget test above (36 against 40 GiB, flat) does not
support assuming much.

## Ideas we looked at and did not do

- **Rewriting the engine in Rust.** The decode step is CPU vector code, GPU kernels and transfers; a rewrite would compile to the same
  instructions, the Python server is not on the hot path, and we would lose the ability to merge upstream. Not worth it.
- **Moving the card to the second slot.** x4 through the chipset: no faster than what we have, and it competes with the NVMe.
- **Keeping conversations in RAM** (`--conversation-cache-mib`). It would cost pinned expert RAM (about 0.7 tok/s per GiB before the profile). We kept
  them on the NVMe instead: see [Conversations on the NVMe](#conversations-on-the-nvme).
- **More VRAM or a second NVIDIA card.** The estimates are in the notes below, not measured here.

Hardware notes, not measurements: a 32 GB card such as a Tesla V100 would hold about five times as many experts (about +50% decode by our
estimate); it needs a CUDA 12 build with `STRATA_EXPERIMENTAL_SM60=ON`, and Volta is being dropped from newer Windows drivers. A newer CPU with
AVX-512 and more cores attacks the CPU-bound step directly.

## Noise and method

- Each setting runs on a fresh server start; three passes of the same prompts; **the first pass is dropped** (cold start); a repeated control
  runs at the end. Draft acceptance and the engine's expert-read counters are recorded with the speed.
- **Run-to-run noise on this machine is 1% to 10%.** Two identical starts differed by 3% in one queue, 9% in another and 10% in a third.
  Gains under about 3% are not findings; file reads per token are far steadier than decode speed and are the figure to trust when they move.
- Decode speed is the engine's own figure (tokens over decode time); the earlier general-prompt numbers elsewhere in this log are wall-clock
  and include prompt reading, so the two kinds are not interchangeable.
- `strata_ab.py --rounds K` now interleaves the arms and `summarize_ab_sweep.py` gives a noise figure and a verdict per arm
  ([what the tools do now](BENCH_NOISE.md#what-the-tools-do-about-it-now-2026-10-16)).
- Arms must not be ordered by the thing under test, and a sweep must not run its first arms cold; see [BENCH_NOISE.md](BENCH_NOISE.md) for the
  analysis of six earlier sweeps (25 arms, 750 same-turn pairs): the same settings repeated came out 4% to 5% apart, and the first 20 minutes of
  a session were slower than the rest.
- Do not benchmark and run other work at the same time, and keep a guard on free RAM and page-ins: a variant that starts paging is not a
  slow variant, it is a broken one.
- Related write-ups: [CACHE_VS_DRAFTS.md](CACHE_VS_DRAFTS.md) (what the expert cache and the drafts are each worth), [SWEEP_ADAPT_KV.md](SWEEP_ADAPT_KV.md)
  (the adapt and KV sweep), [REJECTED_PROPOSALS.md](REJECTED_PROPOSALS.md).
