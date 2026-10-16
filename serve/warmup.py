"""Opt-in: a few throwaway requests right after the model has loaded ("warmup" in the config).

The engine's GPU expert cache starts from the expert profile and then adapts to what is asked.  Measured on this PC the first answers
after a start ran at about half speed (13.9 tok/s on the first request, 18.8, 20.5, 21.8 on the next three, the cache hit rate rising from
31% to 58%).  Sending a handful of short, typical requests while nobody is waiting moves that learning before the first real question.

    "warmup": {"enabled": true, "max_tokens": 100, "prompts": ["...", "..."]}      (prompts optional: a built-in mix is used)

The requests are ordinary chat requests to this server, sent one after another from a background thread once /v1/status says the model is
loaded; they are not saved as chats, and a real request that arrives meanwhile simply waits its turn.  Off unless "enabled" is true.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

DEFAULT_PROMPTS = [
    "Write a Python function that parses a log line like '2026-10-15 12:00:01 worker-3 job 42 ok in 120 ms' into a dict, with type hints.",
    "Here is a tool result:\n[result] exit code 0 (1.2 s) --- stdout --- 3 files changed, 41 insertions(+), 7 deletions(-)\n\nSummarize what happened in one sentence and name the next step.",
    "Explain in a short paragraph why the sky is blue.",
    "Fix the bug: `for i in range(len(items)): if items[i] == target: return i` returns None when the item is missing; it should return -1. Show the corrected function.",
    "Give me three tips for planning a two-day trip to Lisbon.",
]


def settings(cfg) -> dict | None:
    """The validated warm-up block, or None when it is off or malformed."""
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return None
    prompts = cfg.get("prompts")
    if not (isinstance(prompts, list) and prompts and all(isinstance(p, str) and p.strip() for p in prompts)):
        prompts = DEFAULT_PROMPTS
    tokens = cfg.get("max_tokens")
    return {"prompts": prompts[:12], "max_tokens": tokens if isinstance(tokens, int) and 16 <= tokens <= 512 else 100}


def _loaded(base: str, headers: dict) -> bool:
    try:
        req = urllib.request.Request(base + "/v1/status", headers=headers)
        return bool(json.load(urllib.request.urlopen(req, timeout=5)).get("loaded"))
    except Exception:  # noqa: BLE001 - the server is not answering yet
        return False


def run(cfg: dict, base: str, api_key: str = "", wait_s: float = 1800.0, poll_s: float = 5.0) -> int:
    """Wait for the model, then send the prompts; returns how many were answered."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    end = time.monotonic() + wait_s
    while not _loaded(base, headers):
        if time.monotonic() > end:
            return 0
        time.sleep(poll_s)
    time.sleep(3)
    t0, done = time.monotonic(), 0
    for prompt in cfg["prompts"]:
        body = json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": cfg["max_tokens"], "temperature": 0.6,
                           "strata_checkpoint": False}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/v1/chat/completions", body, headers), timeout=600).read()
            done += 1
        except Exception:  # noqa: BLE001 - a failed warm-up request is not worth more than a log line
            break
    print(f"[strata] warm-up: {done} of {len(cfg['prompts'])} requests in {time.monotonic() - t0:.0f} s", flush=True)
    return done


def start(cfg, base: str, api_key: str = ""):
    """Called by serve/server.py once the server answers.  Returns the thread, or None when the warm-up is not set up."""
    s = settings(cfg)
    if s is None:
        return None
    t = threading.Thread(target=run, args=(s, base, api_key), name="warmup", daemon=True)
    t.start()
    return t
