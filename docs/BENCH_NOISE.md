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

## The first run that measured it, rather than reading it back

On 2026-10-20 the harness ran the same arm three times on this machine - three launches, `--runs 2 --tests agent
--config strata.json`, the live configuration, engine 0.1.45 at commit 181fe260, about two minutes a launch, driven
by `tools/noise_floor.bat` because the driver cannot be launched by the thing it kills. It is the first run on disk
with machine state recorded and the first to write `runs.jsonl` (0 lines before it, 3 now).

| launch | median decode | utilisation mean | clock band | max temp | background work seen |
|---|---|---|---|---|---|
| 1 | 38.55 | 72.3% | 2542-2925 MHz | 50 °C | `searchindexer.exe` busy in 97 of 120 s of samples |
| 2 | 32.1 | 54.8% | 2550-2910 MHz | 51 °C | `searchindexer.exe` in 20 |
| 3 | 34.3 | 65.9% | 2550-2910 MHz | 54 °C | none |

Twelve NVML samples per launch, 10 s apart, plus one at the start and one at the end; pstate P1 throughout and no
throttle reason in any sample of any launch. The machine state on disk is what item 4 asked for. What it says is
uncomfortable:

* **The spread between the three launch medians is 20.1%, and it is exactly the number this document says not to
  read.** With `--runs 2` a launch's median is half its cold run, so the three medians are not three warm samples of
  the same thing; they are three cold-warm mixtures. Nothing here contradicts the reading above.
* **The machine-state data does not order the launches.** The fastest launch is the one with the most background
  work, the slowest is the one with the least busy GPU, and the clocks, temperatures and pstates of all three sit
  within a whisker of each other. There was nothing to explain - the spread is the floor - but a reader who hoped
  the sampler would separate a cold launch from a real one should know that on its first real run it found no
  difference to separate them with.
* **Within each launch, run 2 beat run 1 in 14 of 18 same-turn pairs** - the within-launch warm-up, the right
  direction and the size `check_cold_axis.py` predicts (+8.8%). The pairs themselves are the floor measured rather
  than computed: **18 pairs, median 13.6%, p90 26.4%, worst 32.7%**. They belong beside the 12.1%/29.3% row above
  (all runs, warm-up included), which they agree with, and not beside the warm-only 10.1% - a two-run launch has
  one cold run and one warm one, so every pair here is cold against warm and no warm-only pair can be formed.
* **At turn 0 the first run was faster in all three launches** (by 24.4%, 25.5% and 15.3%), which is the opposite
  direction to the old turn-0 control (+5.5% for the second run). Three pairs prove nothing, and it is written down
  because it is the predicted direction failing on the first data collected to check it.

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
   **Done 2026-10-19:** `tools/bench_state.py` takes the snapshots — uptime, free RAM, GPU clock, **utilisation**
   (the field this item names; NVML answers it here, `nvmlDeviceGetUtilizationRates`), temperature, power, pstate,
   throttle reasons, and any watched background process that took a core — at the start of a run, every 10 s on a
   thread through the run, and at the end; the run's `results.json` and its `runs.jsonl` line carry them. They are
   read through NVML in-process, not by starting `nvidia-smi`, because polling `nvidia-smi` costs about 1 tok/s of
   decode and that is exactly the disturbance being measured. The README now has a section for them instead of
   saying they were not recorded, and a field the machine could not read says so rather than going quiet — a
   missing clock looks exactly like an idle one. What has **not** happened is a real run: none of the 89
   `results.json` on disk has state, so this has never been rendered from measured data, only from the selftest's
   fabricated snapshot. The cold region of this session is still read from an arm's position, as it was when the
   section above was written; the next sweep is the first that can read it off the record.
   **First real data 2026-10-20**, three launches of one arm, in the section above: the sampler works on a live
   run, and on that run it found no difference in clock, temperature, pstate or throttling between launches that
   came out 20.1% apart — which is the floor talking, not the machine state, and the sampler's job is to say so
   rather than to be blamed for it. The three launches are the only `results.json` on disk with state; the older
   89 still have none, so a sweep cannot read its cold region off the record until it is re-run.
5. **Keep the output text, or a hash of it.** The current record holds no generated text and no output
   fingerprint: `digest()` ends at `drafts_accepted`, and `engine-log-excerpt.txt` holds 5 lines with nothing
   resembling a fingerprint in them. So nothing in `bench/results/` can show that two arms produced the same
   answer, which is the one thing a speed comparison is supposed to be checked against. The older harness in
   `bench/results/2026-10-04-community-rtx-4090-laptop/` recorded both `text` and `request_sha256` per request;
   the current one records neither.
   **Done 2026-10-19:** `digest()` records `output_sha256` per request now, and `tools/output_fingerprint.py`
   fingerprints the same fixed prompts the sweeps send, over runs, and says whether the machine reproduced itself.
   It did not: five runs of the same prompt at temperature 0 (greedy) with the same seed gave 2, 5 and 4 distinct
   answers on the three prompts — while a short exact-answer prompt was identical five times out of five in an
   earlier probe. The per-request fingerprints are in `bench/results/2026-10-19-output-fingerprint/`. A speed
   comparison can now be checked against the answer it produced, on prompts where the answer is reproducible at
   all; on the others there is nothing to check, and the tool says so.
   **Gap found by the first real run 2026-10-20:** the three `runs.jsonl` lines it wrote have `state_hash: null`.
   Two reasons, both fixed. The engine only prints a `STATE_HASH` line when `STRATA_STATE_HASH=1` is in its
   environment, and the harness never set it — so `start_server` now starts its server with it (the server passes
   its own environment straight on to the engine, `serve/server.py` `child_env`, so one setting is enough). And the
   recorder was reading the harness's temp log, which never has one of those lines — the engine writes to the log
   the config names, all 459 of them on this machine — so the recorder now reads that log, from the byte it was at
   when the server started, so a run only counts what the engine printed during it. One honest limit stays: the
   fingerprint is filled when the harness starts the server itself; a run against a server already running gets
   nothing, because that server was started without the variable.
   **Verified on real data 2026-10-20:** five launches of the baseline arm, agent suite, two runs each, in
   `bench/results/state-hash-check/` — the first runs on this machine with both halves of the record filled.
   Every line has a fingerprint now, 25/25/26/25/25 lines and five distinct digests, and the arithmetic agrees:
   the engine's log held 459 fingerprint lines before these runs and holds 585 now, exactly 126 more, which is
   what the five runs claim between them — so the recorder is reading this run's lines and not old ones.
   The launch medians (38.25, 35.8, 35.5, 35.05, 36.7) sit 9.1% apart, and the first launch is again the fastest,
   the same direction the three-launch run reported. `tools/state_hash_check.py` is the script that runs it; it
   re-launches itself out of the process tree it has to kill, because on this machine the tool runner that
   launches it hangs *under* the server, and stopping the server was taking the driver down with it.


## Eight launches, and the two effects point opposite ways

`tools/check_cold_axis.py` now reads both batches (its reader wanted two directory levels under the root;
a single-session run writes one, so it was silently reading zero arms and printing empty tables). Over the
eight launches on disk -- three, then five, same commit, same config, same arm -- the within-launch number
is **48 pairs, median +13.4%, 90th percentile 32.7%**, and the later run is faster in **39 of the 48**. That
is the hand-computed 12.1% / 29.3% again from the tool that computes it, and the direction the doc predicts.

At the launch level the sign flips. In both batches the **first launch is the fastest**: +16.1% over the rest
of its batch in the three-launch one, +7.3% in the five-launch one. So the cold thing that happens at the
start of a launch makes that launch the fastest of its batch, while inside it the first run is the slowest --
the two effects run in opposite directions, and which one wins at the launch level is not something the
three-launch run could see.

The turn-0 check from the previous section, which came out backwards on three launches, is now **4 of 8** in
the predicted direction (run 2 faster than run 1 at turn 0) — all four that agree come from the five-launch
batch, and the four that do not are that batch's remaining launch plus all three of the three-launch one.
Eight launches is still not a result; it is
a coin flip with the sign of each one visible.

The tool's across-launch table only takes the five-launch batch: it wants at least four arms in a session,
and gives r = -0.43 for position against decode -- later launch, slower. The three-launch batch is below its
floor, so that correlation is one number from one batch, not a finding.


## Fourteen launches, and the machine state on disk says nothing again

The driver ran six more launches of the same arm on `c2a7dbd6` - the same config, the same suite, two runs each,
about three and a half minutes a launch. That is the third batch on disk (three launches, then five, then six), so
the within-launch number can now be computed on **84 pairs**: median **+13.9%**, 90th percentile 32.8%, later run
faster in **65 of 84**. Per batch it is +7.3% (18 pairs), +16.7% (30) and +15.7% (36) - the new batch lands on the
same number as the five-launch one, and the whole thing sits where the hand-computed 12.1% said it would before
any of this was measured. The within-launch drift is the one effect here that has now survived three batches.

At the launch level the new batch is the one that breaks the pattern, which is the interesting part:

| batch | commit | launches | first launch vs the rest of its batch | first launch's rank |
|---|---|---|---|---|
| 1 | 181fe260 | 3 | +16.1% | 1st of 3 |
| 2 | 1cfe412b | 5 | +7.3% | 1st of 5 |
| 3 | c2a7dbd6 | 6 | -3.6% | 5th of 6 |

Two of three batches lead with their first launch; the third does not, and its first launch is the second slowest
of its six. The plan for this run said that if the new batch's first launch led again - three of three - the
cold-start rule would need rethinking rather than more data. It did not lead, so the rule is not dead, but it is
now 2 of 3 with the third batch going the other way, which is what a coin flip looks like at n=3. What the rule
was written for - the first 20 minutes of a session behaving differently - still has exactly one batch of real
data behind it (the harness's own cold-region line on the merged folder: first 20 min median 32.10, after
35.78, +11.5%), and that batch is the three-launch one, so the cold region and the batch are the same data.

The harness's own noise floor, computed on the merged folder with the cold launches left out, is now **12.7% over
6 launches** - the predicted 12-13%, measured. So the number the doc has been quoting from the 3-run arms is now
confirmed by launches whose only difference is which one ran when.

The turn-0 check is **6 of 14** in the predicted direction (the new batch is 2 of 6: 0/3, then 4/5, then 2/6).
Fourteen launches is still not a result and I am not going to write it as one.

**Machine state, nine rows now (six with fingerprints).** The new batch's six rows are the first machine state
written by the fixed code on a run that was meant to produce it. Correlating each state column against the
launch's own median decode over the eleven launches that have a state hash, nothing is strong enough to name:
max temperature is the best of a bad lot at r = +0.41, background-busy samples r = -0.28 (the wrong way), free
RAM at the end r = +0.14, clock r = +0.16, power r = +0.08, uptime r = -0.15. The fastest launch of the new
batch (38.30) is the one with the most background work in its samples (103 busy samples against 0 for most of
the rest) and one of the lowest clock maxima (2910 MHz) - the state columns point the wrong way for the fastest
launch, which is the sampler telling me again that the spread is not in them.

The state-hash fix works on real data at scale: 11 of 11 launches recorded by the fixed code have a fingerprint,
24-26 lines each, every digest distinct. The three launches from before the fix are still the only nulls.

What I would do with this, in the same order as the list above: the within-launch drift is real and item 2 stands
on it. Item 1 - interleave the arms or place them by clock - is the one this run was supposed to settle, and it
came back 2 of 3, so it is still open and the honest move is to stop collecting batches of this shape (they all
look like this one) and run the interleaved version instead: same arm, arms placed in a different order in each
repetition, so the position effect shows up as disagreement between repetitions instead of as a result.
