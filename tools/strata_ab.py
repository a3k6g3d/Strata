#!/usr/bin/env python3
"""Run the arms of one experiment, one after the other, and print each beside the first.

    python tools/strata_ab.py                      all four arms of the cache-vs-drafts question
    python tools/strata_ab.py --tests chat,4k     only these tests, in every arm
    python tools/strata_ab.py --arm-name drafts-off ...   one arm on its own

An experiment asks which of two settings carries the speed, and answers it by running the SAME suite with one changed
engine argument at a time.  strata_bench.py already does one arm (--engine-arg, --arm, --compare-only); this runs the
whole set, so an arm cannot be forgotten and the arms cannot be mixed up: the first is the baseline and every later one
is printed against it.

Two things it insists on, both learned from measuring this machine:

* The GPU must be free.  An arm starts its own server, and a second one beside the Strata you are chatting through
  either will not fit (11.5 of 12.2 GiB are already gone) or will be measured while the first steals cycles - either way
  the numbers mean nothing.  So it refuses to start while a Strata answers on the port, and says what to do.
* Nothing else may talk to the server while an arm runs.  The engine reports the cache's hit rate and the draft
  acceptance per request, and those records carry no bench tag: they are put back against the tests by their own
  seconds.  Another client's requests in the same window are counted as ours.  One arm, one user, nothing else.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "tools" / "strata_bench.py"

# The question the video left open ("llama.cpp Just Got an Expert Cache. Do You Still Need Strata?"): the expert cache
# and the speculative drafts both put work the card cannot hold back within reach, so a fast run says nothing about
# which of them made it fast.  These four arms separate them: each takes away one, and the last takes away both.
# The drafts-off recipe is the one bench/results/exchange-rotation-ab.json already used: load the MTP runtime the
# configuration names, but offer at most one token and switch the suffix drafter off, so the runtime is loaded exactly
# as in the other arms and no draft is ever offered.
# The fifth arm asks a question the first four could not.  All of them reported a 0.0% expert-cache hit rate, and the
# engine says why that is possible even when a workload repeats: without `--expert-cache-per-layer` the cache's slots
# are one pool shared by all 48 layers, filled in the order pairs ARRIVE and never evicted, so the first n_slots
# distinct (layer, position) pairs take every slot - in-tree that is measured at 2.97% at 256 slots, against 21.4% at
# 8 slots per layer and 70.4% at 64.  A 1951-slot cache on a fresh prompt therefore spends everything on the warm-up's
# own positions, and nothing arriving later can be resident.  This arm changes that ONE flag on top of the baseline and
# reads the hit line, so it tests the cache's own admission policy rather than whether the workload repeats.
ARMS = [("as configured", []),
        ("expert-cache off", ["--expert-cache 0"]),
        ("drafts off", ["--mtp-max-t 1 --suffix-draft 0"]),
        ("both off", ["--expert-cache 0", "--mtp-max-t 1 --suffix-draft 0"]),
        ("expert cache, per-layer slots", ["--expert-cache-per-layer"])]
NOTE = ("Does the expert cache carry the speed, or the drafts? Same model, same prompts, same runs; one engine "
        "argument changed per arm. Decode tok/s is the engine's own figure; the cache hit and PCIe share come from "
        "the engine's per-request records, matched to each test by their own seconds.")


def port_busy(port: int) -> bool:
    """Is something answering on this port?  A Strata, or anything else holding the GPU."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            return True
    except Exception:
        return False


def newest_result(parent: Path, since: float) -> Path | None:
    """The folder this arm just wrote: the newest one that appeared after we started it."""
    fresh = sorted((d for d in parent.iterdir() if d.is_dir() and d.stat().st_mtime >= since),
                   key=lambda d: d.stat().st_mtime)
    return fresh[-1] if fresh else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="the model configuration to run each arm on (default: the newest strata-*.json)")
    ap.add_argument("--port", type=int, default=8080, help="the port to check is free, and to run each arm on")
    ap.add_argument("--tests", default="chat,4k", help="these tests in every arm (default chat,4k: the two that answer the question)")
    ap.add_argument("--runs", type=int, default=3, help="runs per test in every arm (default 3)")
    ap.add_argument("--out", default=None, help="where the arms' folders go (default bench/results/ab-cache-vs-drafts)")
    ap.add_argument("--arm-name", default=None, help="run only this arm (by its label), and stop")
    ap.add_argument("--ready-timeout", type=int, default=600, help="seconds to wait for the model to load per arm")
    ap.add_argument("--yes", action="store_true", help="start even if something already answers on the port")
    ap.add_argument("--dry-run", action="store_true", help="print the arms and stop: check the recipe before spending an hour")
    a = ap.parse_args(argv)

    if not BENCH.exists():
        print(f"No {BENCH} here.", file=sys.stderr)
        return 2
    parent = Path(a.out) if a.out else ROOT / "bench" / "results" / "ab-cache-vs-drafts"
    parent.mkdir(parents=True, exist_ok=True)

    arms = [x for x in ARMS if not a.arm_name or x[0] == a.arm_name]
    if not arms:
        print(f"No arm called {a.arm_name!r}: it is one of " + ", ".join(f"{n!r}" for n, _ in ARMS), file=sys.stderr)
        return 2

    if port_busy(a.port) and not (a.yes or a.dry_run):
        print(f"Something already answers on port {a.port}.  An arm has to load the model itself, on an otherwise empty "
              f"GPU - and while a Strata is serving a chat, its requests land inside the arm's own seconds and are "
              f"counted as the arm's.  Stop that server (Ctrl-C in its window, or the desktop's Stop), then run this "
              f"again; --yes runs it anyway and the numbers are then only indicative.", file=sys.stderr)
        return 2

    cfg = ["--config", a.config] if a.config else []
    if not cfg:
        print("Which model?  Give --config FILE: an arm starts its own server, and the newest strata-*.json of this "
              "folder is not necessarily the one you want measured (it is the one the install last used).", file=sys.stderr)
        return 2

    # name the interpreter, not the .py: Windows cannot CreateProcess a script by itself (WinError 193, "%1 is not a
    # valid Win32 application"), which is how the first attempt at this died in three seconds without loading a model.
    if a.dry_run:
        for i, (name, extra) in enumerate(arms):
            print(f"arm {i + 1}/{len(arms)}: {name}\n  " + " ".join(cmd for cmd in [*cfg, *("--engine-arg " + x for x in extra)])
                  + f"\n  runs {a.runs} x {a.tests} on port {a.port}, out: {parent}")
        print("\nNothing has run yet.  Drop --dry-run when the GPU is free.")
        return 0

    base = [sys.executable, "-u", str(BENCH), *cfg, "--port", str(a.port), "--tests", a.tests, "--runs", str(a.runs),
            "--out", str(parent), "--ready-timeout", str(a.ready_timeout), "--note", NOTE]
    done, failed = [], []
    for i, (name, extra) in enumerate(arms):
        cmd = [*base, "--arm", name] + sum([["--engine-arg", x] for x in extra], [])
        print(f"\n=== arm {i + 1}/{len(arms)}: {name} ===")
        print("  " + " ".join(cmd[1:]) + "\n", flush=True)
        since = time.time() - 1
        t = time.time()
        # one arm at a time, in this console, so the machine is doing nothing else and the log is the arm's own
        rc = subprocess.call(cmd)
        spent = time.time() - t
        d = newest_result(parent, since)
        if d is None:
            print(f"  arm failed after {spent / 60:.1f} min (exit {rc}) and wrote nothing; the arms after this one "
                  f"cannot be compared to it", flush=True)
            failed.append(name)
            continue
        print(f"  {d.name}   ({spent / 60:.1f} min, exit {rc})", flush=True)
        done.append((name, d))
        if rc != 0:
            failed.append(name)
        if i + 1 < len(arms):                      # let the engine's process and its VRAM actually go before the next load
            print("  waiting 20 s for the GPU to be free again ...", flush=True)
            time.sleep(20)

    if not done:
        print("\nNo arm produced a result: nothing to compare.  The logs above say why.", file=sys.stderr)
        return 1
    base_dir = done[0][1]
    for name, d in done[1:]:
        print(f"\n=== {name}  vs  {done[0][0]} ===")
        subprocess.call([sys.executable, BENCH, "--compare-only", str(d), "--compare", str(base_dir)])
    print(f"\n{len(done)} arm(s) in {parent}: " + ", ".join(n for n, _ in done))
    if failed:
        print("incomplete: " + ", ".join(failed))
    print("Read the arms' own README.md for the cache and draft figures behind these speeds.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
