#!/usr/bin/env python3
"""Summarize A/B arm folders into one table.

Reads every results.json under the given result root(s) and prints one row per arm:
decode tok/s (client median and the engine's own figure), first-token time, expert
cache hit, PCIe share, prompt-cache reuse, draft acceptance.  Used by
docs/SWEEP_ADAPT_KV.md; see tools/run_ab_offline.ps1 for how the arms are produced.

    python tools/summarize_ab_sweep.py bench/results/ab-confirm-kv-vram2
"""
import glob
import json
import os
import statistics
import sys

COLS = ("arm", "arm args", "decode", "eng dec", "ttft s", "hit %", "pcie %", "reuse %", "draft %", "n")


def med(xs):
    return round(statistics.median(xs), 2) if xs else None


def fmt(v, width):
    return ("-" if v is None else str(v)).rjust(width)


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
                "decode": med([t["decode_tps"] for t in turns if t.get("decode_tps")]),
                "eng_dec": a.get("decode_tok_s_median"),
                "ttft": med([t["ttft_s"] for t in turns if t.get("ttft_s")]),
                "hit": a.get("cache_hit_pct"),
                "pcie": a.get("pcie_share_pct"),
                "reuse": a.get("prompt_reuse_pct"),
                "draft": a.get("draft_accept_pct"),
                "n": len(turns),
                "elapsed": round(j.get("elapsed_s") or 0),
                "dir": d,
            }
        )
    return rows


def main():
    roots = sys.argv[1:] or ["bench/results"]
    width = 44
    for root in roots:
        rows = collect(root)
        print("==", root)
        head = COLS[0].ljust(width) + "".join(c.rjust(10) for c in COLS[1:])
        print(head)
        print("-" * len(head))
        for r in rows:
            line = str(r["arm"])[: width - 1].ljust(width)
            for key in ("args", "decode", "eng_dec", "ttft", "hit", "pcie", "reuse", "draft", "n"):
                line += fmt(r[key], 10)
            print(line)
        print()
        # noise floor: two identical arms, if present
        by = {}
        for r in rows:
            by.setdefault(r["args"], []).append(r)
        for args, group in by.items():
            if len(group) < 2:
                continue
            dec = [g["decode"] for g in group if g["decode"]]
            hit = [g["hit"] for g in group if g["hit"] is not None]
            if len(dec) < 2:
                continue
            spread = round(max(dec) - min(dec), 2)
            rel = round(100.0 * spread / statistics.mean(dec), 1)
            print("  noise floor for %r: decode %s (spread %s = %s%%), hit %s" % (
                args, dec, spread, rel, hit if hit else "-"))
        print()


if __name__ == "__main__":
    main()
