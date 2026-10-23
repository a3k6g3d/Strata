#!/usr/bin/env python3
"""Run the arms of one experiment, one after the other, and print each beside the first.

    python tools/strata_ab.py                      all four arms of the cache-vs-drafts question
    python tools/strata_ab.py --tests chat,4k     only these tests, in every arm
    python tools/strata_ab.py --arm-name drafts-off ...   one arm on its own
    python tools/strata_ab.py --sweep adapt-kv --rounds 3  every arm three times, the arms taking turns (A B C, C B A, A B C)
    python tools/strata_ab.py --noise-floor 3 --config F   the baseline arm alone, three launches: how much two identical launches differ

An experiment asks which of two settings carries the speed, and answers it by running the SAME suite with one changed
engine argument at a time.  strata_bench.py already does one arm (--engine-arg, --arm, --compare-only); this runs the
whole set, so an arm cannot be forgotten and the arms cannot be mixed up: the first is the baseline and every later one
is printed against it.

Why --rounds: with every repeat of one arm back to back, an arm's position in the session and the machine's drift are the SAME variable (the
machine runs 10% to 35% slower for its first minutes, then settles: docs/BENCH_NOISE.md), and no table can separate them afterwards.  With
--rounds K each arm is launched K times, the arms taking turns, and the order is reversed every second round (A B C, then C B A) so a drift that is
linear in time cancels to first order.  tools/summarize_ab_sweep.py then compares each launch with the reference arm's launch of the same round
and says whether the difference clears the noise.  --runs is the total per arm: each launch does runs / rounds of them (at least one).

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

# The second experiment, and the one the audit in docs/REJECTED_PROPOSALS.md left standing: the adaptation and KV-
# streaming knobs, varied one at a time against this machine's own configuration.  Two things make this list different
# from the five above, and both are recorded in docs/SWEEP_ADAPT_KV.md so an arm is not believed to say more than it
# can.  (1) `--pcie-frac`, `--pool-workers` and `--expert-cache` are passed in NO arm: the link probe, the CPU topology
# and the fit calculation answer for this machine, and pinning any of them would be pinning a measurement away.  (2) Two
# of the values one might sweep are already the configuration's own - strata.json sets --adapt-swaps 8 and
# --vram-reserve-mib 640, and --adapt-decay is absent where the engine's default is 0.7 - so they are the baseline
# rather than arms, and asking for them again would only re-measure the baseline under a second label.  The last arm
# does that on purpose: on this machine one arm's decode medians ranged 13-35 tok/s, so an arm is worth believing only
# once a same-configuration arm has said how much of that spread was noise.
# 16384 is NOT in the kv-resident arms: layer.cpp:667 sets a floor of 20480 and layer.cpp:648 clamps to it, so 16384
# would have measured 20480 under the label 16384 with nothing complaining (generate.cpp:2462 rejects only negatives).
SWEEPS = {
    "adapt-kv": {
        "note": ("Which adaptation / KV-streaming setting carries the speed?  Same model, same prompts, same runs; one "
                 "engine argument changed per arm, and --pcie-frac / --pool-workers / --expert-cache left to the "
                 "engine's own probes in every arm.  The three --kv-resident arms also lose --kv-grow on their own "
                 "(generate.cpp:4228 gates the elastic KV on the KV being wholly in VRAM), so their difference belongs "
                 "to the pair, not to one flag.  The last arm repeats the baseline to measure the noise floor."),
        "arms": [("as configured", []),
                 ("adapt-swaps 0", ["--adapt-swaps 0"]),
                 ("adapt-swaps 4", ["--adapt-swaps 4"]),
                 ("adapt-swaps 16", ["--adapt-swaps 16"]),
                 ("adapt-decay 0.65", ["--adapt-decay 0.65"]),
                 ("adapt-decay 0.80", ["--adapt-decay 0.80"]),
                 ("kv-resident 20480", ["--kv-resident 20480"]),
                 ("kv-resident 24576", ["--kv-resident 24576"]),
                 ("kv-resident 32768", ["--kv-resident 32768"]),
                 ("vram-reserve 512", ["--vram-reserve-mib 512"]),
                 ("vram-reserve 600", ["--vram-reserve-mib 600"]),
                 ("vram-reserve 768", ["--vram-reserve-mib 768"]),
                 ("as configured (repeat)", [])],
    },
    # The one setting worth asking again after a hardware change (a faster CPU and a PCIe Gen4 link): how often the GPU
    # expert cache is updated.  The configuration's own --adapt-swaps 8 is the baseline; run it with --rounds so the
    # arms take turns and the baseline's own spread says what a difference has to beat.
    "adapt-swaps": {
        "note": ("Does a faster link or CPU change how often the expert cache should adapt?  Same model, same prompts, "
                 "same runs; --adapt-swaps 4 and 16 against the configuration's own 8, the arms taking turns."),
        "arms": [("as configured", []),
                 ("adapt-swaps 4", ["--adapt-swaps 4"]),
                 ("adapt-swaps 16", ["--adapt-swaps 16"])],
    },
    # 2026-10-18: 4 was slower than 8 and 16 looked slightly faster with a clearly higher cache hit rate, so the next
    # question is where the gain stops.
    # 48 became the configuration's own value; this asks whether going further still helps or starts to cost.
    "adapt-swaps-top": {
        "note": ("Is there more above --adapt-swaps 48, the configuration's own value?  --adapt-swaps 64, 96 and 128 against "
                 "it, the arms taking turns."),
        "arms": [("as configured", []),
                 ("adapt-swaps 64", ["--adapt-swaps 64"]),
                 ("adapt-swaps 96", ["--adapt-swaps 96"]),
                 ("adapt-swaps 128", ["--adapt-swaps 128"])],
    },
    "adapt-swaps-high": {
        "note": ("Where do returns stop as the expert cache is allowed to change more per adaptation step?  "
                 "--adapt-swaps 16, 24, 32 and 48 against the configuration's own 8, the arms taking turns."),
        "arms": [("as configured", []),
                 ("adapt-swaps 16", ["--adapt-swaps 16"]),
                 ("adapt-swaps 24", ["--adapt-swaps 24"]),
                 ("adapt-swaps 32", ["--adapt-swaps 32"]),
                 ("adapt-swaps 48", ["--adapt-swaps 48"])],
    },
    # The control the last three noise-floor batches could not give: the same arm, launched in a different order each
    # repetition (A B, B A, A B), so an arm's position in the session stops being the same variable as the machine's
    # drift.  Both arms are byte-identical, so every difference between them IS the noise - which is the point: the
    # batches on disk put the same arm in the same position every time, so their spread can never separate the two.
    # Three rounds of two arms is the smallest run that has a round where the order is reversed at all.
    # The cache-off arm the original four could not make.  `--expert-cache 0` is not off: generate.cpp:3499 turns a 0
    # back into auto whenever a profile is in use, which is what happened to both arms labelled cache-off in
    # bench/results/ab-cache-vs-drafts-agent - their own engine logs say 1951/1954 experts, not zero.  So off has no
    # spelling on this rig while a profile is loaded, and the arm asks for ONE expert instead: the flag truncates the
    # ranked list, so the cache holds a single pair and everything else is served off the fast path.  The arm is only
    # believed if the engine's own log line reads 1 expert - that line is the check the old arm failed.
    # Stating the confound rather than hiding it: a 1-expert cache hands ~3.7 GiB of VRAM back, and the KV and prompt
    # path will take it, so this arm is "cache off AND 3.7 GiB given back", not cache off alone.
    "cache-off": {
        "note": ("Does the expert cache carry the speed, on its own?  The baseline against a run whose GPU cache holds "
                 "one expert, so the cache is off in effect (`--expert-cache 0` is not: the parser turns a 0 back into "
                 "auto while a profile is in use, generate.cpp:3499).  Same model, same prompts, same runs, one engine "
                 "argument changed.  The arm also hands ~3.7 GiB of VRAM back to the KV and prompt path, so it is "
                 "'cache off plus that memory given back', not cache off alone.  Read the engine's own log line in each "
                 "arm before believing any of it: it must say 1951 experts and 1."),
        "arms": [("as configured", []),
                 ("expert-cache 1 (off in effect)", ["--expert-cache 1"])],
    },
    # Every way this engine has to produce more than one token per verify window, against the configuration's own (the
    # retrained Basalt MTP layer, --spec 4, prompt-lookup suffix drafts at their default of 3).  One flag per arm; a flag
    # given again here overrides the configuration's, because the engine keeps the last value it reads.  Not here: the
    # coupled / Gumbel draft sampling (STRATA_SPEC_COUPLED, STRATA_SPEC_GUMBEL), which only acts on requests sampled at a
    # temperature above 0 and this suite is greedy; EAGLE, Medusa, a separate small draft model and diffusion or NCM
    # drafters, which this engine does not implement.
    "spec-methods": {
        "note": ("Which multi-token method carries the speed on this machine?  The configuration's MTP drafting against no "
                 "speculation, the model's original MTP head, MTP without prompt lookup, MTP with a prompt-lookup chain after "
                 "its drafts, a 4-bit draft layer and a wider verify window, one flag per arm, the arms taking turns."),
        "arms": [("as configured", []),
                 ("no speculation", ["--spec 1"]),
                 ("original MTP head", ["--mtp mtp-rt"]),
                 ("MTP without prompt lookup", ["--suffix-draft 0"]),
                 ("MTP + lookup chain 4", ["--lookup-chain 4"]),
                 ("4-bit draft layer", ["--mtp-q4 all"]),
                 ("spec 6", ["--spec 6"]),
                 ("spec 2", ["--spec 2"])],
    },
    "spec-low": {
        "note": ("How much of the speed does the verify window carry?  The configuration's --spec 4 against the smallest window "
                 "the engine accepts on this pack, --spec 2, and --spec 3 between them, the arms taking turns."),
        "arms": [("as configured", []),
                 ("spec 2", ["--spec 2"]),
                 ("spec 3", ["--spec 3"])],
    },
    # The low end of the window.  "No speculation" cannot be an arm on this model: a native (IQ) pack needs --spec T with T >= 2
    # ("is a native (IQ) pack: it needs --native SHARD1, --spec T (T >= 2) and --prefill CHUNK", seen 2026-10-21 when --spec 1
    # exited at start-up).  --spec 2 is the smallest window the engine accepts - still MTP, one draft per check - so it bounds how
    # much of the speed the drafting carries from below without being "off".  Run with the baseline taking turns beside it.
    # The one flag spec-methods put above the noise (+3.4% and +2.4% on engine decode, both launches above both baseline launches)
    # in a sweep whose baseline launches were 92 minutes apart: turned off and on, taking turns, to see whether it holds.
    "lookup-off": {
        "note": ("Is prompt lookup (suffix drafts, default 3) a net cost on this machine?  The configuration against "
                 "--suffix-draft 0, the two taking turns, so the position effect that the first reading could not rule out "
                 "is broken up."),
        "arms": [("as configured", []),
                 ("prompt lookup off", ["--suffix-draft 0"])],
    },
    # Build-plan item 3, retargeted (docs/WINDOW_PROFILE_CUDA.md): the GPU waits 59% of a decode window for the PCIe share of the
    # missed experts.  The engine picks that share itself - 0.55 from the link probe, then PcieAuto refines it from the CPU pool's
    # speed in the first decode windows (up to three updates, steps of 0.05, clamped to 0.10-0.60); on 2026-10-21 it moved to 0.45.
    # An explicit --pcie-frac fixes the share from the first token and switches that refinement off, so 0.45 is the control for
    # "fixed from the start" against the automatic arm, and 0.60 is the top of what the automatic rule would ever choose.
    # Less share on PCIe means more of the missed experts on the CPU pool, whose wait was 6% of the window.
    "pcie-frac": {
        "note": ("Does a different PCIe share of the missed experts shorten the decode window?  The automatic share (refined to 0.45 "
                 "on this machine) against fixed 0.25, 0.35, 0.45 and 0.60, one flag per arm, the arms taking turns."),
        "arms": [("as configured", []),
                 ("pcie-frac 0.25", ["--pcie-frac 0.25"]),
                 ("pcie-frac 0.35", ["--pcie-frac 0.35"]),
                 ("pcie-frac 0.45", ["--pcie-frac 0.45"]),
                 ("pcie-frac 0.60", ["--pcie-frac 0.60"])],
    },
    "noise-interleaved": {
        "note": ("The same arm twice, the two taking turns (round 1 A B, round 2 B A, round 3 A B) so the machine's "
                 "drift is not the same variable as an arm's position.  Nothing about the engine changes between the "
                 "two arms, so any difference between them - within a round, or between rounds - is the noise, "
                 "measured with the position effect broken up rather than baked in."),
        "arms": [("as configured", []),
                 ("as configured (repeat)", [])],
    },
    "iq512": {
        "note": ("Is the CPU expert pool compute-bound?  The engine's AVX-512 i-quant rows (the default on this CPU) against "
                 "the AVX2 rows (STRATA_NO_IQ512=1), taking turns.  Nothing else differs."),
        "arms": [("as configured (AVX-512 rows)", []),
                 ("AVX2 rows (STRATA_NO_IQ512=1)", ["--engine-env=STRATA_NO_IQ512=1"])],
    },
}


def schedule(arms, rounds: int = 1, order: str = "alternate") -> list:
    """[(round, index in the pool, arm)] in the order to launch them: each round runs every arm once; with order "alternate" every second round
    runs them backwards (A B C, C B A, A B C ...), with "same" always forwards."""
    plan = []
    for rnd in range(1, max(1, rounds) + 1):
        seq = list(enumerate(arms))
        if order == "alternate" and rnd % 2 == 0:
            seq.reverse()
        plan += [(rnd, i, arm) for i, arm in seq]
    return plan


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
    ap.add_argument("--runs", type=int, default=3, help="runs per test in every arm (default 3); with --rounds, the total: each launch does runs / rounds")
    ap.add_argument("--rounds", type=int, default=1, help="launch every arm this many times, the arms taking turns (default 1: each arm once, back to back)")
    ap.add_argument("--order", choices=("alternate", "same"), default="alternate",
                    help="with --rounds: reverse the arms every second round (default) so linear drift cancels, or always go forwards")
    ap.add_argument("--noise-floor", type=int, default=0, metavar="N",
                    help="launch the baseline arm alone N times (3 is the usual): the figure a difference between two arms has to beat")
    ap.add_argument("--out", default=None, help="where the arms' folders go (default bench/results/ab-cache-vs-drafts)")
    ap.add_argument("--arm-name", default=None, help="run only this arm (by its label), and stop")
    ap.add_argument("--sweep", default=None, choices=sorted(SWEEPS),
                    help=f"run the arms of one experiment instead of the cache-vs-drafts set: "
                         f"{', '.join(sorted(SWEEPS))}")
    ap.add_argument("--ready-timeout", type=int, default=600, help="seconds to wait for the model to load per arm")
    ap.add_argument("--yes", action="store_true", help="start even if something already answers on the port")
    ap.add_argument("--dry-run", action="store_true", help="print the arms and stop: check the recipe before spending an hour")
    a = ap.parse_args(argv)

    if not BENCH.exists():
        print(f"No {BENCH} here.", file=sys.stderr)
        return 2
    if a.rounds < 1 or a.noise_floor < 0:
        print("--rounds must be at least 1 and --noise-floor at least 0.", file=sys.stderr)
        return 2
    default_out = "noise-floor" if a.noise_floor else "ab-cache-vs-drafts"
    parent = Path(a.out) if a.out else ROOT / "bench" / "results" / default_out
    parent.mkdir(parents=True, exist_ok=True)

    # a sweep brings its own arms AND its own note: the note is printed in every arm's README, and a README that
    # describes the cache-vs-drafts question above a table of KV numbers would mislead whoever reads it next month.
    pool, note = (SWEEPS[a.sweep]["arms"], SWEEPS[a.sweep]["note"]) if a.sweep else (ARMS, NOTE)
    arms = [x for x in pool if not a.arm_name or x[0] == a.arm_name]
    if not arms:
        print(f"No arm called {a.arm_name!r}: it is one of " + ", ".join(f"{n!r}" for n, _ in pool), file=sys.stderr)
        return 2
    rounds = a.noise_floor or a.rounds
    if a.noise_floor:
        arms = arms[:1]                                    # the baseline, launched again and again: nothing else changes
    plan = schedule(arms, rounds, "same" if a.noise_floor else a.order)
    runs_each = max(1, round(a.runs / rounds)) if rounds > 1 else a.runs

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
        for i, (rnd, _, (name, extra)) in enumerate(plan):
            print(f"launch {i + 1}/{len(plan)}: {name}  (round {rnd} of {rounds})\n  " + " ".join(cmd for cmd in [*cfg, *(x if x.startswith("--engine-") else f"--engine-arg={x}" for x in extra)])
                  + f"\n  runs {runs_each} x {a.tests} on port {a.port}, out: {parent}")
        print("\nNothing has run yet.  Drop --dry-run when the GPU is free.")
        return 0

    base = [sys.executable, "-u", str(BENCH), *cfg, "--port", str(a.port), "--tests", a.tests, "--runs", str(runs_each),
            "--out", str(parent), "--ready-timeout", str(a.ready_timeout), "--note", note]
    done, failed = [], []
    for i, (rnd, _, (name, extra)) in enumerate(plan):
        # `--engine-arg VALUE` would not survive an argument that starts with a dash: argparse reads the next token as a
        # flag rather than as the value, so the per-layer arm (--expert-cache-per-layer) died at its argument line while
        # the others, whose values start with a letter, ran fine.  The `=` form always takes the value as written.
        # an arm's items reach the launched server as --engine-arg; an item that is already one of the benchmark's own arm
        # switches (--engine-env=KEY=VALUE, written without a space for the same reason as above) passes through as itself,
        # so an arm can be an environment knob and not only a command-line argument.
        cmd = [*base, "--arm", name] + sum([[x if x.startswith("--engine-") else f"--engine-arg={x}"] for x in extra], [])
        print(f"\n=== launch {i + 1}/{len(plan)}: {name}" + (f"  (round {rnd} of {rounds})" if rounds > 1 else "") + " ===")
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
        if i + 1 < len(plan):                      # let the engine's process and its VRAM actually go before the next load
            print("  waiting 20 s for the GPU to be free again ...", flush=True)
            time.sleep(20)

    if not done:
        print("\nNo arm produced a result: nothing to compare.  The logs above say why.", file=sys.stderr)
        return 1
    if rounds > 1:
        # several launches per arm: the per-launch table, then each launch against its own round's reference, with the noise verdict
        print(f"\n=== summary of {len(done)} launch(es) in {parent} ===")
        subprocess.call([sys.executable, str(ROOT / "tools" / "summarize_ab_sweep.py"), str(parent)])
        if a.noise_floor:
            print("\nThe noise floor is the spread of the WARM launches above (the first launch warms the machine); give this folder to "
                  "tools/summarize_ab_sweep.py --noise to put a verdict on another sweep.")
    else:
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
