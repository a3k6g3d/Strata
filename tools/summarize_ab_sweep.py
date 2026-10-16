#!/usr/bin/env python3
"""Summarize A/B arm folders into one table.

Reads every results.json under the given result root(s) and prints one row per arm:
decode tok/s (client median and the engine's own figure), first-token time, expert
cache hit, PCIe share, prompt-cache reuse, draft acceptance.  Used by
docs/SWEEP_ADAPT_KV.md; see tools/run_ab_offline.ps1 for how the arms are produced.

    python tools/summarize_ab_sweep.py bench/results/ab-confirm-kv-vram2
    python tools/summarize_ab_sweep.py bench/results/my-sweep/runs.jsonl          the same, read from the one-line-per-run record
    python tools/summarize_ab_sweep.py DIR --noise bench/results/noise-floor      add a verdict against a measured noise floor

When an arm ran more than once (tools/strata_ab.py --rounds K interleaves the arms: A B C, C B A, A B C ...) each launch is compared with the
reference arm's launch of the SAME round, so slow drift cancels instead of being read as a result, and the table says whether the median
difference clears the noise: the spread between the reference arm's own launches, or a noise-floor run's warm launches (`--noise DIR`, made with
`strata_ab.py --noise-floor 3`).

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

# 2026-10-16: tools/check_cold_axis.py measured where the warm-up actually sits, and it is NOT only the session.  Inside one
# launch, run 1 of the SAME turn beats run 0 by +8.8% and run 2 by +13.9% (n=150 each) - as large as the across-launch drift -
# and three controls say that is the machine, not the prompt cache: turn 0, which has no cache to warm, still gains +5.5%; a
# base run that had already reused over half its prompt tokens still gains +13.0%; and prompt_tps, the quantity the cache
# actually moves, gains only +2.2%.  Every arm reloads a 12 GiB model, and the first thing it does after that is slow.  So
# the cold sample is run 0 of EVERY launch, not the first arm of the session, and every median below drops it when it can.


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
        warm, dropped = warm_turns(turns)
        rows.append(
            {
                "arm": prm.get("arm") or os.path.basename(d),
                "args": " ".join(prm.get("engine_arg") or []) or "(none)",
                "launch": (j.get("machine") or {}).get("launch") or "",
                "decode": med([t["decode_tps"] for t in turns if t.get("decode_tps")]),
                # the same median with this launch's warm-up run taken out; where it differs from `decode` by more than a
                # point or two, the arm's own first run is pulling it down and no arm-vs-arm table of `decode` is fair to
                # the arms that ran more times (warm_turns).
                "decode_warm": med([t["decode_tps"] for t in warm if t.get("decode_tps")]) if dropped else None,
                "warm_n": len(warm) if dropped else len(turns),
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


def warm_turns(turns):
    """The turns of one arm minus its launch's first run, when that launch ran more than once.

    Run 0 is the launch warming up (see COLD_MIN above), so a median over all runs understates every arm by roughly the same
    amount - which mostly cancels between arms, except that the arms with more runs sit further up the curve.  On the two
    8-run arms the within-arm run effect reaches +30%, against +14% on a 3-run arm, so pooling all runs makes an 8-run arm
    and a 3-run arm incomparable.  With one run there is nothing to drop and the arm is kept as measured.
    """
    runs = {t.get("run") for t in turns if t.get("run") is not None}
    if len(runs) < 2:
        return turns, False
    return [t for t in turns if t.get("run") != 0], True


def noise_within(rows):
    """The noise floor out of the sweep itself: two runs of one arm's one turn, both warm, against each other.

    Nothing extra has to be run.  Every arm already repeats the same turns, so the spread between them at the same turn
    number IS the machine's resolution - the figure docs/BENCH_NOISE.md had to compute by hand after the fact (12.1% at the
    median, 29.3% at the 90th percentile).  Only warm runs are used, for the reason in warm_turns.
    """
    percents = []
    for r in rows:
        turns, dropped = warm_turns(r.get("turns") or [])
        if not dropped:
            continue
        by_turn = {}
        for t in turns:
            if t.get("decode_tps"):
                by_turn.setdefault(t.get("turn"), []).append(t["decode_tps"])
        for values in by_turn.values():
            if len(values) < 2:
                continue
            mean = statistics.mean(values)
            if mean > 0:
                percents.append(100.0 * (max(values) - min(values)) / mean)
    if not percents:
        return {"pct": None, "n": 0, "median": None, "p90": None, "worst": None}
    percents.sort()
    return {"pct": round(percents[len(percents) // 2], 1), "n": len(percents),
            "median": round(percents[len(percents) // 2], 1),
            "p90": round(percents[min(int(len(percents) * 0.9), len(percents) - 1)], 1),
            "worst": round(percents[-1], 1)}


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
    # most company first; among equals (every arm of an interleaved sweep ran the same number of times) the one that started first, the baseline
    groups = sorted(by_launch(rows).values(),
                    key=lambda g: (-len(g), min(r["t0"] if r["t0"] is not None else float("inf") for r in g)))
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


def spread_pct(values):
    """(max - min) / mean, in percent, of a few launches' decode medians; None for fewer than two."""
    vals = [v for v in values if v]
    if len(vals) < 2:
        return None
    return round(100.0 * (max(vals) - min(vals)) / statistics.mean(vals), 1)


def assign_rounds(rows):
    """Number each arm's launches 1, 2, 3 ... in the order they started; returns {arm label: its rows in that order}."""
    arms = {}
    for r in rows:
        arms.setdefault(r["arm"], []).append(r)
    for members in arms.values():
        members.sort(key=lambda r: (r["t0"] is None, r["t0"] or 0, r["dir"]))
        for i, r in enumerate(members, 1):
            r["round"] = i
    return arms


def noise_from(root):
    """The noise floor out of a folder of launches of ONE configuration (`strata_ab.py --noise-floor N`): the spread between the warm launches.
    With three or more launches the first is dropped - it is spent warming the machine (docs/BENCH_NOISE.md)."""
    rows = collect(root)
    position(rows)
    groups = by_launch(rows) or {"": rows}
    members = max(groups.values(), key=len) if groups else []
    members = sorted(members, key=lambda r: (r["t0"] is None, r["t0"] or 0, r["dir"]))
    values = [r["decode"] for r in members if r["decode"]]
    dropped = len(values) >= 3
    if dropped:
        values = values[1:]
    return {"pct": spread_pct(values), "n": len(values), "values": values, "dropped_first": dropped, "dir": root}


def verdict(delta, noise_pct):
    if noise_pct is None:
        return "no noise figure"
    return "above noise" if abs(delta) > noise_pct else "inside noise"


def interleaved(rows, ref_arm):
    """Every other arm against the reference arm's launch of the same round, turn by turn: [{arm, deltas: [(round, % difference, n, cold)]}]."""
    arms = assign_rounds(rows)
    ref = {r["round"]: r for r in arms.get(ref_arm, [])}
    out = []
    for name, members in arms.items():
        if name == ref_arm:
            continue
        deltas = []
        for r in members:
            b = ref.get(r["round"])
            if b is None:
                continue
            p = paired([r], b)
            if p:
                deltas.append((r["round"], p[0][1], p[0][2], is_cold(r) or is_cold(b)))
        if deltas:
            out.append({"arm": name, "deltas": deltas})
    return out


def collect_jsonl(path):
    """The rows of `collect`, read from runs.jsonl (tools/bench_record.py): one record per run, per-turn decode speeds included."""
    import bench_record
    rows = []
    for rec in bench_record.read_records(path):
        turns = [{"turn": t.get("turn"), "decode_tps": t.get("decode_tps"), "ttft_s": t.get("ttft_s"), "ok": True} for t in rec.get("turns") or []]
        rows.append({"arm": rec.get("arm") or rec.get("folder") or "?", "args": " ".join(rec.get("engine_arg") or []) or "(none)",
                     "launch": rec.get("launch") or "", "decode": rec.get("decode_tps_median"), "eng_dec": rec.get("engine_decode_tok_s"),
                     "ttft": rec.get("ttft_s_median"), "hit": rec.get("hit_pct"), "pcie": rec.get("pcie_share_pct"),
                     "reuse": rec.get("prompt_reuse_pct"), "draft": rec.get("draft_accept_pct"), "n": len(turns),
                     "elapsed": round(rec.get("elapsed_s") or 0), "t0": rec.get("start_epoch"), "turns": turns,
                     "dir": os.path.dirname(os.path.abspath(path)) + os.sep + str(rec.get("folder") or len(rows))})
    return rows


def print_interleaved(rows, external):
    """The per-round comparison and its verdict; printed when an arm ran more than once, or a noise floor was given."""
    arms = assign_rounds(rows)
    multi = [a for a, m in arms.items() if len(m) >= 2]
    if not multi and not external:
        return
    ref = pick_reference(rows)
    if not ref:
        return
    ref_arm = ref["arm"]
    ref_launches = arms.get(ref_arm, [])
    warm = [r for r in ref_launches if not is_cold(r)]
    own = spread_pct([r["decode"] for r in (warm if len(warm) >= 2 else ref_launches)])     # a cold launch is not noise, it is the machine warming up
    if external and external.get("pct") is not None:
        noise, source = external["pct"], "the noise-floor run %s: %s%% over %d warm launches%s" % (
            external["dir"], external["pct"], external["n"], " (the first dropped)" if external["dropped_first"] else "")
    elif own is not None:
        used = len(warm) if len(warm) >= 2 else len(ref_launches)
        noise, source = own, "the reference arm's own launches: %s%% over %d launches%s" % (own, used, " (the cold ones left out)" if used < len(ref_launches) else "")
    else:
        noise, source = None, "none (run the reference arm more than once, or give --noise DIR)"
    print("  per round, against the reference arm %r launch for launch (+ means faster):" % ref_arm)
    print("     noise floor: %s" % source)
    for item in sorted(interleaved(rows, ref_arm), key=lambda x: -statistics.median([d[1] for d in x["deltas"]])):
        usable = [d for d in item["deltas"] if not d[3]] or item["deltas"]
        med_delta = round(statistics.median([d[1] for d in usable]), 1)
        shown = "  ".join("%+.1f%s" % (d[1], "*" if d[3] else "") for d in item["deltas"])
        print("     %s  %s  median %+.1f%%  %s" % (str(item["arm"])[:34].ljust(34), shown.ljust(30), med_delta, verdict(med_delta, noise)))
    print("  (* = a launch inside the machine's first %.0f minutes; the median leaves those out when others remain)" % COLD_MIN)
    print()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    external = None
    if "--noise" in argv:
        i = argv.index("--noise")
        external = noise_from(argv[i + 1])
        del argv[i:i + 2]
    roots = argv or ["bench/results"]
    width = 44
    for root in roots:
        rows = collect_jsonl(root) if root.endswith(".jsonl") else collect(root)
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
        # the noise floor the sweep already contains: warm runs of one turn against each other
        within = noise_within(rows)
        if within["n"]:
            print("  noise floor from this sweep's own repeats (same turn, warm runs, no extra launches needed): "
                  "n=%d pairs, median %s%%, p90 %s%%, worst %s%%"
                  % (within["n"], within["median"], within["p90"], within["worst"]))
            print("     a gap between two arm medians smaller than the median line is not a finding; "
                  "docs/BENCH_NOISE.md")
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
            noise = external["pct"] if external and external.get("pct") is not None else within["pct"]
            for r, d, n in sorted(paired(rows, ref), key=lambda x: -x[1]):
                mark = " *cold" if is_cold(r) else ""
                said = "  " + verdict(d, noise) if noise is not None and not is_cold(r) else ""
                print("     %s %s  n=%d  %s%s%s" % (fmt(d, 6), fmt(r.get("tmin"), 8), n, r["arm"], mark, said))
        print()
        print_interleaved(rows, external)


if __name__ == "__main__":
    main()
