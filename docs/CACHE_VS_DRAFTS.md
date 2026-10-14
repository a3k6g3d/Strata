# What the speed on this machine actually comes from (2026-10-14)

Two features make a MoE model fast on a small card: the expert cache, which keeps the experts a token needs in VRAM,
and the drafts (MTP and the suffix drafter), which let several tokens be verified in one pass.  They are usually turned
on together, so nobody knows which one is doing the work.  This run takes them away one at a time.

## The arms, and what each one cost

RTX 5070 12 GiB, Qwen3.8-Flash-Next IQ3_S (35.4 GiB of experts in system RAM), INT8 KV, 65,536-token context,
`--expert-cache auto` (1,951 slots, 3,840 MiB), `--spec 4 --spec-min-p 0.5` with an MTP runtime offering 4 drafts.
Same model, same prompts, same number of runs; one engine argument changed per arm.  Temperature 0, 3 runs each,
medians.  Full reports: `bench/results/ab-cache-vs-drafts/`.

| arm | chat decode tok/s | fresh 4K prompt tok/s | first token | cache hit | over PCIe | drafts accepted |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| as configured | 28.60 | 479.2 | 1.68 s | 0.0% | 6.9% | 68.8% |
| `--expert-cache 0` | 28.10 (-1.7%) | 476.7 (-0.5%) | 1.66 s | 0.0% | 7.6% | 68.1% |
| `--mtp-max-t 1 --suffix-draft 0` | 23.20 (-18.9%) | 457.6 (-4.5%) | 1.71 s | 0.0% | 4.2% | - |
| both off | 21.20 (-25.9%) | 448.9 (-6.3%) | 2.07 s | 0.0% | 8.5% | - |

The run-to-run spread inside one arm was 26.8-32.1 tok/s, so -1.7% is nothing; -18.9% is not noise.  Taking the drafts
away costs almost a fifth of the decode and, on the long prompt, 4.5% of the read.  Taking the cache away changes
nothing that this suite can see.

## Why the cache shows nothing here, and what this does NOT prove

The engine reports a **0.0% expert hit rate in every arm**, including the one where the cache is on.  That is not the
readout being broken - the PCIe share is non-zero and moves between arms, so the cache is being consulted.  It is the
tests: every prompt in the suite is fresh by construction (each carries a unique tag, and the 4K prompts report
`0 reused`), so there is no repeated prefix whose routed experts could already be resident.  A cache that only ever
helps when the same experts come back has nothing to hit, and the arm that removes it loses nothing.

So this measures the value of the expert cache on work that never repeats itself, which on this machine is nil, and it
measures the value of the drafts on that same work, which is 19%.  It says **nothing** about the workload the cache is
built for - the agent session that resends the same conversation turn after turn - because that test was not run here
(`--tests chat,4k`).  The suite has it; run `--tests agent` (or the default suite) before drawing any conclusion about
the cache.

Two secondary readings worth keeping: the drafts-off arm put **fewer** experts over PCIe (4.2% against 6.9%), because
a rejected draft wastes a pass over the experts it routed through; and both-off is worse than drafts-off alone
(-25.9% against -18.9%), so with no drafts the cache still earns something even on fresh prompts.

## The next arm, run: the agent session changes nothing, and says why

The same four arms on `--tests agent` (five turns that each add about 1,500 words of a file and continue from the
model's own earlier replies; 14,759 prompt tokens by the last turn; 3 sessions; `bench/results/ab-cache-vs-drafts-agent/`):

| arm | agent decode tok/s | turn first token | expert hit | over PCIe | drafts accepted | conversation cache hit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| as configured | 25.8 (20.2-31.1) | 8.13 s | 0.0% | 6.4% | 60.4% | 69.2% |
| `--expert-cache 0` | 25.0 (20.8-32.6) | 8.10 s | 0.0% | 6.0% | 60.7% | 69.1% |
| `--mtp-max-t 1 --suffix-draft 0` | 24.6 (19.8-27.2) | 8.12 s | 0.0% | 2.6% | - | 69.2% |
| both off | 25.7 (20.4-28.2) | 8.10 s | 0.0% | 2.6% | - | 69.2% |

Three things came out of this, and only one of them is about speed.

**The conversation cache did its job and the expert cache still found nothing to hit.** The agent session reused 69.2%
of the prompt (3 tokens re-read per session against 73 with the expert cache off), and the expert hit rate is still
0.0% in every arm.  That is not the readout failing - the PCIe share is non-zero and drops from 6.4% to 2.6% when the
drafts go, so the cache is consulted and the numbers move.  It is structural: a turn whose prefix comes back from the
conversation cache never re-routes those tokens at all, so there are no experts to find for them, and the tokens that
are new are new.  Reusing a conversation and reusing experts are two different reuse events, and only the second one
puts anything in this cache.  Any claim that an agent session lifts the expert hit rate has to be measured, not assumed.

**The 0.0% is still worth a look.** Experts are loaded over PCIe in every arm, and never once found already resident -
not across the 64 decoded tokens of a turn, not across five turns of the same session.  With 1,951 slots (3,840 MiB)
against this model's expert count that may simply be too little to retain anything between tokens, but "loads every
time, hits never" is also what a retention or accounting path that never credits a hit would look like.  Check it with
`STRATA_LOOKAHEAD_STATS` and the engine's own hit/miss/evicted/loaded counters before repeating the claim in public.

**Three runs cannot resolve this suite.** The spread inside one arm was 20.2-31.1 tok/s - wider than every difference
between arms.  The drafts, which cost 18.9% on the fresh-prompt suite, cost 4.7% here, and both-off came out ahead of
drafts-off.  On the agent session none of these deltas mean anything; the fresh-prompt result stands because its
spread was far narrower, and that is the one to quote.

## Where the 0.0% comes from: the cache's own admission policy, which is off by default

Reading the engine after that run answered the question the counters were supposed to answer, without waiting for them.

The expert cache hands out slots from **one counter shared by all 48 layers, in the order pairs arrive, and it never
evicts** (`Options::expert_cache`, `src/program/generate.cpp` ~466).  So the first `n_slots` distinct (layer, position)
pairs take every slot - the engine's own comment calls that "about 26 LAYERS OF POSITION 0" - and a hit can then only
ever happen in those layers.  In-tree, on one run's real routing (`Memory/cache_allocation.py`), that policy is measured
at **1,781 of 60,000 = 2.97%** with 256 slots, against **21.4%** at 8 slots per layer and **70.4%** at 64.

The fix is already in the engine and is **off by default**: `--expert-cache-per-layer` (R4.2g, parsed at
`generate.cpp` ~2098, applied at ~5119), which gives each layer a range of its own so no layer can be starved by
another.  Round 328 measured it: the CPU pool drain fell **19.076 -> 10.312 ms/token** at 4,096 per-layer slots, worth
**-2.7 ms/token** end to end, and the engine's help text quotes **54.4% hits** with it.  The same comment says plainly
that what an earlier measurement had called "the GPU hit path saves nothing" was really *"the ADMISSION POLICY gave
every slot to the first position"*.

That is a complete explanation of a 0.0% that does not depend on the workload at all.  A fresh prompt spends the whole
1,951-slot pool on the positions of its own warm-up; a turn whose prefix returns from the **conversation** cache is
never re-routed, so it cannot add to the pool either; and the tokens that are genuinely new arrive too late to get a
slot.  Both suites could report 0.0% forever and the cache would still be doing exactly what it was configured to do.

What is not yet known, and is the reason this is written as a cause rather than as a win: those in-tree numbers are
2.97% and 54.4% at sizes (256 slots, 4,096 per-layer slots) that are **not** this configuration's 1,951 slots on this
model, and the per-layer *sizing* path at ~4943 is gated on `native_pack`, which this pack may or may not be.  The
engine prints the truth on stderr - `R4.2g PER-LAYER: each layer owns N slots (lo..hi)` - and 1,951 over 48 layers is
only ~40 each if they divide evenly, which is between the 21.4% and 70.4% rows and not close to either.

## What to run next: a fifth arm, added

`tools/strata_ab.py` now has a fifth arm, `expert cache, per-layer slots`, which adds that one flag to the baseline and
nothing else.  It tests the cache's own policy instead of the workload, and it is the only arm here that can move the
hit line off 0.0%:

```
py -3.11 tools/strata_ab.py --config <work>/strata.json --tests agent --dry-run   # 5 arms, prints the recipes
py -3.11 tools/strata_ab.py --config <work>/strata.json --tests agent --runs 8 \
    --out bench/results/ab-cache-vs-drafts-agent-perlayer --yes
```

Read the arm in this order, because the speed is not the first thing it can tell us:

1. the engine's `R4.2g PER-LAYER: each layer owns N slots` line - if it is missing, the flag did not take and the arm
   is measuring the baseline again;
2. the hit line.  Anything above 0.0% means the 0.0% was the policy, and the whole cache-vs-drafts question has to be
   re-asked with the cache actually retaining experts;
3. only then the tok/s, and only from `--runs 8` or more: this suite's within-arm spread (20.2-31.1 tok/s) is wider
   than every difference it produced at three runs.

If the hit rate stays at 0.0% with per-layer slots on, then the policy was not the cause, and the counters
(`STRATA_LOOKAHEAD_STATS`, the engine's hit/miss/evicted/loaded lines) become the next thing to run rather than a
sixth arm.

