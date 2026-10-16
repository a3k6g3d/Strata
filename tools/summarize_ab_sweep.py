#!/usr/bin/env python3
"""Summarize A/B arm folders into one table.

Reads every results.json under the given result root(s) and prints one row per arm:
decode tok/s (client median and the engine's own figure), first-token time, expert
cache hit, PCIe share, prompt-cache reuse, draft acceptance.  Used by
docs/SWEEP_ADAPT_KV.md; see tools/run_ab_offline.ps1 for how the arms are produced.

    python tools/summarize_ab_sweep.py bench/results/ab-confirm-kv-vram2

Two things a table of arm medians hides, and this prints both, because both have
already fooled a reading of these results (docs/BENCH_NOISE.md):

  * arms whose **launch lines are byte-identical** are the same experiment, whatever
    the --arm label says.  Grouping by --engine-arg instead lumps every unswept arm
    together across unrelated sweeps, and separates one arm from its own twin.
  * a sweep's arms are **not independent**: the machine has a cold region at the start
    of a session, so an arm's position in the session predicts its speed and its hit
    rate.  Arms inside it are marked, and their comparison to the baseline is marked
    with the same flag rather than printed as a result.
"""
import glob
import json
import os
import statistics
import sys

COLS = ("arm", "arm args", "decode", "eng dec", "ttft s", "hit %", "pcie %", "reuse %", "draft %", "n", "t+min")
COLD_MIN = 20.0  # minutes of a session the machine is still warming up in; docs/BENCH_NOISE.md


def med(xs):
    return round(statistics.median(xs), 2) if xs else None


def fmt(v, width):
    return ("-" if v is None else str(v)).rjust(width)


def start_s(j):
    """Wall clock of the arm's first measured phase, or None.

    `from_s` of 0.0 is a real timestamp (an arm with a single phase), so the test is
    `is not None`, not truthiness - otherwise that arm has no position and reads as neither
    cold nor warm.
    """
    st = [p["from_s"] for p in (j.get("results") or {}).get("phases") or []
          if p.get("from_s") is not None]
    return min(st) if st else None


def collect(root):
    rows = []
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        path = os.path.join(d, "results.json")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            j = json.load(fh)
        prm = j.get("params") or {}
        agent = (j.get("results") or {}).get("agent") or {}
        turns = [t for s in agent.get("sessions") or [] for t in s if t.get("ok")]
        eng = ((j.get("results") or {}).get("engine") or {}).get("per_test") or {}
        a = eng.get("agent session") or {}
        rows.append(
            {
                "arm": prm.get("arm") or os.path.basename(d),
                "args": " ".join(prm.get("engine_arg") or []) or "(none)",
                "launch": (j.get("machine") or {}).get("launch") or "",
                "decode": med([t["decode_tps"] for t in turns if t.get("decode_tps")]),
                "eng_dec": a.get("decode_tok_s_median"),
                "ttft": med([t["ttft_s"] for t in turns if t.get("ttft_s")]),
                "hit": a.get("cache_hit_pct"),
                "pcie": a.get("pcie_share_pct"),
                "reuse": a.get("prompt_reuse_pct"),
                "draft": a.get("draft_accept_pct"),
                "n": len(turns),
                "elapsed": round(j.get("elapsed_s") or 0),
                "t0": start_s(j),
                "turns": turns,
                "dir": d,
            }
        )
    return rows


def by_launch(rows):
    """Arms that ran the identical experiment, judged by the launch line, not the label."""
    g = {}
    for r in rows:
        if r["launch"]:
            g.setdefault(r["launch"], []).append(r)
    return g


def position(rows):
    """Minutes each arm started after the earliest arm of the same sweep folder."""
    groups = {}
    for r in rows:
        if r["t0"] is None:
            continue
        sweep = os.path.dirname(os.path.normpath(r["dir"]))
        groups.setdefault(sweep, []).append(r)
    for members in groups.values():
        base = min(r["t0"] for r in members)
        for r in members:
            r["tmin"] = round((r["t0"] - base) / 60.0, 1)


def is_cold(r):
    """An arm that ran while the machine was still warming up: its numbers are not comparable.
    An arm with no timestamp is not cold, it is unknown - the table shows '-' for its position."""
    return r.get("tmin") is not None and r["tmin"] < COLD_MIN


def pick_reference(rows):
    """The arm to measure the others against: the experiment with most company, preferring one
    that ran warm.  A cold reference makes every other arm look 50-75% faster, which is the
    machine warming up, not the flag under test - the mistake that read adapt-swaps 0/4 as
    losses and kv-resident as a win in the first sweep (docs/BENCH_NOISE.md).
    """
    groups = sorted(by_launch(rows).values(), key=len, reverse=True)
    if not groups:
        return None
    return next((g for g in groups[0] if not is_cold(g)), groups[0][0])


def paired(rows, ref):
    """Decode of each arm against the reference arm's, turn number by turn number.

    The same turn number in two arms is the same prompt, so the pairing cancels the
    draft-acceptance luck that dominates the spread between two runs of one arm.  The
    figure printed is the median of the per-turn differences: an arm's own speed, told
    to the machine's own resolution.
    """
    base = {}
    for t in ref["turns"]:
        if t.get("decode_tps"):
            base.setdefault(t.get("turn"), []).append(t["decode_tps"])
    base = {k: statistics.median(v) for k, v in base.items()}
    out = []
    for r in rows:
        if r is ref:
            continue
        got = {}
        for t in r["turns"]:
            if t.get("decode_tps"):
                got.setdefault(t.get("turn"), []).append(t["decode_tps"])
        diffs = [100.0 * (statistics.median(v) - base[k]) / base[k] for k, v in got.items() if k in base]
        if diffs:
            out.append((r, round(statistics.median(diffs), 1), len(diffs)))
    return out


def main():
    roots = sys.argv[1:] or ["bench/results"]
    width = 44
    for root in roots:
        rows = collect(root)
        position(rows)
        print("==", root)
        head = COLS[0].ljust(width) + "".join(c.rjust(10) for c in COLS[1:])
        print(head)
        print("-" * len(head))
        for r in rows:
            line = str(r["arm"])[: width - 1].ljust(width)
            for key in ("args", "decode", "eng_dec", "ttft", "hit", "pcie", "reuse", "draft", "n"):
                line += fmt(r[key], 10)
            mark = "" if r.get("tmin") is None else ("*" if r["tmin"] < COLD_MIN else " ")
            line += fmt(r.get("tmin"), 9) + mark
            print(line)
        print("  (* = started inside the machine's first %.0f minutes of the session; docs/BENCH_NOISE.md)" % COLD_MIN)
        print()
        # noise floor: arms that ran the identical experiment
        for launch, group in by_launch(rows).items():
            dec = [g["decode"] for g in group if g["decode"]]
            if len(group) < 2 or len(dec) < 2:
                continue
            spread = round(max(dec) - min(dec), 2)
            rel = round(100.0 * spread / statistics.mean(dec), 1)
            hit = [g["hit"] for g in group if g["hit"] is not None]
            print("  identical launch line x%d: decode %s (spread %s = %s%%), hit %s" % (
                len(group), dec, spread, rel, hit if hit else "-"))
            print("     %s" % launch[:120])
            for g in group:
                print("     t+%s min  %s  %s" % (fmt(g.get("tmin"), 5), fmt(g["decode"], 6), g["arm"]))
        print()
        # paired by turn, against the arm that has the most company (the shared baseline)
        ref = pick_reference(rows)
        if ref:
            print("  paired by turn number against %r at t+%s min (median of per-turn differences; + means faster):"
                  % (ref["arm"], fmt(ref.get("tmin"), 5)))
            for r, d, n in sorted(paired(rows, ref), key=lambda x: -x[1]):
                mark = " *cold" if is_cold(r) else ""
                print("     %s %s  n=%d  %s%s" % (fmt(d, 6), fmt(r.get("tmin"), 8), n, r["arm"], mark))
        print()


if __name__ == "__main__":
    main()
