#!/usr/bin/env python3
"""Read an A/B sweep that ran the non-agent tests (chat, 4k), which summarize_ab_sweep.py and read_sweep_warm.py cannot:
both read agent turns only, so a chat/4k sweep prints empty rows there.

Per launch: the chat decode median, the 4K prompt's reading speed and the engine's expert-cache hit rate, then per arm the
launches side by side and each arm against the first one (the reference): the difference of the medians, the reference's
own spread over its repeats, and whether every launch of the arm lies outside every launch of the reference - the only
verdict three launches an arm can carry.

    python tools/read_suite_sweep.py bench/results/ab-cache-off-nonagent
"""
import argparse
import glob
import json
import os
import statistics as S
import sys


def launches(folder):
    out = []
    for f in sorted(glob.glob(os.path.join(folder, "*", "results.json"))):
        r = json.load(open(f, encoding="utf-8"))
        res = r.get("results") or {}
        four = next((v for v in (res.get("prompts") or {}).values()
                     if isinstance(v, dict) and str(v.get("target_tokens")) == "4096"), {})
        out.append({"arm": (r.get("params") or {}).get("arm") or "?",
                    "chat": ((res.get("chat") or {}).get("decode_tps") or {}).get("median"),
                    "prompt4k": (four.get("prompt_tps") or {}).get("median"),
                    "hit": ((res.get("engine") or {}).get("whole_suite") or {}).get("cache_hit_pct"),
                    "folder": os.path.basename(os.path.dirname(f))})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sweep")
    a = ap.parse_args(argv)
    rows = launches(a.sweep)
    if not rows:
        print(f"no results.json under {a.sweep}", file=sys.stderr)
        return 1
    arms = []
    for r in rows:
        if r["arm"] not in arms:
            arms.append(r["arm"])
    print(f"{a.sweep}: {len(rows)} launches, {len(arms)} arms, reference = {arms[0]!r}\n")
    for r in rows:
        print(f"  {r['arm']:34s} chat decode {r['chat']}  4K prompt {r['prompt4k']} tok/s  hit {r['hit']}%")
    ref = [r for r in rows if r["arm"] == arms[0]]
    for metric, label in (("chat", "chat decode tok/s"), ("prompt4k", "4K prompt tok/s")):
        rv = [r[metric] for r in ref if r[metric] is not None]
        if len(rv) < 2:
            continue
        print(f"\n{label}: reference median {S.median(rv):.2f}, its own spread {(max(rv) - min(rv)) / S.median(rv) * 100:.1f}% "
              f"over {len(rv)} launches")
        for arm in arms[1:]:
            v = [r[metric] for r in rows if r["arm"] == arm and r[metric] is not None]
            if not v:
                continue
            apart = max(v) < min(rv) or min(v) > max(rv)
            print(f"  {arm:34s} median {S.median(v):.2f}  {(S.median(v) / S.median(rv) - 1) * 100:+.1f}%  "
                  f"{'separated: every launch outside every reference launch' if apart else 'overlaps the reference'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
