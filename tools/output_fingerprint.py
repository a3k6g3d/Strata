"""Fingerprint what the model actually said, and check that two arms say the same thing.

    python tools/output_fingerprint.py --url http://127.0.0.1:8080 [--arm-b http://127.0.0.1:8081]
    python tools/output_fingerprint.py --url http://127.0.0.1:8080 --runs 8 --seed 1234 --out fingerprints.json
    python tools/output_fingerprint.py --selftest

Same prompt, same seed, greedy, N runs.  The answer's sha256 is the fingerprint.  The prompts are the A/B
harness's own fixed prompts (`tools/ab_engine.py`), so this checks the same requests the sweeps make; with two
arms they run interleaved, because the machine drifts on a schedule and arms that run in list order read that
schedule as a result (BENCH_NOISE.md item 1).

Two things this measured on this machine before it could be written (2026-10-19, RTX 5070, engine 0.1.45), and
both are baked into what it reports:

* **The existing engine is not byte-identical to itself** on open-ended generation - the same prompt, the same
  seed, temperature 0, three runs, three different answers - while a short exact-answer prompt was identical
  five times out of five.  So "byte-identical output" is only checkable on prompts the existing engine
  reproduces on its own, and this tool names which ones those are instead of assuming it.
* **The first run of a prompt is a different computation from the later ones** (the conversation cache makes run
  2 read what run 1 wrote).  Runs are therefore split cold/warm and only like is compared with like.

Seed must be > 0 to reach the engine at all (`serve/server.py`: a seed <= 0 is unset), and temperature 0 is not
forwarded because it means the engine's default, which is greedy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
import ab_engine as A  # noqa: E402  the fixed prompts, so this checks the same requests the sweeps make

STABLE, WARM_ONLY, UNSTABLE = "stable", "stable once warm", "unstable"
MAX_TOKENS = 64


# ------------------------------------------------------------------------------------------------------------- http
def ask(url: str, key: str, prompt: str, seed: int, max_tokens: int = MAX_TOKENS, timeout: float = 1800) -> str:
    body = {"model": "strata", "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
            "temperature": 0, "seed": seed, "stream": False, "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + key} if key else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read())
    return ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


# ---------------------------------------------------------------------------------------------------- the parts under test
def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def classify(runs: list) -> dict:
    """One arm's runs of one prompt -> a verdict.

    `runs` is [{"run", "cold", "hash", "err"}].  An errored run never votes: it is counted and named, because a
    run that failed and returned the empty string would otherwise agree with every other failure and the tool
    would report a clean parity out of nothing.
    """
    live = [r for r in runs if not r.get("err")]
    warm = [r for r in live if not r.get("cold")]
    cold = [r for r in live if r.get("cold")]
    out = {"runs": len(runs), "errors": len(runs) - len(live), "verdict": UNSTABLE, "hash": None,
           "distinct": len({r["hash"] for r in live})}
    if not live:
        out["verdict"] = None                                   # nothing to judge on
        return out
    if len({r["hash"] for r in live}) == 1:
        out["verdict"], out["hash"] = STABLE, live[0]["hash"]
    elif warm and len({r["hash"] for r in warm}) == 1 and len(warm) > 1:
        out["verdict"], out["hash"] = WARM_ONLY, warm[0]["hash"]
    elif cold and warm and len({r["hash"] for r in cold}) == 1 and len({r["hash"] for r in warm}) == 1 and len(warm) == 1:
        out["verdict"] = UNSTABLE                               # one cold, one warm: nothing to conclude
    if out["hash"] is None and out["verdict"] == UNSTABLE and live:
        top = max({r["hash"] for r in live}, key=lambda h: sum(r["hash"] == h for r in live))
        out["hash"] = top                                       # the majority, for the report; not a parity hash
    return out


def compare(per_prompt: dict) -> list:
    """Two arms' verdicts per prompt -> rows.  Only a prompt both arms can reproduce is compared at all."""
    rows = []
    for prompt, (a, b) in sorted(per_prompt.items()):
        if a["verdict"] in (None, UNSTABLE) or b["verdict"] in (None, UNSTABLE):
            rows.append({"prompt": prompt, "verdict": "not comparable",
                         "why": f"arm A {a['verdict'] or 'has no live run'}, arm B {b['verdict'] or 'has no live run'}"})
        else:
            rows.append({"prompt": prompt, "verdict": "match" if a["hash"] == b["hash"] else "mismatch",
                         "why": f"{a['verdict']} / {b['verdict']}"})
    return rows


# ------------------------------------------------------------------------------------------------------------- report
def report(arms: list, runs_per_prompt: int, seed: int, rows: list) -> tuple:
    """(the table, the exit code).  With one arm the question is whether the engine reproduces itself; with two,
    whether they agree - and only on prompts where the first question has already been answered yes."""
    lines = [f"{runs_per_prompt} runs, seed {seed}, greedy, arms interleaved", ""]
    ok = True
    for name, per in arms:
        lines.append(f"arm {name}")
        for prompt in sorted(per):
            v = per[prompt]
            mark = {"stable": "yes", "stable once warm": "warm only", "unstable": "NO"}.get(v["verdict"], "no data")
            if v["verdict"] in (None, UNSTABLE):
                ok = False
            lines.append(f"  {v['runs']:>3} runs, {v['distinct']} distinct answer(s), errors {v['errors']}, "
                         f"reproduces itself: {mark:<10} {(v['hash'] or '')[:12]}  {prompt[:44]}")
        lines.append("")
    if len(arms) > 1:
        lines.append("arm A vs arm B")
        for r in rows:
            if r["verdict"] != "match":
                ok = False
            lines.append(f"  {r['verdict']:<15} {r['why']:<32} {r['prompt'][:40]}")
        lines.append("")
    return "\n".join(lines), ok


def selftest() -> int:
    """This tool's own arithmetic, on answers we made up.  No server, no GPU."""
    checks = []

    def check(what, got, want):
        checks.append((what, got == want, f"got {got!r}, want {want!r}"))

    a, b = "The capital of France is Paris.", "The capital of France is Paris!"
    check("the same answer fingerprints the same", fingerprint(a), fingerprint(a))
    check("one character changes it", fingerprint(a) != fingerprint(b), True)
    check("an empty answer still gets a fingerprint", len(fingerprint("")) == 64, True)

    def runs(hashes, errs=(), cold_first=True):
        return [{"run": i, "cold": cold_first and i == 0, "hash": fingerprint(h), "err": None}
                for i, h in enumerate(hashes)] + [{"run": "x", "cold": False, "hash": fingerprint(""), "err": e}
                                                  for e in errs]

    h1, h2, h3 = fingerprint(a), fingerprint(b), fingerprint("Paris is the capital of France.")
    check("all agreeing is stable", classify(runs([a, a, a]))["verdict"], STABLE)
    check("a cold run may differ; the warm ones agreeing is 'stable once warm'",
          classify(runs([a, b, b, b]))["verdict"], WARM_ONLY)
    check("warm runs disagreeing is unstable", classify(runs([a, a, b, a]))["verdict"], UNSTABLE)
    check("one cold and one warm is nothing to conclude", classify(runs([a, b]))["verdict"], UNSTABLE)
    v = classify(runs([a, a], errs=["boom"]))
    check("an errored run does not vote, and is named", (v["verdict"], v["errors"], v["runs"]), (STABLE, 1, 3))
    check("all runs errored is no data, not a match", classify(runs([], errs=["boom"]))["verdict"], None)
    check("the majority hash is reported, not hidden", classify(runs([a, b, b]))["hash"], fingerprint(b))

    stable_a, stable_b = {"verdict": STABLE, "hash": h1, "errors": 0, "runs": 3, "distinct": 1}, \
                         {"verdict": STABLE, "hash": h1, "errors": 0, "runs": 3, "distinct": 1}
    other = dict(stable_b, hash=h2)
    shaky = {"verdict": UNSTABLE, "hash": h1, "errors": 0, "runs": 3, "distinct": 2}
    rows = {"agrees": (stable_a, stable_b), "differs": (stable_a, other),
            "engine cannot reproduce it": (shaky, stable_b)}
    got = {r["verdict"] for r in compare(rows)}
    check("the gate is not vacuous: a real difference is reported as a mismatch", "mismatch" in got, True)
    check("agreement is reported as a match", "match" in got, True)
    check("a prompt the engine cannot reproduce is not compared", "not comparable" in got, True)

    failed = 0
    for what, good, why in checks:
        failed += not good
        print(f"  {'ok  ' if good else 'FAIL'} {what}" + ("" if good else f"  ({why})"))
    print(f"\n{len(checks) - failed} of {len(checks)} passed")
    return 1 if failed else 0


# ---------------------------------------------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.splitlines()[2:]))
    ap.add_argument("--url", help="the existing engine (a running Strata server)")
    ap.add_argument("--arm-b", help="the Rust host, if there is one yet; without it this only asks whether the "
                                    "existing engine reproduces itself")
    ap.add_argument("--key", default="", help="the server's API key, if it has one")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--seed", type=int, default=1234, help="must be > 0 to reach the engine")
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    ap.add_argument("--out", help="write the per-request fingerprints here (JSON)")
    ap.add_argument("--selftest", action="store_true", help="check this tool's own arithmetic on answers we made up, and stop")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.url:
        print("give --url of a running server (or --selftest).", file=sys.stderr)
        return 2

    arms = [("A", a.url)] + ([("B", a.arm_b)] if a.arm_b else [])
    prompts = {f"chat{i}": p for i, p in enumerate(A.PROMPTS)}
    seen = {name: {p: [] for p in prompts} for name, _ in arms}
    for run in range(a.runs):
        order = arms if run % 2 == 0 else arms[::-1]            # the machine drifts on a schedule; interleave
        for name, url in order:
            for tag, prompt in prompts.items():
                cold = not seen[name][tag]                      # the first of a prompt is the cold one
                try:
                    text, err = ask(url, a.key, prompt, a.seed, a.max_tokens), None
                except Exception as e:  # noqa: BLE001
                    text, err = "", repr(e)[:200]
                seen[name][tag].append({"run": run, "cold": cold, "prompt": tag,
                                        "hash": fingerprint(text), "err": err,
                                        "chars": len(text), "at": round(time.time(), 3)})
                print(f"  {name} run {run} {'cold' if cold else 'warm':<4} {tag:<6} "
                      f"{(seen[name][tag][-1]['hash'])[:12]} {err or f'{len(text)} chars'}", flush=True)

    per = {name: {tag: classify(v) for tag, v in d.items()} for name, d in seen.items()}
    rows = compare({tag: (per[name[0]][tag], per[name[1]][tag]) for tag in prompts}
                   ) if len(arms) > 1 else []
    table, ok = report([(name, per[name]) for name, _ in arms], a.runs, a.seed, rows)
    print("\n" + table)
    if a.out:
        Path(a.out).write_text(json.dumps({"seed": a.seed, "runs": a.runs, "max_tokens": a.max_tokens,
                                           "arms": [{"name": n, "url": u, "per_prompt": per[n]} for n, u in arms],
                                           "requests": {n: [r for v in seen[n].values() for r in v] for n, _ in arms},
                                           "comparison": rows, "ok": ok}, indent=1), encoding="utf-8")
        print(f"fingerprints: {a.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
