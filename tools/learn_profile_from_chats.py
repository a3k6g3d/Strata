"""tools/learn_profile_from_chats.py - learn an expert profile from the chats you actually have.

The engine fills its GPU cache and chooses the experts it pins in RAM from a profile (`--expert-profile`).  The shipped one is built from
generic prompts; one learned on your own work ranks the experts you really need (docs/LEARN_A_PROFILE.md: +11% decode and 56% fewer file
reads on agent chats, no loss on general prompts when general prompts are in the mix).

What this does: it turns your saved chats (the web app's session files) into replay prompts, replays a share of them against a RUNNING
server whose config has "expert_profile_save": "<file>", and unloads the model so the engine writes the profile.

    python tools/learn_profile_from_chats.py --sessions PATH/TO/sessions --count 120 --held-out held.json
    python tools/learn_profile_from_chats.py --server http://127.0.0.1:8080 --measure held.json --log strata.log   # compare profiles

Nothing leaves your PC: the prompts go to your own server, and a profile is only a ranking of expert numbers.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import sys
import time
import urllib.request

CHARS = 10_500            # about 3,000 tokens of the newest history
CUT = 1_200               # a message or tool result longer than this is cut in the middle
HERE = os.path.dirname(os.path.abspath(__file__))


def cut(text, n=CUT):
    text = (text or "").strip()
    return text if len(text) <= n else text[: n // 2] + "\n[... cut ...]\n" + text[-n // 2:]


def expand(m):
    """One saved message -> chat messages.  Every tool round is a separate request to the model, so an assistant message with tool
    calls becomes call / result pairs: the call as the assistant's text, the result as the next user message."""
    if m.get("role") == "user":
        return [{"role": "user", "content": cut(m.get("text"))}]
    out, lead = [], (m.get("text") or "").strip()
    for t in m.get("tools") or []:
        a = t.get("arguments")
        a = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
        out.append({"role": "assistant", "content": (cut(lead, 600) + "\n" if lead else "") + f"[tool {t.get('name')}] {cut(a, 500)}"})
        out.append({"role": "user", "content": f"[result] {cut(str(t.get('result') or ''), 700)}"})
        lead = ""
    if not out and lead:
        out.append({"role": "assistant", "content": cut(lead)})
    return out


def session_turns(data: dict) -> list:
    """The replay prompts of one saved chat: for every user message (a real one or a tool result) the conversation up to it, newest ~3K tokens."""
    flat = []
    for m in data.get("messages") or []:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
            flat += expand(m)
    turns = []
    for j, x in enumerate(flat):
        if x["role"] != "user" or j == 0:
            continue
        hist, size = [], 0
        for y in reversed(flat[: j + 1]):
            size += len(y["content"])
            if size > CHARS and hist:
                break
            hist.append(y)
        hist.reverse()
        while hist and hist[0]["role"] == "assistant":
            hist = hist[1:]
        if len(hist) >= 3:
            turns.append(hist)
    return turns


def load_turns(folder: str) -> list:
    turns = []
    for f in sorted(glob.glob(os.path.join(folder, "s-*.json"))):
        if f.endswith(".meta.json"):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                turns += session_turns(json.load(fh))
        except (OSError, ValueError):
            continue
    return turns


def split_turns(turns: list, count: int, held: int, seed: int = 11) -> tuple:
    """TRAIN from the first 70% of the turns (sampled), HELD from the last 25% (spread out): a held prompt never follows a trained one."""
    n = len(turns)
    train = random.Random(seed).sample(turns[: int(n * 0.70)], min(count, int(n * 0.70)))
    tail = turns[int(n * 0.75):]
    step = max(1, len(tail) // max(1, held))
    return train, tail[::step][:held]


def generic_prompts(path: str | None) -> list:
    """Plain prompts to put in the mix (so the profile does not forget ordinary use): the repo's 45-prompt corpus by default."""
    path = path or os.path.join(HERE, "..", "bench", "profile_corpus.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [[{"role": "user", "content": x["p"] if isinstance(x, dict) else str(x)}] for x in data]


def ask(server: str, key: str, messages: list, max_new: int, temperature: float) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    body = json.dumps({"messages": messages, "max_tokens": max_new, "temperature": temperature, "strata_checkpoint": False}).encode()
    return json.load(urllib.request.urlopen(urllib.request.Request(server.rstrip("/") + "/v1/chat/completions", body, headers), timeout=1800))


def file_blobs(log: str):
    last = None
    with open(log, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            m = re.search(r"expert tiers: GPU (\d+) hits this request; since the start RAM (\d+) blobs, files (\d+) blobs", line)
            if m:
                last = int(m.group(3))
    return last


def measure(server: str, key: str, prompts: list, log: str | None, passes: int = 3) -> dict:
    """Replay held prompts (temperature 0): decode tok/s from the engine's timings and, with the engine log, expert blobs read from files per token.
    The first pass is dropped (the GPU cache is still learning)."""
    rows = []
    for p in range(passes):
        toks = secs = 0.0
        before = file_blobs(log) if log else None
        n_tokens = 0
        for messages in prompts:
            r = ask(server, key, messages, 160, 0)
            tm = r.get("timings") or {}
            k = r["usage"]["completion_tokens"]
            n_tokens += k
            if tm.get("predicted_per_second"):
                toks, secs = toks + k, secs + k / tm["predicted_per_second"]
        time.sleep(1.5)
        after = file_blobs(log) if log else None
        rows.append({"tok_s": round(toks / secs, 2) if secs else None,
                     "reads_per_token": round((after - before) / max(1, n_tokens), 2) if before is not None and after is not None and after >= before else None})
        print(f"pass {p + 1}: {rows[-1]}", flush=True)
    good = [r for r in rows[1:] if r["tok_s"]]
    return {"tok_s": round(sum(r["tok_s"] for r in good) / len(good), 2) if good else None,
            "reads_per_token": (round(sum(r["reads_per_token"] for r in good if r["reads_per_token"] is not None) / max(1, len([r for r in good if r["reads_per_token"] is not None])), 2)
                                if any(r["reads_per_token"] is not None for r in good) else None)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--server", default="http://127.0.0.1:8080")
    ap.add_argument("--api-key", default=os.environ.get("STRATA_API_KEY", ""))
    ap.add_argument("--sessions", help="the folder of saved chats (s-*.json); needed to learn")
    ap.add_argument("--count", type=int, default=120, help="how many chat prompts to replay (default 120)")
    ap.add_argument("--generic", type=int, default=45, help="how many general prompts to mix in (0 = none; default 45)")
    ap.add_argument("--generic-file", help="a JSON list of prompts for the mix (default: bench/profile_corpus.json)")
    ap.add_argument("--max-new", type=int, default=160)
    ap.add_argument("--held-out", help="write the held-out prompts here, for --measure later")
    ap.add_argument("--measure", metavar="FILE", help="replay a held-out file and print decode tok/s (and reads per token with --log)")
    ap.add_argument("--log", help="the engine log (the server's `log` file), for reads per token")
    ap.add_argument("--no-unload", action="store_true", help="do not unload the model at the end (the profile is written when the engine quits)")
    a = ap.parse_args(argv)

    if a.measure:
        with open(a.measure, encoding="utf-8") as fh:
            prompts = json.load(fh)
        print(json.dumps(measure(a.server, a.api_key, prompts, a.log)))
        return 0
    if not a.sessions:
        ap.error("--sessions is required to learn a profile")
    turns = load_turns(a.sessions)
    if len(turns) < 20:
        print(f"only {len(turns)} usable turns in {a.sessions}: too few to learn from", file=sys.stderr)
        return 1
    train, held = split_turns(turns, a.count, 24)
    mix = [{"messages": t} for t in train] + [{"messages": g} for g in generic_prompts(a.generic_file)[: a.generic]]
    random.Random(3).shuffle(mix)
    if a.held_out:
        with open(a.held_out, "w", encoding="utf-8") as fh:
            json.dump(held, fh)
        print(f"wrote {len(held)} held-out prompts to {a.held_out}")
    print(f"replaying {len(mix)} prompts ({len(train)} from your chats, {len(mix) - len(train)} general) against {a.server} ...", flush=True)
    for i, item in enumerate(mix):
        try:
            ask(a.server, a.api_key, item["messages"], a.max_new, 0.6)
        except Exception as e:  # noqa: BLE001 - one failed prompt must not end a long run
            print(f"prompt {i}: {e}", file=sys.stderr)
        if i % 25 == 24:
            print(f"  {i + 1} of {len(mix)}", flush=True)
    if not a.no_unload:
        req = urllib.request.Request(a.server.rstrip("/") + "/unload", b"{}", {"Content-Type": "application/json"})
        print("unloading the model so the engine writes the profile:", json.load(urllib.request.urlopen(req, timeout=300)))
    print("done: the profile is the file named by \"expert_profile_save\" in your config; use it with --expert-profile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
