#!/usr/bin/env python3
"""Read where a decode window's time goes, from an engine log written with STRATA_VERIFY_PROFILE=1 STRATA_DECODE_TIMING=1.

With both set, `strata --serve` prints two lines per request: `strata decode timing: ...` (the window split host-side:
GPU-reach wait, per-layer host and CPU pool, draft, and the per-layer-window expert counts) and `strata decode GPU stages
(ms/window): ...` (every GPU stage of the window, GDN and QSA layers apart).  This tool reads every such pair in a log and
prints medians and ranges over the requests - the numbers a doc may quote - plus the three waits the GPU spends in:

  waitA    the doorbell round trip: the GPU rang (router+plan+bell), the host planned the group, flag A came back
  waitB    the PCIe share of the missed experts copied into GPU staging (DMA); flag B is raised by a host function
           once the copies have landed (verify.cpp, fetch_dma)
  waitCPU  the CPU pool's results for its share of the missed experts

Each request's share is its wait over its own window; the median is over requests.  Nothing here is a comparison
between arms, so no noise floor applies; the ranges are the spread over the requests in the log.

An instrumented engine prints a third line per request - `strata decode tier split (per window): ...` - counting the two
tiers of the copy path: the blobs that were already resident (RAM) and the blobs read off the drive (files), each with
its own clock.  The counts are the strong half (they cost nothing); the milliseconds are the weak half, since the RAM
tier's clock is a steady_clock::now() around every resident hit and so is partly measuring itself.  Both arms of a run
get the same env block, so arms compare to arms; nothing here compares to a run of the uninstrumented binary.

An instrumented engine prints a fourth line per request - `strata decode submissions (per window): ...` - counting the
submissions the copy path handed to the runtime and the blobs inside them.  The counts are exact (two atomics at the
submission point), which makes them the strongest thing this reader prints; the ratio is the reading - ~1 blob per
submission is the one-by-one loop, far more is the batch.

    python tools/read_window_profile.py bench/results/window-profile-2026-10-21/engine-log.txt
"""
import argparse
import re
import statistics as S
import sys

TIMING = {
    "window ms": r"([\d.]+) ms/window =",
    "tokens/window": r"([\d.]+) tokens/window",
    "GPU-reach wait ms": r"GPU-reach wait ([\d.]+)",
    "per-layer host ms": r"per-layer host ([\d.]+)",
    "  of which CPU pool ms": r"CPU ([\d.]+)\]",
    "draft ms": r"draft ([\d.]+);",
    "CPU experts / layer-window": r"CPU experts ([\d.]+)",
    "VRAM hits / layer-window": r"VRAM hits ([\d.]+), PCIe",
    "PCIe experts / layer-window": r"PCIe ([\d.]+); adapt",
}
WAITS = ("waitA", "waitB", "waitCPU")

# the submissions the copy path handed to the runtime, stamped per window by the instrumented build; the counts are exact,
# and blobs per submission is the reading (~1 is the loop, far more is the batch).  Printed only when the log stamped it.
SUB = re.compile(r"(\d+) submissions, (\d+) blobs, (\d+) batched")
SUBS = (("submissions/window", 1), ("blobs/window", 2), ("batched submissions/window", 3))

# The two tiers of the copy path, printed per window beside the stage table when the engine is the instrumented one.
# Counts are the strong half (they cost nothing); the milliseconds are the weak half - the RAM tier's clock is a
# steady_clock::now() around every resident hit, so it is partly measuring itself.  Read the times as shares.
TIER = re.compile(r"RAM (\d+) blobs ([\d.]+) ms, files (\d+) blobs ([\d.]+) ms")
TIERS = (("RAM tier blobs/window", 1), ("RAM tier ms/window", 2), ("file tier blobs/window", 3), ("file tier ms/window", 4))


def num(pattern, line):
    m = re.search(pattern, line)
    return float(m.group(1)) if m else None


def stage(line, section, name):
    parts = [p for p in line.split("|") if section in p]
    if not parts:
        return 0.0
    m = re.search(r"(?<![\w+])" + re.escape(name) + r" ([\d.]+)", parts[0])
    return float(m.group(1)) if m else 0.0


def spread(values):
    return f"median {S.median(values):7.2f}  range {min(values):.2f}-{max(values):.2f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("log")
    a = ap.parse_args(argv)
    lines = open(a.log, encoding="utf-8", errors="replace").read().splitlines()
    timing = [l for l in lines if l.startswith("strata decode timing:")]
    stages = [l for l in lines if l.startswith("strata decode GPU stages (ms/window):")]
    if not timing or len(timing) != len(stages):
        print(f"{len(timing)} timing lines and {len(stages)} GPU-stage lines: expected one pair per request, both env "
              "variables set (STRATA_VERIFY_PROFILE=1 STRATA_DECODE_TIMING=1)", file=sys.stderr)
        return 1
    print(f"{a.log}: {len(timing)} requests\n\nthe window, host side:")
    for label, pattern in TIMING.items():
        vals = [v for v in (num(pattern, l) for l in timing) if v is not None]
        if vals:
            print(f"  {label:28s} {spread(vals)}")
    window = [num(TIMING["window ms"], l) for l in timing]
    print("\nwhat the GPU waits for, ms/window (GDN + QSA layers) and share of the window:")
    total = [0.0] * len(stages)
    for w in WAITS:
        per = [stage(l, "GDN", w) + stage(l, "QSA", w) for l in stages]
        total = [t + p for t, p in zip(total, per)]
        share = [p / x * 100 for p, x in zip(per, window)]
        print(f"  {w:8s} {spread(per)}   share {S.median(share):5.1f}%")
    share = [t / x * 100 for t, x in zip(total, window)]
    print(f"  {'all three':8s} {spread(total)}   share {S.median(share):5.1f}%")
    layers = [num(r"\| total ([\d.]+) ms/window", l) for l in stages]
    layers = [v for v in layers if v is not None]
    if layers:
        print(f"\nGPU stages' own total, ms/window: {spread(layers)}")
    tiers = [TIER.search(l) for l in lines if l.startswith("strata decode tier split (per window):")]
    tiers = [m for m in tiers if m]
    if tiers:
        print(f"\nthe two tiers of the copy path, per window ({len(tiers)} requests printed it):")
        for label, g in TIERS:
            print(f"  {label:28s} {spread([float(m.group(g)) for m in tiers])}")
    subs = [SUB.search(l) for l in lines if l.startswith("strata decode submissions (per window):")]
    subs = [m for m in subs if m]
    if subs:
        print(f"\nwhat the copy path handed to the runtime, per window ({len(subs)} requests printed it):")
        for label, g in SUBS:
            print(f"  {label:28s} {spread([float(m.group(g)) for m in subs])}")
        ratio = [float(m.group(2)) / float(m.group(1)) for m in subs if float(m.group(1))]
        if ratio:
            print(f"  {'blobs per submission':28s} {spread(ratio)}   <- the reading: ~1 is the loop, far more is the batch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
