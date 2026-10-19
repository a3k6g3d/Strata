# What other projects do with the same problem, and what is worth taking

Research notes from 2026-10-13/14, prompted by llama.cpp merging its own expert cache and by a video asking whether
Strata is still needed now that it has.  Nothing here is a claim that Strata is better: the numbers below are the other
projects' own, on their own machines, and where a difference matters the reason is spelled out.

## llama.cpp's expert cache (`--moe-cache-mib`), merged 2026-10-07

PR #29887 (am17an, merged by ggerganov) adds a GPU cache for MoE experts that live in host memory: the `MUL_MAT_ID`
ops of a routed expert are run **on the GPU**, and only the experts missing from the cache are uploaded.  It is a port
of the `qvac-fabric-llm.cpp` cache.  Details that matter when comparing it with ours:

- It is an LRU of expert slots sized in MiB (`--moe-cache-mib N`); the author's advice is to start near 10% of the
  model's total expert size.
- It applies to small batches only (32 tokens or fewer); larger batches take the regular offload path.
- Layers with different expert layouts get separate banks.
- The hit rate is logged at info level, visible with `-lv 4`.
- **A miss is copied over PCIe and computed on the GPU.** Strata keeps the experts that miss in system RAM and computes
  them there, which is why our sizing rule is not theirs (see "A sizing rule of thumb from the other engine does not
  carry over" in [DETAILS.md](DETAILS.md)).

Their figures, Qwen3.8-Flash-Next Q4_0 (93.7 GiB, 65.4 GiB of experts), EPYC 7742 with 16 threads on the GPU's NUMA
node, PCIe 4.0 x16, SPEED-Bench qualitative, 512-token outputs, temperature 0, **no MTP**:

| GPU | config | experts in VRAM (layers + cache) | decode t/s | speed-up | hit rate |
| --- | --- | --- | ---: | ---: | ---: |
| RTX 4090 | `--fit` (default) | 16.5 + 0.0 GB | 25.0 | 1.00x | - |
| RTX 4090 | `--fit --moe-cache-mib 6544` | 10.4 + 6.4 GB | 39.4 | 1.57x | 72% |
| RTX 4090 | `-cmoe --moe-cache-mib 11000` | 0.0 + 10.7 GB | 40.7 | 1.62x | 77% |
| RTX 5090 | `--fit` | 24.5 + 0.0 GB | 30.8 | 1.00x | - |
| RTX 5090 | `--fit --moe-cache-mib 6544` | 17.9 + 6.4 GB | 54.5 | 1.77x | 77% |
| RTX 5090 | `-cmoe --moe-cache-mib 19000` | 0.0 + 18.6 GB | 67.8 | 2.20x | 89% |

PR #30112 (merged 2026-10-08) extends it over two GPUs with a layer split: on 2x RTX 4090 the same model decodes
34.78 t/s on the default and 60.38 with a 6,544 MiB cache (1.74x, hit rate 85.9%), while prompt processing goes the
other way, 440.7 to 402.0 t/s (0.91x).  So the cache buys decode and pays for it in the read.

A third-party test on Windows/HIP (Radeon AI PRO R9700 32 GB, the same model IQ1_M, 24 GB of experts) reports 11.0
t/s with no cache, 14.5 with 8,000 MiB (1.3x, 33% of experts cached) and 27.6 with 16,000 MiB (2.5x, 67%), with prompt
processing unaffected.  That reviewer also read the diff and flagged something about our own code worth knowing: their
misses are uploaded with `ggml_backend_tensor_set_async` straight from the mmapped (pageable) source, whereas Strata
copies through a pinned staging buffer - on HIP a pageable source is staged or registered per copy.  They did not
measure whether it costs anything there.

### The one number in that thread worth acting on

The same reviewer reports hit rate **by batch size**: 85.0% and 94.6% for decode batches of 8 tokens or fewer, but
about **18%** once the batch is above 8, at both cache sizes.  Expert reuse dies as the batch grows, because a batch of
many tokens routes to many more distinct experts than one token does.  If our own numbers show the same cliff, then
`--batch` above a handful of tokens quietly costs the cache most of its value, and that is worth checking against
`STRATA_LOOKAHEAD_STATS` and the per-request hit rate rather than assumed.

#### Checked: the mechanism is ours too, but our window never reaches the cliff

That check turned out to be a read of the code, not a run.  The mechanism is ours - `expert_pool_dispatch_multi` counts
the window's entries as `n = n_tok * k`, k the experts each token routes (10 here), and groups the distinct ones
(`src/core/expert_source.cpp:3666`), so a wider window really does mean more distinct experts and the same dilution.
What differs is how wide a window may be:

- `Verifier::init` refuses a window outside 2..8 tokens (`src/core/verify.cpp:560`), bounded by `kVerifyMaxT = 8`
  (`include/strata/kernels/verify_kernels.hpp:21`) and `cpu::MAXT = 8` (`include/strata/kernels/cpu/expert.hpp:139`).
- A batch window - `--batch-mtp`, several concurrent slots decoded as one window - is bounded by the same constant:
  `batch_rows_error` rejects `S > max_t` (`include/strata/core/batch_rows.hpp:16`) and `max_t_` is that same 8.

So `n_tok` never passes 8, whether the tokens are one request's drafts or the rows of several slots, and `n` never
passes 80.  Strata sits permanently in the regime llama.cpp reports at 85-95%, and no flag moves it into the other
regime: there is no `--batch` value that costs the cache its hit rate, because the window is capped at the exact number
where their cliff starts.  The cliff is not absent from this engine; it is behind a wall.

Two things follow that are worth having written down.

**The 128-entry guard is currently unreachable.**  `kMaxWindowEntries = 128` carries `static_assert(MAXT * 10 <= 128)`
(`src/core/expert_source.cpp:3603`), so the runtime refusal of a larger window (`:3641`, "a verify window routes more
entries than the expert pool's window tables hold") cannot fire while `MAXT` is 8: a full window is 80 entries, 48 short
of the cap.  The comment there says such a window is "refused at run time", which reads like a live path; it is a guard
for a `MAXT` nobody has raised yet.  The `static_assert` is what keeps this note true: raise `MAXT` past 12 and the
build fails rather than the cliff arriving quietly, so their cliff only becomes ours when someone raises both numbers on
purpose - and then this question has to be measured again instead of read.  `tests/core/batch_rows_test.cpp` holds the
other half, that a batch window cannot have more rows than the window holds.

**The video's comparison is not on this axis.**  llama.cpp's batch is its server's concurrent decode batch, which grows
past 8 as requests arrive; ours is one window of at most 8 tokens.  "They drop to 18% at large batches and we do not"
would be a difference only if we could run a large batch, and we cannot.

## LMCache

LMCache (12.1k stars, PyTorch Foundation, Apache-2.0) is a KV cache layer beside the engine, not inside it: a
standalone daemon that moves KV out of GPU memory into a hierarchy of CPU RAM, local storage and remote backends, and
reuses it across requests, sessions and engine instances.  It plugs into vLLM (and NVIDIA Dynamo); its own claims are
lower TTFT on long-context, agentic and RAG traffic, KV surviving an engine crash, and a 10x MoE improvement from its
2026-04 multiprocess architecture.  It also does P2P CPU-memory sharing between machines (2026-01) and per-request,
per-token and per-user cache metrics.

What is genuinely interesting for us, and what is not:

- **Reuse across processes.** Their KV lives outside the engine, so a restart does not lose it and two engines can share
  one document's KV.  Strata's `SAVE` keeps conversations, and the pinned-prefix work
  ([RESEARCH_RUNS.md](RESEARCH_RUNS.md)) keeps a document's checkpoint, but a server restart still re-reads the
  document.  A KV/checkpoint store that survives a restart is the idea worth stealing.
- **Chunk-level, content-addressed reuse.** They key blocks by content, so two unrelated chats that share a document
  share blocks.  Strata matches the longest common start per session; content-addressed blocks would help exactly the
  case agent clients create (the same file resent by different chats).
- **Their metrics are per-request and per-token, and they are a product surface.** Our per-request hit rate, PCIe share
  and draft acceptance (engine 0.1.39/0.1.43 onward, now also in the bench report) are the same kind of instrument;
  keeping them comparable is worth the effort.
- **Not worth borrowing:** the whole layer.  It exists because a datacenter engine cannot address system RAM well and
  because several engines must share one cache.  Strata already owns its RAM path, its expert tier and its planner, and
  a Python daemon beside the engine would sit on top of work we have already done in C++.

## "Unsloth has unlimited context now"

It is not unlimited, and it is not a rival to Strata.  What changed is the model: Qwen3-Next-style architectures mix
linear attention (Gated DeltaNet) with a little real attention, so the K/V grows far more slowly with context than a
transformer's does; the models are also very sparse (hundreds of experts, a few billion active) and ship native
multi-token prediction.  Unsloth's own contribution on top is the *Dynamic* GGUF quantizations, which shrink the
weights so that more of that context fits on a small card.  Their Studio runs llama.cpp underneath ("speeds should be
mostly the same", their own FAQ).

So the honest reading of "long context on a small GPU" is: **how much context fits is set by the quant size and the
attention type**, and the expert cache's hit rate is the same trade paid in a different currency - both spend VRAM that
the K/V also wants.  The sizing note in [DETAILS.md](DETAILS.md) (read the hit rate beside tok/s, never on its own) is
exactly this warning; a hybrid-attention model makes it sharper, not obsolete.

The interesting consequence for us is the opposite of a threat: an 80B-total, few-billion-active, 512-expert model is
the most expert-cache-shaped thing there is, and native MTP is precisely the axis the
[cache-vs-drafts measurement](CACHE_VS_DRAFTS.md) separates.  These models are our best demo, provided the quant fits.

## Three papers that do the prefetch half of Strata properly

All three are 2025-2026 work on the same problem from the academic side, and all three reach the same conclusion the
engine reached: the way to pay for a data-dependent expert load is to predict it a step ahead and overlap the transfer.
What they add is three levers Strata does not have.

- **Pre-Attention Expert Prediction** (arXiv:2511.10676, Nov 2025, ETH): predicts a layer's routing from the
  activation *before that same layer's attention*, with two linear functions and a ranking-aware loss, because "some
  functions in LLMs are ranking-preserving" - matching the *order* of the selected experts is enough, and a linear
  map can do it.  93.03% on DeepSeek V2 Lite, 94.69% on Qwen3-30B, 97.62% on Phi-mini-MoE, about 15 points above the
  previous best, and it covers the first layer, which the previous-best method cannot.
- **MoE-SpeQ** (arXiv:2511.14102, Nov 2025): a small on-device draft model predicts the *sequence* of experts the
  next tokens will need, and a runtime orchestrator prefetches them so the transfer sits behind useful computation.
  An adaptive governor, guided by an "Amortization Roofline Model", tunes the speculation strategy to the hardware.
  Up to 2.34x over a state-of-the-art offloading framework, on Phi-MoE, on edge devices.
- **SPICE** (arXiv:2608.21240, Aug 2026, ASP-DAC 2027): a lightweight draft model plus a *confidence-aware* adaptive
  lookahead that prefetches the high-confidence predictions, and on a miss decides what to do per case - a
  low-confidence miss is approximated by the resident shared expert with low-rank (LoRE) surrogates, and the exact
  residual work goes to the CPU, asynchronously, in parallel with the GPU.  Up to 3.12x on time-per-output-token
  with minimal quality loss, on DeepSeek V2 Lite and Qwen2-57B-A14B.

### What Strata already does, and where these papers say we are short

`RouterLookahead` (`include/strata/core/expert_source.hpp:240`) applies layer l+1's router to layer l's MoE input -
the residual stream changes little in one layer - takes each token's top `k` (default 10), drops the experts the GPU
cache or the RAM copy holds, and asks the source to warm the rest.  So Strata is squarely in the family those papers
call the weak one: the *exact* router applied to a *stale* input, one layer behind, with the first layer unpredictable
because it has no predecessor to read.  The paper's claim is that a trained head on the same layer's pre-attention
activation buys about 15 points of accuracy over exactly that.  Strata has no training pipeline and will not grow one
for a prefetch hint, so the part of their result that transfers without one is the *loss*: score by rank, not by value.
The vote-ranked ladder in `STRATA_LOOKAHEAD_STATS` (top 4 / 8 / 12, `kVoteN = 16`) is already that estimator, sitting
beside the top-k / k+4 / k+10 one in the same log line - so the question "should the window be filled by per-token
top-k or by the vote across the window" is answerable from counters the engine already writes, and if the vote column
wins on the PCIe-cover column then `k` is the thing to move.

**The lever worth building is the confidence gate.**  `warm()` is unconditional today: every predicted expert that no
tier holds gets its pages asked for.  On this machine that is not free - the whole drive tops out near 2.5 GB/s and a
single stream at 2.0 ([BENCH_NOISE.md](BENCH_NOISE.md), "The disk is at its ceiling"), so a wrong guess spends I/O a
real miss will want, and the file tier is already on the fast path (`NO_BUFFERING | OVERLAPPED`) with nothing left to
win there.  SPICE's answer is to prefetch only what the predictor is confident about; a rank-margin gate (warm the
experts whose score clears the k-th by some margin) costs one comparison and is measurable against the PCIe-cover
column that already exists.  MoE-SpeQ's governor says the same thing about `k` and depth: both are static
environment knobs here (`STRATA_IO_PREFETCH_DEPTH` 1..4, default 1, and the predictor's `k`), and the paper's position
is that they should track the measured balance rather than be set once.  `tools/replay_cache_policy.py` already
replays a `--dump-routing` trace through the swap policy offline, which is the harness for a (k, depth, threshold)
sweep that touches no engine code.

**Not worth borrowing:** SPICE's low-rank surrogates.  Approximating a missed expert with a resident surrogate changes
the answer, and this engine's invariant is that a prediction only ever *warms* - which experts are computed, and how,
is never changed by a guess.  Their other half, sending exact residual work to the CPU beside the GPU, is what Strata
already is.

## So, do you still need Strata?

For the one case the video compares - a big MoE on one card, decoding short fresh prompts - llama.cpp's expert cache
now does a similar job and reports 1.57x-2.20x on a 4090 and a 5090, with no MTP.  What it does not do, as of these
two PRs: compute the missed experts anywhere but on the GPU, size itself against free VRAM, keep the K/V out of the way
of the cache, prefetch by router look-ahead, or handle the parallel-layer-experts (PLE) models Strata exists for.  And
our own measurement on a 12 GiB card found that on fresh-prompt work the drafts, not the cache, carried 19% of the
decode ([CACHE_VS_DRAFTS.md](CACHE_VS_DRAFTS.md)) - a result that argues for measuring both together, which is what
both projects now report.
