# Benchmark noise: is the machine hiding something?

Two runs with the same settings and the same model came out 4.6% apart (28.16 and 26.88 tok/s median
decode). The question is whether that gap is a difference we failed to notice in the configuration, or just
the machine. It is the machine - but not in the vague way "noise" suggests. The machine drifts on a
schedule, and two of the sweeps in `bench/results/` read that schedule as an engine result.

Nothing was re-run for this. It is all read back out of what is already on disk: 25 arms with agent data
across 6 sweeps (`bench/results/*/*/results.json`, written by `tools/strata_bench.py`), 510 measured turns,
750 pairs of runs inside the same arm at the same turn number.

## The short answer: no hidden configuration

Every arm records the engine's full launch line, in `machine.launch`. Across the 25 live arms there are 16
distinct launch lines. Seven of those arms share one launch line, byte for byte, on the same commit on the
same day:

| median decode | arm | sweep | commit |
|---|---|---|---|
| 16.45 | as configured | ab-adapt-kv-sweep | 9edaac19 |
| 23.0 | as configured | ab-agent-perlayer | d7cb238f |
| 24.5 | as configured (repeat) | ab-confirm-kv-vram2 | f7cda3a0 |
| 24.85 | as configured | ab-cache-vs-drafts-agent | 5062b6ed |
| 25.7 | as configured | ab-confirm-kv-vram | f7cda3a0 |
| 25.9 | as configured (repeat) | ab-adapt-kv-sweep | 9edaac19 |
| 26.9 | as configured | ab-confirm-kv-vram | f7cda3a0 |

That is a 63.5% spread with nothing to account for it: the same executable, the same flags, the same model,
the same prompts, the same temperature. The two arms at the extremes (16.45 and 25.9) are the first and last
arm of one sweep, 90 minutes apart, same commit.

The locked invariants are identical in all of them - `--mtp mtp-rt-basalt`, `--spec 4`, `--spec-min-p 0.5`,
`--resident-budget-gib 36`, `--expert-profile`, `--kv int8`, temperature 0 - and so is what the engine's own
probes reported back (`machine.engine_info`: `pool_workers 5`, `pcie_frac 0.20`, `expert_slots 1951`,
`kv_resident 0`). The auto probes did not vary. Nothing in the record differs.

## What the machine is doing instead: it warms up, and the sweeps read that as a result

Order the arms inside each sweep by their own `results.phases[].from_s` timestamps and the pattern is plain.
`ab-adapt-kv-sweep`, 13 arms, 79 minutes of benching, one commit:

| arm | minutes into the sweep | median decode | hit rate |
|---|---|---|---|
| as configured | 0 | **16.45** | 51.9% |
| adapt-swaps 0 | 10 | **23.4** | 24.9% |
| adapt-swaps 4 | 18 | **24.15** | 40.6% |
| adapt-swaps 16 | 26 | 26.85 | 56.9% |
| adapt-decay 0.65 | 35 | 26.45 | 47.6% |
| adapt-decay 0.80 | 43 | 24.5 | 47.1% |
| kv-resident 20480 | 51 | 26.25 | 51.1% |
| kv-resident 24576 | 56 | 27.65 | 51.5% |
| kv-resident 32768 | 61 | 27.55 | 47.9% |
| vram-reserve 512 | 66 | 26.55 | 48.6% |
| vram-reserve 600 | 74 | 26.9 | 47.9% |
| vram-reserve 768 | 82 | 26.15 | 46.9% |
| as configured (repeat) | 91 | 25.9 | 48.0% |

Position in the sweep predicts speed: correlation +0.40 (n = 21 arms across sweeps). Restrict it to arms
started more than 20 minutes in and the correlation collapses to -0.14 (n = 12): the drift is not a slow
climb all session, it is a **cold region for the first ~20 minutes**, after which the machine is flat.
Everything from `adapt-swaps 16` onwards sits in 24.5-27.65; the first three arms do not.

Two consequences, and they are not comfort readings:

- **`--adapt-swaps 0` (23.4) and `--adapt-swaps 4` (24.15) were the 2nd and 3rd arms of the session.** Both
  of the arms that looked slow enough to be a finding are the two arms that were still inside the cold
  region. The one `--adapt-swaps` arm started past the cold region, `16` (26.85), is the fastest of the four
  and indistinguishable from the baseline's 25.9. Read the other way round: the sweep's headline
  "`--adapt-swaps 0` costs 13% of decode" is the machine warming up, not the flag.
- **The hit-rate story is confounded the same way**: position correlates with hit rate at +0.46 (n = 21).
  `--adapt-swaps 0` really does report 24.9% and `--adapt-swaps 16` 56.9%, so the mechanism is live, but part
  of that ladder is the same cold-start gradient, and the arms cannot be ranked against each other by these
  numbers alone.

The cold region also has a smaller twin inside every arm. The first run of an arm sits **12.0% below the median of
that arm's own later runs, in 23 of 25 arms** (pooled: 24.20 against 27.02). And the first turn of a run sits 15.7%
below the median of the later turns of the same run, over 85 runs. The harness warms up before it measures, and still
keeps that run: `bench_record.digest()` takes a median over the turns of **every** run in the launch, so the cold run's
turns sit inside the figure an arm is ranked by, and `--runs 3` dilutes it to about a third of 12% rather than dropping
it. (Numbering: this paragraph counts runs from 1; `tools/check_cold_axis.py` and the section below count them from 0,
so its "run 0" is this paragraph's "run 1".)

## The noise floor, measured

Comparing two runs of the same arm at the same turn number, where the prompt, the flags and the model are all
identical by construction:

| arms | pairs | median | 90th pct | worst | pairs >30% apart |
|---|---|---|---|---|---|
| 3-run arms (23 of them) | 414 | 12.1% | 29.3% | 71.0% | 9% |
| 8-run arms (2 of them) | 336 | 20.3% | 57.0% | 110.1% | 31% |
| all | 750 | 14.3% | 42.1% | 110.1% | 19% |

The 8-run arms are the honest measurement of the spread, because 3 runs only produce 3 pairs per turn and
under-sample the tail: with 8 runs the same machine shows a median of 20.3% between two of its own runs. A
4.6% gap between two arm medians is not a finding; neither is a 10-point gap between a baseline and its own
repeat.

The machine can also be tight, which is what makes the cold region worth naming: three swept configurations
were run twice, in different sweeps, on different days, across different commits, and agreed to **0.4%
(`--adapt-decay 0.65`), 0.7% (`--kv-resident 24576`) and 1.5% (`--vram-reserve-mib 512`)**. All three pairs
sat past the cold region in both sweeps. Reproducible to 1.5% is possible; the sweeps that reported 5-13%
differences were comparing arms that had not been given the same chance to warm up.

## What does carry the speed

Within one arm, turn by turn, subtracting each arm's own mean so only the turn differs (n = 510):

| against decode tok/s | correlation |
|---|---|
| draft tokens accepted in that turn | **+0.44** |
| tokens generated in that turn | +0.31 |
| prompt tokens in that turn | +0.29 |
| tokens read from the cache | +0.27 |
| prompt tokens reused from cache | +0.26 |
| draft tokens offered | +0.17 |
| seconds to first token | +0.07 |

Across arms (n = 25, medians): time to first token **-0.86**, PCIe share **-0.60**, prompt reuse -0.33,
draft acceptance +0.15, **cache hit rate +0.11**.

Draft acceptance is the strongest single thing that moves decode speed, at +0.44 within an arm where the flags
are identical - the same conclusion `docs/CACHE_VS_DRAFTS.md` reached, now with a coefficient on it. Cache hit
rate is nearly nothing: +0.11 across arms, +0.15 within a sweep, and it climbs with wall-clock position
almost as hard as speed does. The PCIe figure (-0.60) is largely the cold region again: the arms with the
largest PCIe share are the early ones.

## Where the warm-up actually sits: each launch, not the boot

Everything above says "the session's first ~20 minutes", which is what an arm's position in the sweep suggested. It is the
wrong axis, and `tools/check_cold_axis.py` says so from the records already on disk - 25 arms, 510 turns, 360 run-vs-run
pairs of the *same turn* inside one launch:

| run N against run 0 of the same turn | n | median |
|---|---|---|
| run 1 | 150 | +8.8% |
| run 2 | 150 | +13.9% |
| runs 3-7 (the two 8-run arms) | 300 | +23.5% to +39.8% |

That is as large as the drift between arms, and it is the machine rather than the prompt cache - three controls:

* **turn 0 gains +5.5%** (n=60). Turn 0 is the first prompt of a conversation; it has nothing to reuse in any run, so the
  cache cannot explain its second run being faster.
* **a base run that had already reused over half its prompt tokens still gains +13.0%** (n=240), against +15.4% for a cold
  base (n=120). The cache was already doing its job in the slower sample.
* **`prompt_tps`, the quantity the cache actually moves, gains only +2.2%** (n=360). If the gain were cache warming, this
  is the number that would move.

Each arm starts its own server and reloads a 12 GiB model, and the first thing it does afterwards is slow. The session
looks warm only because its first arm was also the coldest launch. Two consequences:

* **The cold sample is run 0 of every launch**, not the first arm of a session. `summarize_ab_sweep.py` now reports
  `decode_warm` next to `decode` - the same median with that run removed - and drops it wherever a launch ran more than
  once. It matters between arms: the effect reaches +30% inside an 8-run arm against +14% inside a 3-run arm, so pooling
  all runs makes an 8-run arm and a 3-run arm incomparable.
* **The noise floor no longer needs a run of its own.** Every arm already repeats the same turns, so warm runs of one turn
  against each other *are* the machine's resolution: **10.1% at the median, 27.0% at the p90, 51.9% worst**, over 78 pairs
  in `ab-adapt-kv-sweep`. (The 12.1%/29.3% figures above came from all runs, warm-up included.) `--noise DIR` is still
  accepted, and still the better figure when it exists, but a sweep with repeats now judges itself.

Applied to the sweep that started all this, the verdict is flat: **all twelve arms sit inside that 10.1%**, from
`kv-resident 32768` at +6.5% down to `adapt-swaps 16` at -4.7%. No flag in that sweep moves decode, and the ranking of
those numbers is noise read as order.

## The disk is at its ceiling, and it is a lower ceiling than the label says

A monitor screenshot taken mid-prefill read **1.58 GB/s** of disk reads against a 21,588-token prompt, which looked
like slow I/O. It is not slow I/O, and the paging alternative the number suggests has already been taken.

The tile is `psutil.disk_io_counters()`, summed over every disk: system-wide **physical** reads, so bytes that
genuinely came off the media, not pagefile traffic and not a software rate (`serve/telemetry.py:367-372`, rendered at
`serve/web/app.js:75`). The model sits on `J:`, which is disk 5, a Sandisk Optimus 5100 500 GB (a WD SN740 OEM part).
Measured on it with the engine idle:

| pattern on J: | result |
|---|---|
| sequential 8 MiB, one stream | **2.00 GB/s** |
| sequential 4 MiB, two streams | 2.65 GB/s |
| four / eight streams | 2.49 / 2.56 GB/s |
| random 4 KiB, one at a time | 5,884 IOPS = **23 MB/s** |

So the whole-drive ceiling is ~2.5 GB/s and one stream gets 2.0. The 1.58 GB/s in the screenshot is **79% of a single
stream and 63% of the whole drive**, with at most a quarter left in it - and the spec sheet's 4.9 GB/s is not what this
500 GB OEM part does. The engine's own file tier is already the fast path, not mmap: `FILE_FLAG_NO_BUFFERING |
FILE_FLAG_OVERLAPPED` at `src/core/expert_source.cpp:1596` (Linux gets `O_DIRECT` at :1612, one `io_submit` per batch),
and the console says which side of the memory budget it is on - "the file tier reads unbuffered (49.1 GiB available,
40.0 GiB of it still to be taken by the RAM copy, 6.8 GiB of experts read from the files: the file cache cannot keep
them)". With a 36 GiB resident budget on a 64 GB box the OS cache cannot hold the model, so leaning on it would trade
2,000 MB/s for the 23 MB/s fault rate above.

Pagefile contention, which is what "shared with C:" raises, measured at 2%: 4 KiB random traffic (6,330 IOPS, the shape
of page-fault traffic) running against a 1 GiB sequential read **on the same disk** cost 2.02 against 2.06 GB/s. The
NVMe has enough queue depth that pagefile-shaped traffic barely dents a big sequential stream, so paging is not a
speed problem here - it is a RAM-headroom problem, and it is set by the resident budget rather than by pagefile size.

Changed anyway, on 2026-10-17, for space and commit reasons: `J:\pagefile.sys` **96 GB fixed -> 48 GB fixed**,
`C:\pagefile.sys` left at 2 GB (it is what `CrashDumpEnabled = 3`, a small dump, requires on the boot volume). Before:
pagefile use on disk was 407 MB + 946 MB, peak 1,096 MB - about 90x less than the allocation - and
`Memory\Page Reads/sec` during a live generation was 7-239/s, under 1 MB/s, while Strata held 56 GB of pagefile-backed
*commit* (a reservation, not I/O) split across both pagefiles. The commit limit goes 161.8 GB -> **113.8 GB** against
73.2 GB committed, leaving **40.6 GB** of headroom, which is the number that matters: exceeding the commit limit is an
allocation failure rather than a slowdown, so a bigger `--resident-budget-gib` than 36 eats that headroom. 48 GB frees
48 GB on the model disk and keeps paging on the fast one. Needs a reboot; until then the 98,304 MB allocation stays
live. `J:\Strata-work\set-pagefile.ps1` and `pagefile-change.log` hold the change and its before/after.

The consequence for reading benchmarks: a disk-backed path on this machine cannot go past ~2.0-2.6 GB/s, so decode
speed on a model whose experts do not fit is bounded by **bytes pulled per token**, not by how the reads are issued.
The engine's own line - "expert cache ~50% hit (+5-6% of the routed experts over PCIe)" - is the number to attack. It
agrees with `CACHE_VS_DRAFTS.md:97-102`, which found only 9.1% of routed experts crossing the bus against 61.0%
already resident: most of what the cache misses comes from the copy of the model in system RAM, so the disk is a
smaller share of the traffic than the monitor tile makes it look.

## What to do about it

In the order I would do them, cheapest first. None of these need a re-run to write down; all of them need a
re-run to be worth anything.

1. **Interleave the arms, or place them by clock, not by list order.** This is the bug, not the noise. Every
   conclusion in `SWEEP_ADAPT_KV.md` and `CACHE_VS_DRAFTS.md` compares arms that were run at different points
   of a session whose first 20 minutes behave differently. Either repeat every arm at several positions and
   average, or discard every arm started inside the cold region. A sweep that runs its baseline first and last
   (which the harness already does) can detect this; it cannot correct for it.
2. **Discard the first run of each launch, or report it separately.** It is 12% below its own arm's later runs in 23
   of 25 arms, and it is pooled into the median every arm is ranked by, which biases every arm down by roughly 4% at
   `--runs 3` - the size of the differences being argued about. `summarize_ab_sweep.py` now prints that figure
   alongside the pooled one (`decode_warm`); reading the sweep off that column instead costs nothing and needs no
   re-run.
3. **Compare arms turn by turn, not median to median.** The same turn number in two arms faces the same
   prompt; pairing cancels the draft-acceptance luck that dominates the within-arm spread. The data is already
   there - `results.agent.sessions[run][turn]` is indexed by turn - so this is a summariser change, not a
   re-run.
4. **Record the machine during the run.** The README states plainly that background workloads and power limits
   are not recorded. One GPU clock and utilisation sample per arm would have separated the cold region from a
   real result without any statistics at all.
5. **Keep the output text, or a hash of it.** The current record holds no generated text and no output
   fingerprint: `digest()` ends at `drafts_accepted`, and `engine-log-excerpt.txt` holds 5 lines with nothing
   resembling a fingerprint in them. So nothing in `bench/results/` can show that two arms produced the same
   answer, which is the one thing a speed comparison is supposed to be checked against. The older harness in
   `bench/results/2026-10-04-community-rtx-4090-laptop/` recorded both `text` and `request_sha256` per request;
   the current one records neither.
