"""Is the slow start tied to the machine's boot, or to each server start?

The sweep tooling has to know which run to throw away.  summarize_ab_sweep.py currently drops the first LAUNCH of a
session (COLD_MIN, minutes since the session began), and bench_state samples uptime - both assume the warm-up belongs
to the machine's session.  But every arm starts its own server and reloads a 12 GiB model, which takes minutes; if the
curve resets at each launch, then the cold run is each launch's first RUN, not the session's first ARM, and the noise
figure is being computed from the wrong samples.

This answers it from data already on disk, with nothing re-run.  Two comparisons, on the same records:

  within a launch   the same turn of the same arm, run 1 and run 2 against run 0 of that same arm
  across launches   each arm's median against its position in the session

If the within-launch ratios sit at 1.0 while the across-launch medians climb, the warm-up is the machine's and the
current rule is right.  If the within-launch ratios climb too, every launch pays its own warm-up and the rule has to
become per-launch.

    python tools/check_cold_axis.py [bench/results ...]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys


def turns_of(record) -> list[dict]:
    """The measured turns of an A/B arm.  Only the dict-shaped records qualify: the flat list-shaped ones
    (an older tool's per-request log) carry no run/turn numbering, and run-vs-run is the whole question."""
    if not isinstance(record, dict):
        return []
    results = record.get("results") or {}
    if not isinstance(results, dict):
        return []
    out = []
    for session in (results.get("agent", {}) or {}).get("sessions", []) or []:
        for t in session:
            if isinstance(t, dict) and t.get("ok") and t.get("decode_tps") and "run" in t and "turn" in t:
                out.append(t)
    return out


def start_epoch(record) -> float | None:
    """When this arm's benching began: the engine's counters were read at the start of the run."""
    if not isinstance(record, dict):
        return None
    v = ((record.get("machine") or {}).get("totals") or {}).get("since")
    return float(v) if v else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="*", default=["bench/results"])
    args = ap.parse_args()

    paths = []
    for root in args.roots:
        # recursive on purpose: a sweep puts each arm two levels down (root/sweep/arm), a single-session
        # run puts it one (root/arm), and the reader should not care which of the two it is handed.
        paths += sorted(set(glob.glob(os.path.join(root, "**", "results.json"), recursive=True)))
    arms = []
    for p in paths:
        try:
            rec = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        ts = turns_of(rec)
        t0 = start_epoch(rec)
        if len(ts) < 6 or t0 is None:
            continue
        commit = (rec.get("machine") or {}).get("strata_commit")
        arms.append({"path": p, "turns": ts, "start": t0, "commit": commit,
                     "launch": (rec.get("machine") or {}).get("launch", "")})

    # One sweep, one session, one commit: only here can arm position mean anything.
    groups: dict[tuple, list] = {}
    for a in arms:
        root = os.path.dirname(os.path.dirname(a["path"]))
        groups.setdefault((root, a["commit"]), []).append(a)

    print(f"{len(arms)} arms read from {len(args.roots) or 1} root(s)\n")

    # ---- within a launch: run index against the same arm's own run 0, same turn number
    by_run: dict[int, list[float]] = {}
    for a in arms:
        per: dict[tuple[int, int], float] = {}
        for t in a["turns"]:
            per[(int(t.get("run", 0)), int(t.get("turn", 0)))] = float(t["decode_tps"])
        for (run, turn), v in per.items():
            base = per.get((0, turn))
            if base and run > 0:
                by_run.setdefault(run, []).append(100.0 * (v / base - 1.0))
    print("within one launch - the SAME turn, run N against run 0 of the same arm (0 = no effect):")
    for run in sorted(by_run):
        vs = sorted(by_run[run])
        if len(vs) < 5:
            continue
        med = statistics.median(vs)
        print(f"   run {run}: n={len(vs):4d}  median {med:+6.1f}%  "
              f"interquartile {vs[len(vs)//4]:+.1f}..{vs[3*len(vs)//4]:+.1f}  "
              f"spread {vs[0]:+.1f}..{vs[-1]:+.1f}")

    # ---- across launches: the arm's own median against where it sat in its session
    print("\nacross launches - the arm's median decode against minutes since its session's first arm:")
    for (root, commit), g in sorted(groups.items()):
        if len(g) < 4:
            continue
        first = min(a["start"] for a in g)
        pts = [((a["start"] - first) / 60.0, statistics.median([float(t["decode_tps"]) for t in a["turns"]]))
               for a in g]
        pts.sort()
        span = pts[-1][0] - pts[0][0]
        if span < 10:
            continue
        # split at 20 minutes: the boundary summarize_ab_sweep.py currently uses
        cold = [v for m, v in pts if m < 20.0]
        warm = [v for m, v in pts if m >= 20.0]
        line = "  ".join(f"{m:5.1f}m:{v:5.2f}" for m, v in pts[:14])
        print(f"\n   {os.path.basename(root)} @ {commit}  {len(pts)} arms, {span:.0f} min")
        print(f"     {line}")
        if cold and warm:
            print(f"     first 20 min: n={len(cold)} median {statistics.median(cold):.2f}   "
                  f"after: n={len(warm)} median {statistics.median(warm):.2f}   "
                  f"({100.0 * (statistics.median(warm) / statistics.median(cold) - 1.0):+.1f}%)")
        else:
            print(f"     all {len(pts)} arms on one side of the 20-minute line - this sweep cannot separate them")

    # ---- the correlation the docs claim: position vs decode, pooled per sweep
    try:
        from statistics import correlation
    except ImportError:
        correlation = None
    if correlation:
        rs = []
        for (root, commit), g in groups.items():
            if len(g) < 4:
                continue
            first = min(a["start"] for a in g)
            xs = [(a["start"] - first) / 60.0 for a in g]
            ys = [statistics.median([float(t["decode_tps"]) for t in a["turns"]]) for a in g]
            try:
                rs.append((root, correlation(xs, ys)))
            except Exception:
                pass
        if rs:
            print("\nposition-in-session vs decode, per sweep (Spearman-free, plain r):")
            for root, r in sorted(rs, key=lambda x: -abs(x[1])):
                print(f"   r={r:+.2f}  {os.path.basename(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
