# Tuning log: what we measured on a 12 GB card

Everything we tried while getting Strata's IQ3_S build of Qwen3.8-Flash-Next to run as fast as one mid-range PC allows, with the numbers,
the ones that did not hold up, and the method. Dates are 2026-10 unless stated. It is a log of one machine, not a benchmark suite:
read the numbers as "what this box did", and see [Noise](#noise-and-method) before trusting any difference under about 10%.

**The machine:** RTX 5070 12 GB on PCIe 2.0 x16 (about 7 GB/s measured), Intel Core i5-10600K (6 cores, AVX2, no AVX-512), 64 GB DDR4-2666
(four DIMMs), a SATA-class budget NVMe for the model files, Windows 11. The model is the IQ3_S GSQ-RCO build (two GGUF shards, about 78 GiB,
experts about 53 GiB) with its MTP draft layer, run through Strata 0.1.42, then 0.1.43 and 0.1.44.1.

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

## Cold start and the warm-up

The first answers after a start run at about half speed: the same prompt ran at 13.9, 18.8, 20.5 and 21.8 tok/s on four requests in a row, the
expert cache hit rate rising from 31% to 58% (the last figures are inflated by repetition; varied prompts settle at 40% to 50%). Across 16
server starts the first benchmark pass ran 6% to 9% slower than the later passes. An opt-in warm-up
(`"warmup": {"enabled": true}`, see the changelog) sends five short typical requests once the model is loaded. It costs 31 seconds and lifted
the first real request from 13.9 to 17.2 tok/s. Because the cache keeps learning for some time, **any benchmark must drop its first pass**; two
of our early sweeps read the cold start as a result.

## Ideas we looked at and did not do

- **Rewriting the engine in Rust.** The decode step is CPU vector code, GPU kernels and transfers; a rewrite would compile to the same
  instructions, the Python server is not on the hot path, and we would lose the ability to merge upstream. Not worth it.
- **Moving the card to the second slot.** x4 through the chipset: no faster than what we have, and it competes with the NVMe.
- **A cache of conversation state on the NVMe.** Parked conversations live in RAM only and the option is off here (it would cost pinned RAM).
  Prompt reading was 27% of agent time in our logs, mostly after the conversation was summarised or restarted. The engine can already write a
  session to disk (`--slot-save-path`); wiring it to save and restore automatically is the open idea.
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
- Arms must not be ordered by the thing under test, and a sweep must not run its first arms cold; see [BENCH_NOISE.md](BENCH_NOISE.md) for the
  analysis of six earlier sweeps (25 arms, 750 same-turn pairs): the same settings repeated came out 4% to 5% apart, and the first 20 minutes of
  a session were slower than the rest.
- Do not benchmark and run other work at the same time, and keep a guard on free RAM and page-ins: a variant that starts paging is not a
  slow variant, it is a broken one.
- Related write-ups: [CACHE_VS_DRAFTS.md](CACHE_VS_DRAFTS.md) (what the expert cache and the drafts are each worth), [SWEEP_ADAPT_KV.md](SWEEP_ADAPT_KV.md)
  (the adapt and KV sweep), [REJECTED_PROPOSALS.md](REJECTED_PROPOSALS.md).
