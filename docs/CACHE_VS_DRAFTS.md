# What the speed on this machine actually comes from (2026-10-14)

Two features make a MoE model fast on a small card: the expert cache, which keeps the experts a token needs in VRAM,
and the drafts (MTP and the suffix drafter), which let several tokens be verified in one pass.  They are usually turned
on together, so nobody knows which one is doing the work.  This run takes them away one at a time.

## The arms, and what each one cost

RTX 5070 12 GiB, Qwen3.8-Flash-Next IQ3_S (35.4 GiB of experts in system RAM), INT8 KV, 65,536-token context,
`--expert-cache auto` (1,951 slots, 3,840 MiB), `--spec 4 --spec-min-p 0.5` with an MTP runtime offering 4 drafts.
Same model, same prompts, same number of runs; one engine argument changed per arm.  Temperature 0, 3 runs each,
medians.  Full reports: `bench/results/ab-cache-vs-drafts/`.

| arm | chat decode tok/s | fresh 4K prompt tok/s | first token | expert hit | over PCIe | drafts accepted |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| as configured | 28.60 | 479.2 | 1.68 s | 40.6% | 6.6% | 68.8% |
| `--expert-cache 0` | 28.10 (-1.7%) | 476.7 (-0.5%) | 1.66 s | 41.1% | 7.4% | 68.1% |
| `--mtp-max-t 1 --suffix-draft 0` | 23.20 (-18.9%) | 457.6 (-4.5%) | 1.71 s | 48.7% | 3.5% | - |
| both off | 21.20 (-25.9%) | 448.9 (-6.3%) | 2.07 s | 51.0% | 7.8% | - |

The speed columns are the client's own measurements and stand.  The two cache columns were **recomputed by hand from the
`results.json` after this report was first written** - as run, `strata_bench.py` printed 0.0% in the hit column of every
arm, for a reason given at the end of this file, and the PCIe column was the only one of the two that had ever been read
from the right place.  Read both cache columns with the caveat in "the hit column is not a clean on/off signal".

The run-to-run spread inside one arm was 26.8-32.1 tok/s, so -1.7% is nothing; -18.9% is not noise.  Taking the drafts
away costs almost a fifth of the decode and, on the long prompt, 4.5% of the read.  Taking the cache away changes
nothing that this suite can see.

## Why the cache shows nothing here, and what this does NOT prove

*(This section was written reading the 0.0% in the table above at face value.  The 0.0% turned out to be a reporting bug,
so the reasoning is kept as what was believed and why it was wrong; the recomputed table says what actually happened.
The conclusion about the drafts is untouched - it never came from the hit column.)*

The engine appeared to report a **0.0% expert hit rate in every arm**, including the one where the cache is on.  The
reason it looked like a finding rather than a glitch was that the PCIe share beside it was non-zero and moved between
arms, so the cache was evidently being consulted.  It was the readout: `engine_attribution()` summed a key the server
never sends (`prompt_cache_reused_tokens`, where the records write `reused`), so the sum was always 0, and it divided
that by prompt tokens - which is the **conversation** cache's reuse rate and not the expert cache's hit rate at all,
whatever the sentence printed next to it claimed.  Both halves were wrong in the same direction.  The selftest did not
catch it because the `/metrics` it fabricates had invented the same wrong key, so the fake agreed with the buggy code
instead of with the server.

What the recomputed figures say: the cache hits plenty on this machine (40.6% of its lookups on the chat suite, 61.0% on
the agent session), and removing it still costs only 1.7% - the same conclusion as before, reached the honest way.  The
suite's prompts are fresh by construction (each carries a unique tag, and the 4K prompts report `0 reused`), so what the
cache is hitting is the experts the same run routes to twice - across the tokens of one decode, and across the runs of
one arm - not a repeated prompt.

So this measures the value of the expert cache on work that never repeats its prompt, and finds it small (1.7%), and it
measures the value of the drafts on that same work, which is 19%.  It says **nothing** about the workload the cache is
built for - the agent session that resends the same conversation turn after turn - because that test was not run here
(`--tests chat,4k`).  The suite has it; run `--tests agent` (or the default suite) before drawing any conclusion about
the cache.

**The hit column is not a clean on/off signal.** The two arms that run with `--expert-cache 0` report 41.1% and 51.0%,
*higher* than the arms with the cache on, which cannot be right in the way the column is labelled: the engine's own
`req_look` is `hits + admitted + refused` (`src/program/generate.cpp` ~12187), so a cache with no slots should have
nothing to hit.  Either the record's `hit_rate` counts a resident-tier lookup that is not this cache, or
`--expert-cache 0` does not mean what the arm's label says.  Until that is settled against the engine's own `DONE` line
and `STRATA_LOOKAHEAD_STATS`, the hit column may be read **within** one arm as "how much of the traffic the top tier
served", and must not be compared across arms as the value of the feature.  The PCIe column and the speed columns do not
have this problem.

Two secondary readings worth keeping: the drafts-off arm put **fewer** experts over PCIe (4.2% against 6.9%), because
a rejected draft wastes a pass over the experts it routed through; and both-off is worse than drafts-off alone
(-25.9% against -18.9%), so with no drafts the cache still earns something even on fresh prompts.

## The next arm, run: the agent session changes nothing, and says why

The same four arms on `--tests agent` (five turns that each add about 1,500 words of a file and continue from the
model's own earlier replies; 14,759 prompt tokens by the last turn; 3 sessions; `bench/results/ab-cache-vs-drafts-agent/`):

| arm | agent decode tok/s | turn first token | expert hit | over PCIe | drafts accepted | conversation cache hit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| as configured | 25.8 (20.2-31.1) | 8.13 s | 61.0% | 6.4% | 60.4% | 69.2% |
| `--expert-cache 0` | 25.0 (20.8-32.6) | 8.10 s | - | 6.0% | 60.7% | 69.1% |
| `--mtp-max-t 1 --suffix-draft 0` | 24.6 (19.8-27.2) | 8.12 s | - | 2.6% | - | 69.2% |
| both off | 25.7 (20.4-28.2) | 8.10 s | - | 2.6% | - | 69.2% |

(As run, this suite printed 0.0% in the hit column for all four arms and 69.2% under the conversation-cache heading,
which is the same reporting bug read the other way round: the figure that belonged to the conversation cache was the
only one the tool could compute, and the real expert hit rate - 61.0% for the baseline arm - sat unused in the same
records.  The three arms whose hit is now `-` are the ones whose recomputed figure is caught by the caveat above; a dash
is what the column deserves there, not the 0.0% that was printed.)

Three things came out of this, and only one of them is about speed.

**The conversation cache and the expert cache are two different reuse events, and both of them happened.** A turn reused
69.2% of its prompt (3 tokens re-read per session against 73 with the expert cache off), and the expert cache found
61.0% of what it looked for already in VRAM.  Those are separate ledgers - reusing a conversation does not re-route its
tokens, so it cannot put an expert in the cache - and the 61.0% is earned by the tokens that *are* re-routed: the decode
of the turn, and the repeats of the session.  The earlier version of this section claimed the opposite ("the expert
cache still found nothing to hit") because it was reading the bug rather than the records.

**The 9% PCIe share is the number to read, not the hit rate.** Only 9.1% of the routed experts come over the bus while
61.0% are already resident, which means most of what the cache does not serve is coming from the copy of the model in
system RAM - a 36 GiB resident budget against a 35.4 GiB expert stack, so nearly the whole model is already home.  That
is the correct reading of these figures and it is not a complaint; it is exactly what a cache smaller than the model is
for.  But it bounds the cache's marginal value on this box: the path it owns is 9% of the traffic, which is precisely
why taking it away costs 1.7%.

**Three runs cannot resolve this suite.** The spread inside one arm was 20.2-31.1 tok/s - wider than every difference
between arms.  The drafts, which cost 18.9% on the fresh-prompt suite, cost 4.7% here, and both-off came out ahead of
drafts-off.  On the agent session none of these deltas mean anything; the fresh-prompt result stands because its
spread was far narrower, and that is the one to quote.

## The admission policy: true, and no longer the explanation of anything

*(This section was written as the cause of the 0.0%.  The 0.0% was a bug, so there is nothing left to explain - but the
policy itself is a fact about the engine, taken from the source and from in-tree measurements rather than from these
runs, and it is worth keeping for that reason.  Read it as background, not as the resolution of the tables above.)*

The expert cache hands out slots from **one counter shared by all 48 layers, in the order pairs arrive, and it never
evicts** (`Options::expert_cache`, `src/program/generate.cpp` ~466).  So the first `n_slots` distinct (layer, position)
pairs take every slot - the engine's own comment calls that "about 26 LAYERS OF POSITION 0" - and a hit can then only
ever happen in those layers.  In-tree, on one run's real routing (`Memory/cache_allocation.py`), that policy is measured
at **1,781 of 60,000 = 2.97%** with 256 slots, against **21.4%** at 8 slots per layer and **70.4%** at 64.

That in-tree measurement is about a **small** pool.  At this configuration's 1,951 slots the shared pool evidently stops
being the limit, since the baseline arm reaches 61.0% of its lookups with the default policy.  The two statements sit
together without contradiction, and the 1,951-slot case had never been measured until this run.

The alternative policy is already in the engine and is **off by default**: `--expert-cache-per-layer` (R4.2g, parsed at
`generate.cpp` ~2098, applied at ~5119), which gives each layer a range of its own so no layer can be starved by
another.  Round 328 measured it: the CPU pool drain fell **19.076 -> 10.312 ms/token** at 4,096 per-layer slots, worth
**-2.7 ms/token** end to end, and the engine's help text quotes **54.4% hits** with it.  The same comment says plainly
that what an earlier measurement had called "the GPU hit path saves nothing" was really *"the ADMISSION POLICY gave
every slot to the first position"*.

What is not known is the part that matters here: those in-tree numbers are 2.97% and 54.4% at sizes (256 slots, 4,096
per-layer slots) that are **not** this configuration's 1,951 slots on this model, and the per-layer *sizing* path at
~4943 is gated on `native_pack`, which this pack may or may not be.  The engine prints the truth on stderr - `R4.2g
PER-LAYER: each layer owns N slots (lo..hi)` - and 1,951 over 48 layers is only ~40 each if they divide evenly, which is
between the 21.4% and 70.4% rows and close to neither.  A baseline already at 61.0% leaves little room for it to win at
that size, and per-layer splitting can also **lose**: 40 slots per layer cannot hold what 1,951 shared slots hold when
the traffic is concentrated on a few layers.

## What to run next: a fifth arm, added

`tools/strata_ab.py` has a fifth arm, `expert cache, per-layer slots`, which adds that one flag to the baseline and
nothing else.  It was added to move a 0.0% that has since turned out to be a bug, so the question it answers is now the
narrower one: **at 1,951 slots, does giving each layer its own range beat a shared pool that already hits 61.0%?**  That
is still worth knowing in both directions - if per-layer wins, the default policy is leaving something on the table at
ordinary sizes too; if it loses, the shared pool is doing the right thing at this size and the in-tree 2.97% should stop
being quoted as though it applied here.

```
py -3.11 tools/strata_ab.py --config <work>/strata.json --tests agent --dry-run   # 5 arms, prints the recipes
py -3.11 tools/strata_ab.py --config <work>/strata.json --tests agent --runs 8 \
    --out bench/results/ab-cache-vs-drafts-agent-perlayer --yes
# or the two arms that matter, in one model load each:
#   run_ab_offline.ps1 -armName "as configured;expert cache, per-layer slots" -runs 8 \
#       -outOverride bench\results\ab-agent-perlayer
```

Read the arm in this order, because the speed is not the first thing it can tell us:

1. the engine's `R4.2g PER-LAYER: each layer owns N slots` line - if it is missing, the flag did not take and the arm is
   measuring the baseline again.  (The first attempt at this arm never got that far: `strata_ab.py` passed the flag as
   `--engine-arg --expert-cache-per-layer`, and argparse read the value as the *next option*, so the arm died on its
   argument line while the other four - whose values start with a letter - ran happily.  It now passes
   `--engine-arg=VALUE`, which always takes the value as written.)
2. the hit line and the PCIe line together, from the fixed bench, as a **difference from the baseline's 61.0%** - and
   holding the caveat about cross-arm hit comparisons in mind until the `--expert-cache 0` arms are explained.
3. only then the tok/s, and only from `--runs 8` or more: this suite's within-arm spread (20.2-31.1 tok/s) is wider than
   every difference it produced at three runs.

If the hit rate stays at 61.0% with per-layer slots on, then the shared pool was not the limit at this size, and the
counters (`STRATA_LOOKAHEAD_STATS`, the engine's hit/miss/evicted/loaded lines) become the next thing to run rather than
a sixth arm.

## The reporting bug this run found (fixed)

`tools/strata_bench.py` reported the expert cache's hit rate as `reused / prompt_tokens` summed over the server's request
records - under a key the server never writes (`prompt_cache_reused_tokens`, where the records say `reused`), and under a
sentence that described a different quantity from the one it computed.  Every arm of every run this tool has ever made
therefore printed **0.0%**, and the README it writes said "the expert cache found 0.0% of the routed experts already in
VRAM" - the kind of sentence that gets repeated in public.  The real figure was there the whole time in the same records,
in the field the engine itself writes (`hit_rate`; `serve/server.py` ~4039, hits over lookups).

Now fixed: the hit column is the engine's own `hit_rate`; the conversation cache's reuse is its own column
(`prompt_reuse_pct`) computed from the exact cumulative counters; the README says plainly that the cache columns do not
add up to 100 and why (the rest came from the resident copy in system RAM); and a record that reports neither figure is
skipped instead of counted as a zero, so an engine too old to report its cache no longer has a hit rate invented for it.
The selftest's fabricated `/metrics` now uses the keys the server actually writes - the part that had let the bug live,
because the fake shape had been written from the code rather than from the server, so the two could agree while both were
wrong.

What this did **not** fix is the open question the run leaves behind: arms running `--expert-cache 0` report a 41-51%
hit rate, higher than the arms with the cache on.  Until that is reconciled with the engine's own `DONE` line, the hit
column is trustworthy inside one arm and not across arms.

