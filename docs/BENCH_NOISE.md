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

The cold region also has a smaller twin inside every arm. Run 1 of an arm sits **12.0% below the median of
that arm's own later runs, in 23 of 25 arms** (pooled: run 1 median 24.20, runs 2+ median 27.02). And turn 1
of a run sits 15.7% below the median of the later turns of the same run, over 85 runs. The harness warms up
before it measures, but it still keeps run 1 in the median of three.

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

## What to do about it

In the order I would do them, cheapest first. None of these need a re-run to write down; all of them need a
re-run to be worth anything.

1. **Interleave the arms, or place them by clock, not by list order.** This is the bug, not the noise. Every
   conclusion in `SWEEP_ADAPT_KV.md` and `CACHE_VS_DRAFTS.md` compares arms that were run at different points
   of a session whose first 20 minutes behave differently. Either repeat every arm at several positions and
   average, or discard every arm started inside the cold region. A sweep that runs its baseline first and last
   (which the harness already does) can detect this; it cannot correct for it.
2. **Discard run 1, or report it separately.** Run 1 is 12% below its own arm's later runs in 23 of 25 arms.
   Averaging it in biases every arm down by roughly 4%, which is the size of the differences being argued
   about.
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
   **Done 2026-10-19:** `digest()` records `output_sha256` per request now, and `tools/output_fingerprint.py`
   fingerprints the same fixed prompts the sweeps send, over runs, and says whether the machine reproduced itself.
   It did not: five runs of the same prompt at temperature 0 (greedy) with the same seed gave 2, 5 and 4 distinct
   answers on the three prompts — while a short exact-answer prompt was identical five times out of five in an
   earlier probe. The per-request fingerprints are in `bench/results/2026-10-19-output-fingerprint/`. A speed
   comparison can now be checked against the answer it produced, on prompts where the answer is reproducible at
   all; on the others there is nothing to check, and the tool says so.

## What the tools do about it now (2026-10-16)

Items 1 to 4 above, and a way to wait for a run, are in the tools; item 5 is too now (see the note under it).

| | |
|---|---|
| 1. interleaving | `tools/strata_ab.py --rounds K` launches every arm K times, the arms taking turns, and reverses the order every second round (A B C, C B A, A B C ...) so a drift that is linear in time gives every arm the same average position. `--runs` is the total per arm, split over the launches. `tools/summarize_ab_sweep.py` compares each launch with the reference arm's launch of the same round, so slow drift cancels instead of being read as a result. |
| 2. run 1 and the cold region | a launch that started inside the first 20 minutes of the sweep is marked `*`, shown, and left out of the median when others remain; the reference arm's own noise figure leaves it out too. |
| 3. turn by turn | already in the summarizer; now per round. |
| 4. the machine during the run | `tools/bench_state.py`: uptime, GPU clocks, temperature, power, throttling and memory read through NVML in the benchmark's own process (polling `nvidia-smi` costs about 1 tok/s of decode, which is the kind of disturbance this is meant to catch), free RAM, and any watched background process (Defender, Windows Update, the indexer, and so on) that was busy. One snapshot at the start and the end and a sample every 10 s in between go into `results.json` under `state`. |
| a noise figure | `tools/summarize_ab_sweep.py` prints a noise floor and a verdict per arm: "above noise" when the median difference is larger than the spread between the reference arm's own warm launches, or than a measured noise floor (`strata_ab.py --noise-floor 3` launches the baseline alone three times; `summarize_ab_sweep.py DIR --noise bench/results/noise-floor` drops the first, cold launch and uses the spread of the rest). |
| one record per run | `tools/strata_bench.py` appends one JSON line per run to `runs.jsonl` next to the result folders (`tools/bench_record.py`): the arm, the launch line, when it started, how long the PC had been up, the machine state, decode speed (the client's and the engine's own), time to first token, expert-cache, PCIe, prompt-reuse and draft figures, every turn's decode speed, and a digest of the `STATE_HASH` lines when the log excerpt has them. `summarize_ab_sweep.py runs.jsonl` reads it in place of the folders. |
| waiting | `tools/wait_for.py --file run.log --pattern "ALL DONE" [--pid N] [--timeout S]` blocks until the pattern is written, the process ends or the time is up, then prints the tail. One call instead of a poll loop. |

Item 5 is done (2026-10-19), see the note under it. A hash of generated tokens still cannot be compared across arms on most
of the sweep prompts: two launches of the same configuration generated different token counts at temperature 0
(`REJECTED_PROPOSALS.md`, item 6), so the fingerprints never line up - which is why `output_fingerprint.py` compares only the
prompts an arm reproduces on its own and reports the rest as not comparable. The parity
gate `tools/conversation_cache_parity.py` is a different thing: an A/B/A test that loads its own engines one after another in one process and compares
the engine's `STATE_HASH` lines for its own scenarios; it neither needs nor replaces a sweep-wide gate.

A full sweep with three rounds is 39 launches for the 13 arms of `adapt-kv`; use `--noise-floor 3` first (three launches) to see whether any difference
you are looking for is even resolvable.
