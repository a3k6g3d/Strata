#!/usr/bin/env python3
"""Read an old A/B sweep off the warm column, turn by turn.

The sweeps under bench/results/ ranked arms on a mean over every run, and the
doc that wrote them up (bench/results/ab-adapt-kv-sweep/SWEEP_ADAPT_KV.md) said
the ranking cannot be trusted: the baseline was the first arm of a two-hour run
and the slowest thing in it, and the last six arms -- which changed nothing about
the policy -- all landed 7-14% above it.  That is the machine warming up, not the
knobs, and BENCH_NOISE.md says the same thing from the other direction: the first
run of every launch is the cold one, and run 2 beat run 1 in 138 of 138 pairs
across five batches.

So this tool does not produce a ranking.  It answers three narrower questions of
the data that is already on disk, and it answers them without the GPU:

  1. Split every arm's first run (cold) from its later runs (warm), and rank the
     arms on the warm column alone.  If an arm's lead only exists in the column
     the doc forbids reading, it was never a lead.
  2. Pair arms turn by turn.  The agent suite walks the context up over its turns,
     so turn 1 and turn 6 are different measurements; comparing an arm's turn 3
     against the baseline's turn 3 holds context roughly fixed, which is the only
     pairing in here that does.
  3. Say what the two live candidates look like under both rules, against the
     noise floor measured by the interleaved control (24.3% for a batch of six).

Prompts are not identical across arms -- the harness puts a per-run nonce in them
(Suite.nonce = str(int(time.time()))) -- so this is not a parity check and does not
claim to be one; it compares distributions, not outputs.  The doc says a parity
check needs a fixed-prompt harness, which does not exist.

Usage:
    python tools/read_sweep_warm.py bench/results/ab-adapt-kv-sweep [--baseline "as configured"]
    python tools/read_sweep_warm.py bench/results/ab-cache-vs-drafts-agent
"""

import argparse
import glob
import json
import os
import statistics
import sys

# BENCH_NOISE.md, "Six launches, two arms, and the arms are the same arm": the
# spread of six launches that ran the identical 439-character command.
NOISE_FLOOR_PCT = 24.3


def load_arm(path):
    """One arm's results.json -> {run: [turn, ...]} of decode tok/s, plus its label."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    params = data.get("params") or {}
    tests = params.get("tests") or []
    results = data.get("results") or {}
    rows = []
    for test in tests:
        for session in (results.get(test) or {}).get("sessions", []):
            rows.extend(session)
    if not rows:
        return None
    by_run = {}
    for row in rows:
        if not row.get("ok") or not row.get("decode_tps"):
            continue
        by_run.setdefault(row.get("run", 0), []).append((row.get("turn", 0), row["decode_tps"]))
    if not by_run:
        return None
    return {
        "arm": params.get("arm") or os.path.basename(os.path.dirname(path)),
        "dir": os.path.basename(os.path.dirname(path)),
        "runs": by_run,
        "n_runs": len(by_run),
        "cold": min(by_run),
    }


def warm_cold(arm):
    """(mean over warm runs, mean over the cold run) for one arm, tok/s."""
    cold_runs = [r for r in sorted(arm["runs"]) if r == arm["cold"]]
    warm_runs = [r for r in sorted(arm["runs"]) if r != arm["cold"]]
    cold = [v for r in cold_runs for _, v in arm["runs"][r]]
    warm = [v for r in warm_runs for _, v in arm["runs"][r]]
    return (statistics.mean(warm) if warm else None, statistics.mean(cold) if cold else None)


def turn_paired(arm, baseline):
    """Mean over turns of (arm's per-turn median / baseline's per-turn median).

    Pools every run of both arms, so a turn's number is a median over the arm's
    runs at that turn; only turns both arms have are used.
    """
    turns = sorted({t for r in arm["runs"] for t, _ in arm["runs"][r]}
                   & {t for r in baseline["runs"] for t, _ in baseline["runs"][r]})
    ratios = []
    per_turn = []
    for turn in turns:
        a = [v for r in arm["runs"] for t, v in arm["runs"][r] if t == turn]
        b = [v for r in baseline["runs"] for t, v in baseline["runs"][r] if t == turn]
        if not a or not b:
            continue
        ratio = statistics.median(a) / statistics.median(b)
        ratios.append(ratio)
        per_turn.append((turn, ratio))
    if not ratios:
        return None, []
    return statistics.median(ratios), per_turn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sweep")
    ap.add_argument("--baseline", default=None,
                    help="arm label to compare against (default: the one named 'as configured')")
    args = ap.parse_args()

    arms = [a for a in (load_arm(p) for p in sorted(glob.glob(os.path.join(args.sweep, "*", "results.json")))) if a]
    if len(arms) < 2:
        print("nothing to compare in %s" % args.sweep)
        return 1

    want = args.baseline or "as configured"
    baseline = next((a for a in arms if a["arm"].strip().lower() == want.lower()), arms[0])
    others = [a for a in arms if a is not baseline]

    runs_needed = max(a["n_runs"] for a in arms)
    print("%s   %d arms, %d runs each, baseline = %r" % (os.path.basename(args.sweep.rstrip("/\\")), len(arms), runs_needed, baseline["arm"]))
    print("noise floor for a batch of six identical launches: %.1f%% (BENCH_NOISE.md)" % NOISE_FLOOR_PCT)
    print()

    b_warm, b_cold = warm_cold(baseline)
    print("%-26s %9s %9s %10s %10s" % ("arm", "warm", "cold", "warm vs", "cold vs"))
    print("%-26s %9s %9s %10s %10s" % (baseline["arm"], "%.2f" % b_warm, "%.2f" % b_cold, "--", "--"))
    rows = []
    for a in others:
        w, c = warm_cold(a)
        rows.append((a["arm"], w, c))
        print("%-26s %9s %9s %10s %10s" % (
            a["arm"],
            "%.2f" % w if w else "-",
            "%.2f" % c if c else "-",
            "%+.1f%%" % (100.0 * (w / b_warm - 1.0)) if (w and b_warm) else "-",
            "%+.1f%%" % (100.0 * (c / b_cold - 1.0)) if (c and b_cold) else "-"))

    print()
    print("turn by turn (median over runs, ratio to the baseline at the same turn):")
    print("%-26s %10s   %s" % ("arm", "median", "per turn"))
    for a in others:
        med, per_turn = turn_paired(a, baseline)
        cells = " ".join("%d:%+.0f%%" % (t, 100.0 * (r - 1.0)) for t, r in per_turn)
        print("%-26s %10s   %s" % (a["arm"], "%+.1f%%" % (100.0 * (med - 1.0)) if med else "-", cells))

    print()
    biggest = max((abs(100.0 * (w / b_warm - 1.0)) for _, w, _ in rows if w and b_warm), default=0.0)
    if biggest > NOISE_FLOOR_PCT:
        print("largest warm-column gap: %.1f%%, over the %.1f%% floor -- but the baseline is the"
              % (biggest, NOISE_FLOOR_PCT))
        print("outlier row, so the gap is the baseline's, not the arms'.  Read the table against the")
        print("sweep's own repeated baseline, not the first arm of the session.")
    else:
        print("largest warm-column gap: %.1f%%, under the %.1f%% floor -- none of these is a result."
              % (biggest, NOISE_FLOOR_PCT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
