"""tools/bench_record.py - one JSON line per benchmark run.

A run used to leave a results folder (results.json, README.md, a log excerpt) and every comparison afterwards was a regex over prose.  This writes the
same run as ONE flat line in `runs.jsonl` next to the folders, with what a comparison needs: the arm, the launch line, when it started and how long
the PC had been up, what the machine was doing (tools/bench_state.py), the speeds, the expert-cache and draft figures from the engine's own records,
and every turn's decode speed.  A summary is then a read of one file.

    runs.jsonl          appended to by tools/strata_bench.py after each run (next to the results folders)
    read_records(path)  the records, malformed lines skipped
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import statistics
from pathlib import Path

SCHEMA = 1


def _med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 2) if xs else None


def state_hash_summary(lines):
    """STATE_HASH lines (the engine prints them with STRATA_STATE_HASH=1) from a log excerpt -> how many, and a digest of their fields."""
    found = [l for l in (lines or []) if "STATE_HASH L=" in l]
    if not found:
        return None
    fields = [",".join(f"{k}={v}" for k, v in re.findall(r"(\w+)=([0-9a-f,-]+)", l)) for l in found]
    return {"lines": len(found), "digest": hashlib.sha1("\n".join(fields).encode()).hexdigest()[:16]}


def make_record(res: dict, folder: str | None = None, log_lines=None, now: float | None = None, state_lines=None) -> dict:
    """The flat record of one run, from the `res` dict strata_bench.py writes as results.json."""
    machine, params, results = res.get("machine") or {}, res.get("params") or {}, res.get("results") or {}
    agent = results.get("agent") or {}
    turns = [t for s in agent.get("sessions") or [] for t in s if t.get("ok")]
    engine = results.get("engine") or {}
    a = (engine.get("per_test") or {}).get("agent session") or {}
    whole = engine.get("whole_suite") or {}
    state = res.get("state") or {}
    chat = results.get("chat") or {}                                    # a run without an agent session still has the chat requests' figures
    chat_decode, chat_ttft = (chat.get("decode_tps") or {}).get("median"), (chat.get("ttft_s") or {}).get("median")
    agent_decode = _med([t.get("decode_tps") for t in turns])
    import time
    end = now if now is not None else time.time()
    start = end - float(res.get("elapsed_s") or 0)
    start_state = state.get("start") or {}
    return {"schema": SCHEMA,
            "start": datetime.datetime.fromtimestamp(start).isoformat(timespec="seconds"), "start_epoch": round(start, 1),
            "elapsed_s": round(float(res.get("elapsed_s") or 0), 1),
            "arm": params.get("arm") or "", "engine_arg": params.get("engine_arg") or [], "launch": machine.get("launch") or "",
            "engine_version": machine.get("engine_version"), "strata_commit": machine.get("strata_commit"),
            "uptime_s_at_start": start_state.get("uptime_s"), "state": {"start": start_state or None, "end": state.get("end"), "during": state.get("during")},
            "decode_tps_median": agent_decode if turns else chat_decode, "ttft_s_median": _med([t.get("ttft_s") for t in turns]) if turns else chat_ttft,
            "decode_source": "agent" if turns else ("chat" if chat_decode is not None else None),
            "engine_decode_tok_s": a.get("decode_tok_s_median"), "hit_pct": a.get("cache_hit_pct") if a else whole.get("cache_hit_pct"),
            "pcie_share_pct": a.get("pcie_share_pct") if a else whole.get("pcie_share_pct"),
            "prompt_reuse_pct": a.get("prompt_reuse_pct") if a else whole.get("prompt_reuse_pct"),
            "draft_accept_pct": a.get("draft_accept_pct") if a else whole.get("draft_accept_pct"),
            "turns": [{"turn": t.get("turn"), "decode_tps": t.get("decode_tps"), "ttft_s": t.get("ttft_s")} for t in turns],
            "state_hash": state_hash_summary(state_lines if state_lines is not None else log_lines), "folder": folder}


def append(path, record: dict) -> None:
    """One line, appended; the folder is made if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()


def read_records(path) -> list:
    out = []
    p = Path(path)
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
