#!/usr/bin/env python3
"""One command that measures this Strata install the way it is used, and writes a report you can share.

    BENCH.bat                      (Windows)        ./bench.sh        (Linux)        python tools/strata_bench.py

It starts the server with your existing settings (the newest strata-*.json of this folder, or --config FILE), or uses
the one already running (it checks the config's port, or --url http://host:port), and sends a fixed suite through the
real HTTP API, three runs each, medians reported:

  1. chat       three short questions (about 100 tokens out): decode tok/s and time to first token
  2. 4K prompt  a prompt of about 4,096 tokens: prompt tok/s and time to first token (fresh every run: no cache help)
  3. 16K prompt the same at about 16,384 tokens (skipped when the context is smaller)
  4. agent      a coding-agent style session: system prompt + four tools, then five turns, each adding about 1,500 words
                of a file to the conversation and continuing from the model's own earlier replies - how many prompt
                tokens the conversation cache reused, how many had to be read again, time to first token per turn
  5. --clients N   N requests at once (the server needs --batch for them to run together): total tok/s, p50/p95 latency
  6. --long 32K    the same prompt test at about 32,768 tokens

Speeds are the engine's own timings (the `timings` of the answer: prompt_per_second, predicted_per_second); time to first
token and latency are measured by this client.  Thinking is off for the suite (--think turns it on), so the answers are
short and the runs comparable.  The default run takes about 3-10 minutes after the model has loaded (the slower the card, the longer).

The result goes to bench/results/<date>-community-<gpu>-<model>/ : README.md in the format of
docs/COMMUNITY_BENCHMARKS.md, results.json (every request) and an engine log excerpt.  Home folders, user and host names,
IP addresses and API keys are replaced by placeholders before anything is written; read it once before you share it.
`--compare <older results folder>` prints the two side by side.  Standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ab_engine import PROMPTS, long_prompt          # noqa: E402  the same three short questions and word list
from agent_turns_bench import SYSTEM, TOOLS         # noqa: E402  the same agent system prompt and tool list
import serve_load                                   # noqa: E402  the burst client for --clients

SCHEMA = 1

try:                       # what the machine was doing, and one JSON line per run (tools/bench_state.py, tools/bench_record.py)
    import bench_state
    import bench_record
except ImportError:
    bench_state = bench_record = None
CHAT_MAX_TOKENS = 100
LONG_MAX_TOKENS = 24
AGENT_MAX_TOKENS = 64
AGENT_TURNS = 5
AGENT_WORDS = 1500
AGENT_MIN_CONTEXT = 10240      # the last turn of the session holds about 10K tokens
BURST_MAX_TOKENS = 128


# --------------------------------------------------------------------------------------------------------- redaction
def redactor(extra: list[str] = (), key: str = ""):
    """-> f(text) -> text with this machine's home folders, user and host names, the Strata folder, IP addresses and the
    API key replaced.  Applied to everything that is written or printed from the server's or the machine's own text."""
    subs: list[tuple[re.Pattern, str]] = []

    def add(value, label, word=False):
        if value and len(str(value)) >= 4:
            v = str(value)
            for form in sorted({v, v.replace("\\", "/"), v.replace("/", "\\")}, key=len, reverse=True):
                pat = re.escape(form)
                subs.append((re.compile(r"(?<![\w-])" + pat + r"(?![\w-])" if word else pat, re.I), label))

    add(ROOT, "<strata>")
    add(Path.home(), "<home>")
    add(tempfile.gettempdir(), "<tmp>")
    try:
        add(getpass.getuser(), "<user>", True)
    except Exception:                                   # noqa: BLE001
        pass
    add(socket.gethostname(), "<host>", True)
    add(platform.node(), "<host>", True)
    for x in extra:
        add(x, "<private>", True)
    add(key, "<key>")
    subs.sort(key=lambda p: -len(p[0].pattern))
    path_user = re.compile(r"(?i)([A-Za-z]:)?([\\/])(Users|home)([\\/])(?!<)[^\\/\s\"':;,]+")
    ip4 = re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])|169\.254)\.\d{1,3}\.\d{1,3}\b")
    bearer = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")

    def f(text):
        if not isinstance(text, str):
            return text
        for pat, label in subs:
            text = pat.sub(label, text)
        text = path_user.sub(lambda m: f"{m.group(1) or ''}{m.group(2)}{m.group(3)}{m.group(4)}<user>", text)
        text = bearer.sub(r"\1<key>", text)
        return ip4.sub("<ip>", text)
    return f


def redact_obj(obj, red):
    if isinstance(obj, dict):
        return {red(k) if isinstance(k, str) else k: redact_obj(v, red) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, red) for v in obj]
    return red(obj) if isinstance(obj, str) else obj


# ---------------------------------------------------------------------------------------------------------- HTTP
def http_json(base: str, path: str, key: str = "", timeout: float = 20):
    req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + key} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def try_json(base, path, key="", timeout=20):
    try:
        return http_json(base, path, key, timeout)
    except Exception:                                   # noqa: BLE001
        return None


def chat(base: str, key: str, messages: list, max_tokens: int, think: bool = False, tools=None, timeout: float = 3600) -> dict:
    """One streamed chat request -> {first, total, text, usage, timings, err}.  `first` is the seconds to the first
    non-empty content or reasoning piece (keep-alives and empty deltas ignored)."""
    body = {"model": "strata", "stream": True, "max_tokens": max_tokens, "temperature": 0, "messages": messages}
    if tools:
        body["tools"] = tools
    if not think:
        body["reasoning_effort"] = "none"
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + key} if key else {})})
    t0 = time.perf_counter()
    first, text, usage, timings, err, pieces = None, [], None, None, None, 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                d = line[5:].strip()
                if d == "[DONE]":
                    break
                try:
                    j = json.loads(d)
                except ValueError:
                    continue
                if j.get("error"):
                    err = str(j["error"])[:300]
                for ch in j.get("choices") or []:
                    dl = ch.get("delta") or {}
                    piece = (dl.get("content") or "") + (dl.get("reasoning_content") or "")
                    if piece:
                        if first is None:
                            first = time.perf_counter() - t0
                        pieces += 1
                        text.append(dl.get("content") or "")
                if j.get("usage"):
                    usage = j["usage"]
                if j.get("timings"):
                    timings = j["timings"]
    except urllib.error.HTTPError as e:
        err = f"HTTP {e.code}: {e.read()[:200]!r}"
    except Exception as e:                              # noqa: BLE001
        err = repr(e)[:300]
    total = time.perf_counter() - t0
    return {"first": first, "total": total, "text": "".join(text), "usage": usage, "timings": timings, "err": err,
            "pieces": pieces}


def digest(r: dict) -> dict:
    """The numbers of one request, the engine's own where it reported them."""
    u, t = r.get("usage") or {}, r.get("timings") or {}
    gen = u.get("completion_tokens") or r.get("pieces") or 0
    prompt_tokens = u.get("prompt_tokens")
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is None:
        cached = t.get("cache_n")
    read = t.get("prompt_n")
    if read is None and prompt_tokens is not None and cached is not None:
        read = prompt_tokens - cached
    dec, dsrc = t.get("predicted_per_second"), "engine"
    if dec is None and r.get("first") is not None and gen > 1 and r["total"] > r["first"]:
        dec, dsrc = round((gen - 1) / (r["total"] - r["first"]), 1), "client"
    pp, psrc = t.get("prompt_per_second"), "engine"
    if pp is None:
        pp, psrc = None, None
    return {"ok": r.get("err") is None and r.get("first") is not None,
            "err": r.get("err"), "ttft_s": None if r.get("first") is None else round(r["first"], 3),
            "total_s": round(r["total"], 3), "prompt_tokens": prompt_tokens, "reused_tokens": cached,
            "read_tokens": read, "generated_tokens": gen, "prompt_tps": pp, "prompt_tps_source": psrc,
            "decode_tps": dec, "decode_tps_source": dsrc if dec is not None else None,
            "prompt_ms": t.get("prompt_ms"), "decode_ms": t.get("predicted_ms"),
            "drafts_offered": t.get("draft_n"), "drafts_accepted": t.get("draft_n_accepted"),
            # BENCH_NOISE.md item 5: the speed numbers need something to be checked against.  The answer's own hash is
            # the only thing in a results folder that can show two arms said the same thing.
            "output_sha256": hashlib.sha256((r.get("text") or "").encode("utf-8")).hexdigest()}


# ----------------------------------------------------------------------------------------------------- statistics
def stat(values) -> dict | None:
    v = [x for x in values if isinstance(x, (int, float))]
    if not v:
        return None
    return {"median": round(statistics.median(v), 3), "min": round(min(v), 3), "max": round(max(v), 3), "n": len(v)}


def med(values):
    """The middle number of a list, or None when there is nothing to take a median of."""
    v = [x for x in values if isinstance(x, (int, float))]
    return round(statistics.median(v), 3) if v else None


def fmt(s, digits=1, unit="") -> str:
    if not s:
        return "not measured"
    d = digits
    return f"{s['median']:.{d}f}{unit} ({s['min']:.{d}f}-{s['max']:.{d}f})"


def pctl(values, p):
    return serve_load.pct(values, p)


# ---------------------------------------------------------------------------------------------------- the suite
class Suite:
    def __init__(self, base, key, ctx, runs, think, log=print, want_clients=0, want_long=False, quick=False, tests=None):
        self.base, self.key, self.ctx, self.runs, self.think, self.log = base, key, ctx, runs, think, log
        self.clients, self.want_long = want_clients, want_long
        self.quick = quick
        self.nonce = str(int(time.time()))
        self.tokens_per_word, self.overhead = 1.1, 40
        self.tests = tests or {"chat", "4k", "16k", "agent"}
        self.out: dict = {"chat": {}, "prompts": {}, "agent": {}, "clients": {}, "skipped": {}, "failures": [],
                          "phases": []}

    @contextlib.contextmanager
    def phase(self, name: str):
        """Mark the seconds one test occupies, so the engine's own per-request cache and draft figures (which carry no
        bench tag) can be put back against this test afterwards by their own timestamp."""
        span = {"name": name, "from_s": time.time()}
        try:
            yield span
        finally:
            span["to_s"] = time.time()
            self.out["phases"].append(span)

    def tag(self, *parts) -> str:
        return "-".join([*map(str, parts), self.nonce])

    def fail(self, where, d):
        self.out["failures"].append({"where": where, "error": d.get("err") or "no token came back"})
        self.log(f"    FAILED: {d.get('err') or 'no token came back'}")

    def calibrate(self):
        """Words that make a prompt of a wanted size: two tiny requests (200 and 800 words) give the tokens per word as a
        slope, so the template's fixed part does not skew it."""
        pts = []
        for words in (200, 800):
            r = digest(chat(self.base, self.key, [{"role": "user", "content": f"[{self.tag('cal', words)}]\n" + long_prompt(words)}], 1))
            pts.append(r.get("prompt_tokens"))
        slope = (pts[1] - pts[0]) / 600 if all(pts) and pts[1] > pts[0] else None
        self.tokens_per_word = slope or 1.1
        self.overhead = max(0, (pts[0] or 0) - 200 * self.tokens_per_word) if slope else 40
        self.log(f"  calibrated: about {self.tokens_per_word:.2f} tokens per word")

    def prompt_for(self, tokens: int, run: int) -> list:
        words = max(50, int((tokens - self.overhead) / self.tokens_per_word))
        return [{"role": "user", "content": f"[bench {self.tag(run, tokens, 'p')}]\n" + long_prompt(words)}]

    def warm_up(self):
        self.log("warming up (not measured) ...")
        chat(self.base, self.key, [{"role": "user", "content": PROMPTS[0]}], 24, self.think)
        self.calibrate()
        chat(self.base, self.key, self.prompt_for(1024, -1), 8, self.think)

    def run_chat(self):
        self.log(f"chat: {len(PROMPTS)} short questions x {self.runs} runs, up to {CHAT_MAX_TOKENS} new tokens")
        rows = []
        with self.phase("short chat"):
            for run in range(self.runs):
                for i, p in enumerate(PROMPTS):
                    d = digest(chat(self.base, self.key, [{"role": "user", "content": f"[{self.tag(run, i, 'c')}] {p}"}],
                                    CHAT_MAX_TOKENS, self.think))
                    d.update(run=run, prompt=i)
                    rows.append(d)
                    if not d["ok"]:
                        self.fail("chat", d)
                    else:
                        self.log(f"  run {run + 1} q{i + 1}: decode {d['decode_tps']} tok/s, first token {d['ttft_s']} s, "
                                 f"{d['generated_tokens']} tokens")
        good = [r for r in rows if r["ok"]]
        self.out["chat"] = {"requests": rows, "decode_tps": stat(r["decode_tps"] for r in good),
                            "ttft_s": stat(r["ttft_s"] for r in good), "generated_tokens": stat(r["generated_tokens"] for r in good),
                            "prompt_tokens": stat(r["prompt_tokens"] for r in good)}

    def run_prompt(self, tokens: int, label: str):
        if self.ctx and tokens + LONG_MAX_TOKENS + 256 > self.ctx:
            self.out["skipped"][label] = f"the context is {self.ctx} tokens"
            self.log(f"{label} prompt: skipped (the context is {self.ctx} tokens)")
            return
        self.log(f"{label} prompt: {self.runs} fresh prompts of about {tokens} tokens, {LONG_MAX_TOKENS} new tokens")
        rows = []
        with self.phase(f"{label} prompt"):
            for run in range(self.runs):
                d = digest(chat(self.base, self.key, self.prompt_for(tokens, run), LONG_MAX_TOKENS, self.think))
                d["run"] = run
                rows.append(d)
                if not d["ok"]:
                    self.fail(label, d)
                else:
                    self.log(f"  run {run + 1}: {d['prompt_tokens']} tokens, prompt {d['prompt_tps']} tok/s "
                             f"({d['reused_tokens'] or 0} reused), first token {d['ttft_s']} s")
        good = [r for r in rows if r["ok"]]
        self.out["prompts"][label] = {"target_tokens": tokens, "requests": rows,
                                      "prompt_tps": stat(r["prompt_tps"] for r in good),
                                      "ttft_s": stat(r["ttft_s"] for r in good),
                                      "prompt_tokens": stat(r["prompt_tokens"] for r in good),
                                      "reused_tokens": stat(r["reused_tokens"] or 0 for r in good)}

    def agent_files(self) -> list[tuple[str, str]]:
        """Five pieces of text a coding agent would read: this folder's own documents when they exist (nothing personal),
        else generated filler of the same size."""
        names = []
        for rel in ("README.md", "docs/HOW_IT_WORKS.md", "docs/DETAILS.md", "docs/INSTALL.md", "docs/MODELS.md", "docs/COMMUNITY_BENCHMARKS.md"):
            p = ROOT / rel
            if p.is_file():
                words = p.read_text(encoding="utf-8", errors="replace").split()
                if len(words) >= AGENT_WORDS // 2:
                    names.append((rel, " ".join(words[:AGENT_WORDS])))
        i = 0
        while len(names) < AGENT_TURNS:
            names.append((f"generated/part{i}.txt", long_prompt(AGENT_WORDS)))
            i += 1
        return names[:AGENT_TURNS]

    def agent_session(self, run: int) -> list[dict]:
        files = self.agent_files()
        msgs = [{"role": "system", "content": f"{SYSTEM}\n(session {self.tag(run, 'a')})"},
                {"role": "user", "content": "I will show you files from a project one at a time. After each file, answer in "
                                            "one or two sentences what it is for. Do not call tools."}]
        turns = []
        for t in range(AGENT_TURNS + 1):
            if t > 0:
                rel, body = files[t - 1]
                msgs.append({"role": "user", "content": f"File {rel}:\n{body}\n\nWhat is this file for? Two sentences."})
            r = chat(self.base, self.key, msgs, AGENT_MAX_TOKENS, self.think, tools=TOOLS)
            d = digest(r)
            d.update(run=run, turn=t)
            turns.append(d)
            if not d["ok"]:
                self.fail("agent", d)
                break
            msgs.append({"role": "assistant", "content": r["text"] or "Understood."})
        return turns

    def run_agent(self):
        if self.ctx and self.ctx < AGENT_MIN_CONTEXT:
            self.out["skipped"]["agent"] = f"context {self.ctx} is below ~10K"
            self.log(f"agent: skipped: context {self.ctx} is below ~10K")
            return
        self.log(f"agent: a session of {AGENT_TURNS + 1} turns with four tools, x {self.runs} runs")
        sessions = []
        with self.phase("agent session"):
            for run in range(self.runs):
                turns = self.agent_session(run)
                sessions.append(turns)
                for d in turns:
                    if d["ok"]:
                        self.log(f"  run {run + 1} turn {d['turn']}: prompt {d['prompt_tokens']}, reused {d['reused_tokens']}, "
                                 f"read {d['read_tokens']}, first token {d['ttft_s']} s")
        follow = [[d for d in s if d["ok"] and d["turn"] > 0] for s in sessions]
        hit = [sum(d["reused_tokens"] or 0 for d in s) / max(1, sum(d["prompt_tokens"] or 0 for d in s)) * 100
               for s in follow if s]
        # earlier conversation read again: what the previous turn held (prompt + its reply) minus what this turn reused
        reread, fresh = [], []
        for s in sessions:
            s = [d for d in s if d["ok"]]
            reread.append(sum(max(0, (p["prompt_tokens"] or 0) + (p["generated_tokens"] or 0) - (d["reused_tokens"] or 0))
                              for p, d in zip(s, s[1:])))
            fresh.append(sum(d["read_tokens"] or 0 for d in s[1:]) - reread[-1])
        self.out["agent"] = {"sessions": sessions, "hit_pct": stat(hit), "reread_tokens": stat(reread), "new_tokens": stat(fresh),
                             "turn_ttft_s": stat(d["ttft_s"] for s in follow for d in s),
                             "last_turn_prompt_tokens": stat(s[-1]["prompt_tokens"] for s in follow if s),
                             "turn_decode_tps": stat(d["decode_tps"] for s in follow for d in s),
                             "first_turn_ttft_s": stat(s[0]["ttft_s"] for s in sessions if s and s[0]["ok"])}

    def run_clients(self, n: int):
        self.log(f"concurrency: {n} requests at once x {self.runs} rounds, {BURST_MAX_TOKENS} new tokens each")
        rows = []
        with self.phase(f"{self.clients} clients at once"):
            for rd in range(self.runs):
                res: dict = {}
                th = []
                t0 = time.perf_counter()
                for i in range(n):
                    t = threading.Thread(target=serve_load.one, args=(self.base, self.key, "strata", i, 400, BURST_MAX_TOKENS, res, {},
                                                                     self.think, f" {self.tag('b', n, rd)}"))
                    t.start()
                    th.append(t)
                for t in th:
                    t.join()
                wall = time.perf_counter() - t0
                toks = sum(r["tokens"] for r in res.values())
                errs = [r["err"] for r in res.values() if r["err"]]
                rows.append({"round": rd, "wall_s": round(wall, 2), "tokens": toks, "total_tps": round(toks / wall, 2),
                             "first_s": [r["first"] for r in res.values() if r["first"] is not None],
                             "latency_s": [r["total"] for r in res.values()], "errors": errs[:3], "error_count": len(errs)})
                self.log(f"  round {rd + 1}: {toks} tokens in {wall:.1f} s = {toks / wall:.1f} tok/s total"
                         + (f"  ERRORS {errs[:1]}" if errs else ""))
        firsts = [x for r in rows for x in r["first_s"]]
        lats = [x for r in rows for x in r["latency_s"]]
        self.out["clients"] = {"clients": n, "rounds": rows, "total_tps": stat(r["total_tps"] for r in rows),
                               "ttft_p50_s": round(pctl(firsts, .5), 3) if firsts else None,
                               "ttft_p95_s": round(pctl(firsts, .95), 3) if firsts else None,
                               "latency_p50_s": round(pctl(lats, .5), 3) if lats else None,
                               "latency_p95_s": round(pctl(lats, .95), 3) if lats else None,
                               "errors": sum(r["error_count"] for r in rows)}

    def run(self):
        self.warm_up()
        if "chat" in self.tests:
            self.run_chat()
        if "4k" in self.tests:
            self.run_prompt(4096, "4K")
        if "16k" in self.tests:
            self.run_prompt(16384, "16K")
        if "agent" in self.tests:
            self.run_agent()
        if self.clients:
            self.run_clients(self.clients)
        if self.want_long:
            self.run_prompt(32768, "32K")
        return self.out


def engine_attribution(base: str, key: str, out: dict, before: dict) -> dict:
    """The expert cache and the drafts, per test, from the engine's OWN per-request records.

    The speed tests carry a unique tag, but the cache's hit rate and the draft acceptance are not in the answer's usage -
    they are only in the server's request history.  So each test records the seconds it ran (its "phase") and the records
    are put back against it by their own timestamp; the whole-suite figures come from the difference of the engine's
    cumulative counters, which is exact whatever else the server was doing before.
    """
    m = try_json(base, "/metrics", key, 30) or {}
    recs, after = m.get("requests") or [], m.get("totals") or {}
    def delta(k: str) -> int:
        return max(0, int(after.get(k) or 0) - int(before.get(k) or 0))
    offered, accepted, reqs = delta("drafts_offered"), delta("drafts_accepted"), delta("requests")
    # The counters are exact for the whole suite.  The cache's hit rate and its PCIe share are only in the per-request
    # records, and the server keeps records from before the bench too, so those two come from the records whose own
    # timestamp falls inside one of our phases.  Both are a mean over the requests that reported them: an engine too old
    # to report one says nothing about it, and counting its silence as a zero would invent a number.  (The expert cache's
    # hit_rate is its lookups that were already in VRAM, and its pcie_share is the experts routed to the GPU over the bus;
    # they are NOT two halves of one figure - what is left came from the resident copy in system RAM, which is the whole
    # point of the cache being small.)  The prompt cache's reuse is a different thing and is reported as its own figure.
    def hit_pct(rows: list) -> float | None:
        v = [r["hit_rate"] for r in rows if r.get("hit_rate") is not None]
        return round(100.0 * sum(v) / len(v), 1) if v else None
    def pcie_pct(rows: list) -> float | None:
        v = [r["pcie_share"] for r in rows if r.get("pcie_share") is not None]
        return round(100.0 * sum(v) / len(v), 1) if v else None
    def reuse_pct(tok: int, reused: int) -> float | None:
        return round(100.0 * reused / tok, 1) if tok else None
    def span_of(ph: dict) -> list:
        t0, t1 = ph.get("from_s") or 0, ph.get("to_s", 0)
        return [r for r in recs if t0 and t0 <= float(r.get("time") or 0) <= t1]
    ours = [r for r in recs if any(ph.get("from_s") and ph["from_s"] <= float(r.get("time") or 0) <= ph.get("to_s", 0)
                                   for ph in out.get("phases") or [])]
    off, acc = sum(r.get("drafts_offered") or 0 for r in ours), sum(r.get("drafts_accepted") or 0 for r in ours)
    whole = {"requests": reqs, "prompt_tokens": delta("prompt_tokens"), "reused_tokens": delta("reused"),
             "drafts_offered": offered, "drafts_accepted": accepted, "matched_requests": len(ours),
             "cache_hit_pct": hit_pct(ours), "pcie_share_pct": pcie_pct(ours),
             "prompt_reuse_pct": reuse_pct(delta("prompt_tokens"), delta("reused")),
             "draft_accept_pct": round(100.0 * accepted / offered, 1) if offered else None}
    per_test = {}
    for ph in out.get("phases") or []:
        span = span_of(ph)
        if not span:
            continue
        stok = sum(r.get("prompt_tokens") or 0 for r in span)
        soff, sacc = sum(r.get("drafts_offered") or 0 for r in span), sum(r.get("drafts_accepted") or 0 for r in span)
        per_test[ph["name"]] = {"requests": len(span),
                                "cache_hit_pct": hit_pct(span),
                                "pcie_share_pct": pcie_pct(span),
                                "prompt_reuse_pct": reuse_pct(stok, sum(r.get("reused") or 0 for r in span)),
                                "draft_accept_pct": round(100.0 * sacc / soff, 1) if soff else None,
                                "decode_tok_s_median": med([r.get("decode_tok_s") for r in span if r.get("decode_tok_s")])}
    return {"whole_suite": whole, "per_test": per_test}


def summary_metrics(out: dict) -> dict:
    """The flat {name: stat} the --compare table and the README table read."""
    m = {"chat decode tok/s": out["chat"].get("decode_tps"), "chat first token (s)": out["chat"].get("ttft_s")}
    for k, v in out["prompts"].items():
        m[f"{k} prompt tok/s"] = v.get("prompt_tps")
        m[f"{k} first token (s)"] = v.get("ttft_s")
    a = out["agent"]
    if a:
        m["agent cache hit %"] = a.get("hit_pct")
        m["agent re-read tokens/session"] = a.get("reread_tokens")
        m["agent turn first token (s)"] = a.get("turn_ttft_s")
        m["agent decode tok/s"] = a.get("turn_decode_tps")
    c = out["clients"]
    if c:
        m[f"{c['clients']} clients total tok/s"] = c.get("total_tps")
    eng = out.get("engine") or {}
    w = eng.get("whole_suite") or {}
    if w.get("cache_hit_pct") is not None:
        m["expert cache hit %"] = w["cache_hit_pct"]
    if w.get("prompt_reuse_pct") is not None:
        m["prompt cache reuse %"] = w["prompt_reuse_pct"]
    if w.get("pcie_share_pct") is not None:
        m["experts over PCIe %"] = w["pcie_share_pct"]
    if w.get("draft_accept_pct") is not None:
        m["draft accepted %"] = w["draft_accept_pct"]
    return m


# -------------------------------------------------------------------------------------------------- the machine
def run_cmd(cmd, timeout=15) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def total_ram_gib() -> float | None:
    try:
        import psutil
        return round(psutil.virtual_memory().total / 2 ** 30, 1)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 2 ** 20, 1)
    except OSError:
        pass
    if os.name == "nt":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong)] + [
                    (n, ctypes.c_ulonglong) for n in ("a", "b", "c", "d", "e")]
            ms = MS()
            ms.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return round(ms.total / 2 ** 30, 1)
        except Exception:                               # noqa: BLE001
            pass
    return None


def cpu_name() -> str | None:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    if os.name == "nt":
        out = run_cmd(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"])
        if out.strip():
            return out.strip()
    return platform.processor() or None


def gpu_info_local() -> dict:
    out = run_cmd(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 3:
            gpus.append({"name": p[0], "vram_mib": int(float(p[1])) if p[1].replace(".", "").isdigit() else None, "driver": p[2]})
    info = {"gpus": gpus}
    head = run_cmd(["nvidia-smi"])
    m = re.search(r"CUDA Version:\s*([\d.]+)", head)
    if m:
        info["cuda"] = m.group(1)
    if not gpus:
        rocm = run_cmd(["rocm-smi", "--showproductname"])
        names = re.findall(r"Card [Ss]eries:\s*(.+)", rocm) or re.findall(r"Card [Mm]odel:\s*(.+)", rocm)
        info["gpus"] = [{"name": n.strip(), "vram_mib": None, "driver": None} for n in names]
        v = run_cmd(["rocm-smi", "--showdriverversion"])
        m2 = re.search(r"Driver version:\s*(\S+)", v)
        if m2:
            info["rocm_driver"] = m2.group(1)
    return info


QUANT = re.compile(r"(?i)(?:^|[-_.])((?:UD-)?(?:IQ\d_[A-Z]+|Q\d_[A-Z0-9_]+|Q\d_K(?:_[A-Z]+)?|IQ\d_?[A-Z]*|BF16|F16|Q\d))(?=[-_.]|$)")
SECRET_KEY = re.compile(r"(?i)key|token|secret|password|auth")


def base_if_path(v):
    if isinstance(v, str) and (("/" in v or "\\" in v) or re.search(r"\.(gguf|bin|json|pack|txt|jinja|so|dll|exe)$", v, re.I)):
        return re.split(r"[\\/]", v.rstrip("\\/"))[-1]
    return v


def config_view(cfg: dict) -> dict:
    """The settings worth reporting, with paths cut to file names and secrets dropped."""
    out = {}
    for k, v in cfg.items():
        if SECRET_KEY.search(k) or k in ("log", "cwd", "env"):
            continue
        if k == "exe":
            out[k] = base_if_path(v)
        elif k == "args" and isinstance(v, list):
            out[k] = [base_if_path(x) if isinstance(x, str) else x for x in v]
        elif isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = base_if_path(v)
        elif isinstance(v, (list, dict)):
            out[k] = json.loads(json.dumps(v))
    env = cfg.get("env")
    if isinstance(env, dict):
        out["env"] = {k: base_if_path(str(v)) for k, v in env.items() if k.startswith(("STRATA_", "HIP_", "CUDA_", "ROCR_", "ONEAPI_", "SYCL_")) and not SECRET_KEY.search(k)}
    return out


def launch_line(view: dict) -> str:
    args = " ".join(f'"{a}"' if " " in str(a) else str(a) for a in view.get("args", []))
    return f"{view.get('exe', 'strata')} {args}".strip()


def detect_quant(view: dict) -> str | None:
    names = [a for a in view.get("args", []) if isinstance(a, str) and re.search(r"\.(gguf|pack)$|^[\w.-]+$", a)]
    names += [str(view.get("model_name") or "")]
    for n in names:
        m = QUANT.search(n)
        if m:
            return m.group(1).upper()
    return None


def project_version() -> str | None:
    try:
        m = re.search(r"project\(strata VERSION ([\d.]+)", (ROOT / "CMakeLists.txt").read_text())
        return m.group(1) if m else None
    except OSError:
        return None


def git_commit() -> str | None:
    if not (ROOT / ".git").exists():
        return None
    out = run_cmd(["git", "-C", str(ROOT), "rev-parse", "--short=8", "HEAD"]).strip()
    return out or None


def collect_machine(base: str, key: str, cfg: dict | None) -> dict:
    status = try_json(base, "/v1/status", key) or {}
    metrics = try_json(base, "/metrics", key, 30) or {}
    props = try_json(base, "/props", key) or {}
    models = try_json(base, "/v1/models", key) or {}
    static = metrics.get("hardware_static") or {}
    eng = metrics.get("engine") or {}
    local = gpu_info_local()
    view = config_view(cfg) if cfg else {}
    gpu_names = [g["name"] for g in local["gpus"]] or ([static["gpu_name"]] if static.get("gpu_name") else [])
    m = status.get("machine") or {}
    vram_mib = (m.get("gpu") or {}).get("total_mib") or None
    if local["gpus"] and local["gpus"][0].get("vram_mib"):
        vram_mib = sum(g.get("vram_mib") or 0 for g in local["gpus"])
    n_ctx = None
    for d in models.get("data") or []:
        n_ctx = (d.get("meta") or {}).get("n_ctx") or n_ctx
    n_ctx = n_ctx or (props.get("default_generation_settings") or {}).get("n_ctx")
    return {
        "gpu": gpu_names, "gpu_vram_mib": vram_mib, "gpu_driver": (local["gpus"][0].get("driver") if local["gpus"] else local.get("rocm_driver")),
        "cuda": local.get("cuda"), "cpu": static.get("cpu_name") or cpu_name(), "cpu_cores": static.get("cores"),
        "cpu_threads": static.get("threads") or os.cpu_count(), "ram_gib": total_ram_gib(),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})" if os.name == "nt" else f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "strata_version": project_version(), "strata_commit": git_commit(),
        "engine_version": (eng.get("version") or status.get("engine") or str(props.get("build_info", "")).replace("Strata ", "") or None),
        "model": view.get("model_name") or status.get("model") or props.get("model_alias"),
        "quant": detect_quant(view) if view else None,
        "context": n_ctx or eng.get("max_context"), "parallel_slots": props.get("total_slots"),
        "config": view, "launch": launch_line(view) if view else None,
        "engine_info": {k: v for k, v in eng.items() if isinstance(v, (str, int, float, bool, dict)) and k not in ("model_path",)},
        "vram_after": (status.get("vram") or {}),
        # The engine's own counters since it started, and the request records it still holds (every request since start,
        # each with hit_rate / pcie_share / drafts).  The bench takes the DIFFERENCE of two readings, or the records whose
        # timestamp falls inside one of its own tests, so a server that was already doing other work cannot skew it.
        "totals": metrics.get("totals") or {}, "requests_kept": metrics.get("requests_kept") or 0,
        "requests": metrics.get("requests") or [],
    }


# --------------------------------------------------------------------------------------------------- the server
def find_configs(folder: Path = ROOT) -> list[Path]:
    out = []
    for p in folder.glob("strata-*.json"):
        if p.name.endswith(".shared-settings.json"):
            continue
        try:
            c = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(c, dict) and c.get("exe") and isinstance(c.get("args"), list):
            out.append(p)
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def is_strata(base: str, key: str) -> bool:
    h = try_json(base, "/health", key, 5)
    return bool(h and h.get("service") == "strata")


def server_env() -> dict:
    """The environment the benchmark's server starts with: ours, plus the one thing the record needs.

    STRATA_STATE_HASH makes the engine print a fingerprint line per finished request, which is what
    `runs.jsonl`'s `state_hash` field is built from (docs/BENCH_NOISE.md item 5).  The server passes its own
    environment straight on to the engine (serve/server.py `child_env`), so setting it here is enough; a
    run that already set it keeps its own value.
    """
    env = dict(os.environ)
    env.setdefault("STRATA_STATE_HASH", "1")
    return env


def start_server(cfg_path: Path, port: int, gpu: str | None, log_path: Path):
    cmd = [sys.executable, str(ROOT / "serve" / "server.py"), "--engine", "strata", "--config", str(cfg_path), "--port", str(port)]
    if gpu:
        cmd += ["--gpu", gpu]
    logf = open(log_path, "w", encoding="utf-8", errors="replace")
    return subprocess.Popen(cmd, cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT, env=server_env())


def engine_log_start(cfg: dict | None) -> tuple:
    """The engine's own log, and how big it is right now (the path, the offset, or (None, 0)).

    The harness's temp log gets the server's stdout; the STATE_HASH lines the engine prints go to the log the
    config names, so the fingerprint has to be read from there (docs/REJECTED_PROPOSALS.md: every temp log on
    disk has zero of them).  The offset is taken before the server starts, so the lines read afterwards are
    this run's and not the ones the machine wrote before it.
    """
    p = (cfg or {}).get("log")
    if not p:
        return None, 0
    path = Path(p)
    if not path.is_absolute():
        path = ROOT / p
    try:
        return path, path.stat().st_size
    except OSError:
        return path, 0


def engine_log_lines(path, offset: int) -> list:
    """What the engine appended to its own log since `offset`; missing files and unreadable ones give nothing."""
    if not path:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            return fh.read().splitlines()
    except OSError:
        return []


def wait_ready(base: str, key: str, proc, timeout: float, log=print) -> bool:
    t0, last = time.time(), 0
    while time.time() - t0 < timeout:
        if proc is not None and proc.poll() is not None:
            return False
        h = try_json(base, "/health", key, 5)
        if h and h.get("service") == "strata" and h.get("loaded"):
            return True
        if time.time() - last > 30:
            log(f"  still loading ({int(time.time() - t0)} s) ...")
            last = time.time()
        time.sleep(3)
    return False


def stop_server(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(90)
    except subprocess.TimeoutExpired:
        proc.kill()


def log_excerpt(path: Path, limit: int = 80) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    keep = re.compile(r"(?i)expert cache|slots|vram|ready|resident|kv |context|backend|gpu|prefill|mtp|spec|batch|version|hit")
    out = [x for x in lines if keep.search(x) and "[strata] done:" not in x]
    return out[:limit]


# --------------------------------------------------------------------------------------------------- the report
def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit].strip("-") or "unknown"


def gpu_slug(machine: dict) -> str:
    names = machine.get("gpu") or []
    if not names:
        return "cpu"
    n = re.sub(r"(?i)\b(nvidia|geforce|amd|radeon|intel|graphics|corporation)\b", " ", names[0])
    s = slug(n, 30)
    return s if len(names) == 1 else f"{len(names)}x-{s}"


def model_label(machine: dict) -> str:
    model, q = machine.get("model") or "model", machine.get("quant")
    return model if not q or slug(q) in slug(model, 200) else f"{model}-{q}"


def results_dir_name(machine: dict, date: str, arm: str = "") -> str:
    """A community run is named after the hardware and model; an A/B arm adds what was changed, so the arms of one
    experiment sit next to each other in the folder listing and cannot be mistaken for each other."""
    base = f"{date}-community-{gpu_slug(machine)}-{slug(model_label(machine), 40)}"
    return f"{base}-{slug(arm, 28)}" if arm else base


def unique_dir(parent: Path, name: str) -> Path:
    p, n = parent / name, 2
    while p.exists():
        p = parent / f"{name}-{n}"
        n += 1
    return p


def row(label, prompt_tokens, reused, gen, runs, pp, dec, ttft):
    return f"| {label} | {prompt_tokens} | {reused} | {gen} | {runs} | {pp} | {dec} | {ttft} |"


def fmt_pct(v) -> str:
    """A percentage the engine measured, or a dash when it reported nothing (a dense model routes no experts)."""
    return "-" if v is None else f"{v:.1f}"


def results_table(out: dict) -> list[str]:
    lines = ["| Configuration | Actual prompt tokens | Reused tokens | Generated tokens | Runs | Prompt tok/s median and range | "
             "Decode tok/s median and range | TTFT seconds median and range |",
             "| --- | ---: | ---: | ---: | ---: | --- | --- | --- |"]
    c = out["chat"]
    if c:
        lines.append(row("short chat", fmt(c.get("prompt_tokens"), 0), "0", fmt(c.get("generated_tokens"), 0),
                         (c.get("ttft_s") or {}).get("n", 0), "not measured", fmt(c.get("decode_tps")), fmt(c.get("ttft_s"), 2)))
    for k, v in out["prompts"].items():
        lines.append(row(f"{k} prompt, fresh", fmt(v.get("prompt_tokens"), 0), fmt(v.get("reused_tokens"), 0), str(LONG_MAX_TOKENS) + " cap",
                         (v.get("ttft_s") or {}).get("n", 0), fmt(v.get("prompt_tps")), "not measured", fmt(v.get("ttft_s"), 2)))
    a = out["agent"]
    if a:
        n = len([s for s in a["sessions"] if len(s) > 1])
        lines.append(row("agent session, follow-up turns", fmt(a.get("last_turn_prompt_tokens"), 0) + " at the last turn",
                         "see below", f"up to {AGENT_MAX_TOKENS}", n, "see below", fmt(a.get("turn_decode_tps")),
                         fmt(a.get("turn_ttft_s"), 2)))
    return lines


def machine_state_section(res: dict) -> list:
    """What the machine was doing while this arm ran, as its own README section.

    docs/BENCH_NOISE.md named this as the cheapest thing that would have separated the cold region from a real result:
    one clock and utilisation sample per arm, no statistics needed.  The samples are taken by tools/bench_state.py
    through NVML (polling nvidia-smi costs about 1 tok/s of decode, which is the disturbance being measured); the
    run's own record keeps them, and this is where a reader who is not parsing results.json sees them.  A field the
    machine could not read says so rather than going quiet, because a missing clock looks exactly like an idle one.
    """
    st = res.get("state") or {}
    if not (st.get("start") or st.get("end") or st.get("during")):
        return ["## What the machine was doing", "",
                "Not recorded: this run predates the machine-state sampler, or NVML and psutil were not available "
                "where it ran. Read its speeds with the cold-region warning of docs/BENCH_NOISE.md in mind.", ""]

    def line(label: str, snap) -> str:
        if not snap:
            return f"- {label}: not read"
        g = snap.get("gpu") or {}
        clock = f"{g['sm_mhz']} MHz SM" if g.get("sm_mhz") is not None else "clock not read"
        util = f"{g['util_gpu_pct']}% busy" if g.get("util_gpu_pct") is not None else "utilisation not read"
        heat = f"{g['temp_c']} \u00b0C" if g.get("temp_c") is not None else "temperature not read"
        power = f"{g['power_w']} W" if g.get("power_w") is not None else "power not read"
        ps = f"P{g['pstate']}" if g.get("pstate") is not None else "pstate not read"
        ram = f"{snap['free_ram_gib']} GiB free RAM" if snap.get("free_ram_gib") is not None else "free RAM not read"
        up = f"up {snap['uptime_s'] / 3600:.1f} h" if snap.get("uptime_s") is not None else "uptime not read"
        thr = ", throttled by: " + ", ".join(g["throttle_reasons"]) if g.get("throttle_reasons") else ""
        busy = ", busy background: " + ", ".join(f"{n} {p}%" for n, p in snap.get("background_busy") or []) \
            if snap.get("background_busy") else ""
        return f"- {label}: {up}, {ram}; GPU {clock}, {util}, {heat}, {power}, {ps}{thr}{busy}"

    L = ["## What the machine was doing", "",
         line("at the start", st.get("start")), line("at the end", st.get("end"))]
    d = st.get("during") or {}
    if d.get("samples"):
        sm = d.get("sm_mhz") or {}
        band = f"{sm['min']}-{sm['max']} MHz (mean {sm['mean']})" if sm else "clock not read in any sample"
        util = f"{d['util_gpu_pct_mean']}%" if d.get("util_gpu_pct_mean") is not None else "not read"
        heat = f"{d['temp_c_max']} \u00b0C" if d.get("temp_c_max") is not None else "not read"
        power = f"{d['power_w_max']} W" if d.get("power_w_max") is not None else "not read"
        ram = f"{d['free_ram_gib_min']} GiB" if d.get("free_ram_gib_min") is not None else "not read"
        thr = ", ".join(d["throttle_seen"]) if d.get("throttle_seen") else "none"
        busy = ", ".join(f"{n} {p}%" for n, p in (d.get("background_busy") or {}).items()) or "none"
        L += [f"- during: {d['samples']} samples, one every {d.get('interval_s', 10):g} s - GPU clock {band}, utilisation mean {util}, "
              f"hottest {heat}, most power {power}, least free RAM {ram}; throttling seen: {thr}; background work that took a core: {busy}"]
    elif st.get("start") or st.get("end"):
        L += ["- during: no samples (the sampler did not run, or took none)"]
    L += ["",
          "The clock band and the utilisation are the two that matter: docs/BENCH_NOISE.md found the machine drifts on a "
          "schedule, and an arm whose samples sit in a different band from its comparison arm is not the same machine "
          "state, whatever the speeds say.", ""]
    return L


def render_readme(res: dict) -> str:
    mc, out, p = res["machine"], res["results"], res["params"]
    gpu = " + ".join(mc.get("gpu") or []) or "CPU only"
    vram = f"{mc['gpu_vram_mib'] / 1024:.0f} GiB VRAM" if mc.get("gpu_vram_mib") else "VRAM not read"
    model = (model_label(mc) if mc.get("model") else mc.get("quant")) or "model not read"
    L = [f"# Community benchmark on {gpu}" + (f" \u2014 {p['arm']}" if p.get("arm") else ""), "",
         f"Measured on {res['date']} with `tools/strata_bench.py` (suite {SCHEMA}, {p['runs']} runs per test, medians with ranges). "
         f"Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no "
         f"correctness check was run.", ""]
    if p.get("arm") or p.get("note"):
        L += ["**This run changed one thing on purpose**: " + (f"`{p['arm']}`" if p.get("arm") else "see below") + ".", ""] \
             + ([p["note"], ""] if p.get("note") else []) + [
                 "Everything else - model, quantization, prompts, number of runs - is the same as the other arms, which is what "
                 "makes the comparison mean something. Put two arms beside each other with "
                 "`python tools/strata_bench.py --compare-only <this folder> --compare <the other folder>`.", ""]
    L += [
         "## Hardware and software", "",
         f"- GPU and VRAM: {gpu}, {vram}",
         f"- CPU: {mc.get('cpu') or 'not read'} ({mc.get('cpu_cores') or '?'} cores, {mc.get('cpu_threads') or '?'} threads); "
         f"installed RAM: {mc.get('ram_gib') or '?'} GiB; storage: not measured",
         f"- OS: {mc.get('os')}; GPU driver: {mc.get('gpu_driver') or 'not read'}; CUDA: {mc.get('cuda') or 'not read'}",
         f"- Strata: project {mc.get('strata_version') or '?'}, commit {mc.get('strata_commit') or 'not a git checkout'}; "
         f"engine {mc.get('engine_version') or 'not read'}",
         "- What the machine was doing during the run: below", "",
         "\n".join(machine_state_section(res)),
         "## Model and configuration", "",
         f"- Model and quantization: {model}",
         f"- Context: {mc.get('context') or 'not read'} tokens; parallel slots: {mc.get('parallel_slots') or 1}",
         f"- Settings: the install's own configuration, unchanged{' (' + mc['config']['backend'] + ' backend)' if mc.get('config', {}).get('backend') else ''}; thinking "
         f"{'on' if p['think'] else 'off'} for the suite; temperature 0", ""]
    if mc.get("launch"):
        L += ["```text", mc["launch"], "```", ""]
    env = (mc.get("config") or {}).get("env")
    if env:
        L += ["Environment of the config: " + ", ".join(f"`{k}={v}`" for k, v in env.items()), ""]
    L += ["## Method", "",
          f"Run by `{p['command']}` against the HTTP API of a Strata server "
          f"({'started by the benchmark from the install configuration' if p['started_server'] else 'already running'}); model loading is not "
          f"timed. One warm-up chat and a 1K prompt are discarded first. Every prompt starts with a unique tag, so the conversation "
          f"cache cannot help the fresh-prompt tests.", "",
          "The tests run in a fixed order (short chat, fresh prompts, agent session) and the model is **deliberately not reset** "
          "between them: this is how a working machine looks, warm and with its caches filled, which is what Strata is built for. "
          "A later test can therefore be helped by an earlier one, so compare one result with another only test by test, and never "
          "read the first tests as cold-start figures.", "",
          f"- short chat: the three questions of `tools/ab_engine.py`, up to {CHAT_MAX_TOKENS} new tokens, {p['runs']} runs",
          f"- 4K / 16K{' / 32K' if 'prompt' in res['results'] and '32K' in res['results']['prompts'] else ''} prompts: generated word lists sized by a calibration request, "
          f"{LONG_MAX_TOKENS} new tokens, {p['runs']} fresh prompts each",
          f"- agent session: system prompt and four tools, then {AGENT_TURNS} turns that each add about {AGENT_WORDS} words of a project file "
          f"and continue from the model's own earlier answers (the way agent clients resend a conversation), {AGENT_MAX_TOKENS} new tokens per turn, "
          f"{p['runs']} sessions; the cache numbers come from the answer's usage (`cached_tokens`) and timings (`prompt_n`)",
          "- concurrency (when run): N requests sent at the same moment, total tokens over the wall time of the burst, "
          "latency over all requests", "",
          "## Results", ""]
    L += results_table(out)
    L += ["", "Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token."]
    ag = out["agent"]
    if ag and ag.get("sessions"):
        L += ["", "### Agent session (conversation cache)", "",
              f"Cache hit over the follow-up turns: {fmt(ag.get('hit_pct'), 1, ' %')}; earlier-turn tokens read again per session: "
              f"{fmt(ag.get('reread_tokens'), 0)} (earlier turns that the cache did not hold; the {fmt(ag.get('new_tokens'), 0)} new tokens of the files are read once); first turn (nothing cached) first token: {fmt(ag.get('first_turn_ttft_s'), 2, ' s')}.", "",
              "| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |", "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for d in ag["sessions"][0]:
            if d["ok"]:
                L.append(f"| {d['turn']} | {d['prompt_tokens']} | {d['reused_tokens']} | {d['read_tokens']} | {d['prompt_tps'] or '-'} | {d['ttft_s']} | {d['decode_tps'] or '-'} |")
    eng = out.get("engine") or {}
    if eng.get("per_test"):
        w = eng.get("whole_suite") or {}
        L += ["", "### The expert cache and the drafts (the engine's own figures, not measured by this client)", "",
              f"Over the whole run: {w.get('requests', 0)} requests, the expert cache found "
              f"{fmt_pct(w.get('cache_hit_pct'))}% of the experts it looked for already in VRAM, sent "
              f"{fmt_pct(w.get('pcie_share_pct'))}% of the routed experts over PCIe, and the prompt cache reused "
              f"{fmt_pct(w.get('prompt_reuse_pct'))}% of the prompt tokens; the drafts were accepted "
              f"{fmt_pct(w.get('draft_accept_pct'))}% of the time offered "
              f"({w.get('drafts_accepted', 0)} of {w.get('drafts_offered', 0)}).", "",
              "A larger PCIe share is NOT a better run: it is the share of the routed experts that had to be fetched over the bus.",
              "The two cache columns do not add up to 100: the expert cache's hit rate counts its own lookups, its PCIe share counts",
              "the experts that came over the bus, and everything else was already resident in system RAM - which is what a cache",
              "smaller than the model is for.  The prompt cache reusing 70% of a prompt says nothing about the expert cache.",
              "", "| Test | Requests | Expert cache hit % | Over PCIe % | Prompt reuse % | Drafts accepted % | Decode tok/s (engine) |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for name, v in eng["per_test"].items():
            L.append(f"| {name} | {v['requests']} | {fmt_pct(v.get('cache_hit_pct'))} | {fmt_pct(v.get('pcie_share_pct'))} | "
                     f"{fmt_pct(v.get('prompt_reuse_pct'))} | "
                     f"{fmt_pct(v.get('draft_accept_pct'))} | {v.get('decode_tok_s_median') if v.get('decode_tok_s_median') is not None else '-'} |")
        L += ["", "These come from the server's own per-request history, matched to each test by the seconds that test ran; the "
              "whole-run line is the difference of the engine's cumulative counters, so work the server did before this run cannot "
              "enter it. A dense model, or an engine too old to report them, leaves these columns empty."]
    cl = out["clients"]
    if cl:
        L += ["", f"### Concurrency: {cl['clients']} clients", "",
              f"Total {fmt(cl.get('total_tps'), 1, ' tok/s')}; first token p50 {cl.get('ttft_p50_s')} s, p95 {cl.get('ttft_p95_s')} s; "
              f"request latency p50 {cl.get('latency_p50_s')} s, p95 {cl.get('latency_p95_s')} s; errors {cl.get('errors')}. "
              f"Server parallel slots: {mc.get('parallel_slots') or 1} (requests beyond that wait their turn)."]
    if out["skipped"] or out["failures"]:
        L += ["", "### Skipped and failed", ""]
        L += [f"- skipped {k}: {v}" for k, v in out["skipped"].items()]
        L += [f"- failed {f['where']}: {f['error']}" for f in out["failures"]]
    L += ["", "Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).", "",
          "## Correctness and limitations", "",
          "Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. "
          "Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS, and what the machine was doing "
          "during the run is the section above. "
          f"Total time of the suite: {res['elapsed_s']:.0f} s.", ""]
    return "\n".join(L)


def _num(v):
    """A summary figure is either a {median,min,max,n} block (the speed tests) or a plain number (the engine's own
    counters, which are one aggregate over the run, not a set of runs)."""
    return (v.get("median") if isinstance(v, dict) else v)


def compare_table(old: dict, new: dict) -> str:
    a, b = summary_metrics(old["results"]), summary_metrics(new["results"])
    rows = [f"{'':32s} {'old':>12s} {'new':>12s} {'change':>9s}"]
    for k in dict.fromkeys([*a, *b]):
        x, y = _num(a.get(k)), _num(b.get(k))
        if x is None and y is None:
            continue
        ch = f"{(y - x) / x * 100:+.1f}%" if x and y is not None else ""
        rows.append(f"{k:32s} {('-' if x is None else format(x, '.2f')):>12s} {('-' if y is None else format(y, '.2f')):>12s} {ch:>9s}")
    ho, hn = old["machine"], new["machine"]
    notes = [f"  {n}: {ho.get(f)} -> {hn.get(f)}" for n, f in (("GPU", "gpu"), ("engine", "engine_version"), ("model", "model"), ("quant", "quant"),
                                                              ("context", "context")) if ho.get(f) != hn.get(f)]
    # An A/B arm differs ONLY in the launch arguments, and the two arms otherwise look identical; without this line the
    # table says "no differences" about the one thing that was changed on purpose.
    arms = [((old.get("params") or {}).get("arm"), (new.get("params") or {}).get("arm"))]
    if arms[0][0] or arms[0][1]:
        notes.append(f"  arm: {arms[0][0] or 'unchanged'} -> {arms[0][1] or 'unchanged'}")
    for n, f in (("launch", "launch"), ("expert slots", "expert_slots"), ("cache MiB", "expert_cache_mib"),
                 ("spec drafts", "spec"), ("mtp", "mtp_max")):
        x = (ho.get("engine_info") or {}).get(f if f != "launch" else "") or ho.get(f)
        y = (hn.get("engine_info") or {}).get(f if f != "launch" else "") or hn.get(f)
        if x != y:
            notes.append(f"  {n}: {x} -> {y}")
    return "\n".join(rows + (["", "differences:"] + notes if notes else []))


def load_results(path: Path) -> dict:
    p = Path(path)
    if p.is_dir():
        p = p / "results.json"
    return json.loads(p.read_text(encoding="utf-8"))


def short_table(res: dict) -> str:
    m = summary_metrics(res["results"])
    lines = []
    for k, v in m.items():
        if isinstance(v, dict) and v:
            lines.append(f"  {k:32s} {v['median']:>10.2f}   ({v['min']:.2f} - {v['max']:.2f}, n={v['n']})")
        elif isinstance(v, (int, float)):
            lines.append(f"  {k:32s} {v:>10.2f}   (the engine's own figures for the whole run)")
    return "\n".join(lines)


def write_results(res: dict, parent: Path, red, log_lines: list[str]) -> Path:
    d = unique_dir(parent, results_dir_name(res["machine"], res["date"], (res.get("params") or {}).get("arm", "")))
    d.mkdir(parents=True)
    clean = redact_obj(res, red)
    (d / "results.json").write_text(json.dumps(clean, indent=1, ensure_ascii=False), encoding="utf-8")
    (d / "README.md").write_text(red(render_readme(res)), encoding="utf-8")
    if log_lines:
        (d / "engine-log-excerpt.txt").write_text(red("\n".join(log_lines)) + "\n", encoding="utf-8")
    return d


# --------------------------------------------------------------------------------------------------------- main
def selftest() -> int:
    """Check the arithmetic on numbers we made up, so a run's report cannot be wrong in a way the machine cannot see.

    No server and no model: the HTTP calls are replaced by a fabricated /metrics, the same shape the engine sends.
    """
    bad = 0
    def check(name: str, ok: bool, got="") -> None:
        nonlocal bad
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"   got {got}"))
        bad += 0 if ok else 1

    # 1. per-request records matched to the test that ran them, by the test's own seconds
    out = {"phases": [{"name": "chat", "from_s": 100.0, "to_s": 120.0}, {"name": "agent", "from_s": 200.0, "to_s": 260.0}]}
    # The keys are the ones serve/server.py actually writes for a request ("reused", "hit_rate", "pcie_share", ...): a
    # shape invented here is how a wrong key once made every run report a 0.0% expert cache hit.
    recs = [{"time": 90.0, "prompt_tokens": 5000, "reused": 0, "pcie_share": 0.9, "drafts_offered": 40,
             "drafts_accepted": 4, "decode_tok_s": 11.0, "hit_rate": 0.0},          # before the bench: must be ignored
            {"time": 110.0, "prompt_tokens": 1000, "reused": 100, "pcie_share": 0.30, "drafts_offered": 10,
             "drafts_accepted": 2, "decode_tok_s": 14.0, "hit_rate": 0.1},
            {"time": 210.0, "prompt_tokens": 4000, "reused": 3200, "pcie_share": 0.10, "drafts_offered": 40,
             "drafts_accepted": 20, "decode_tok_s": 20.0, "hit_rate": 0.8},
            {"time": 250.0, "prompt_tokens": 6000, "reused": 4700, "pcie_share": 0.20, "drafts_offered": 60,
             "drafts_accepted": 30, "decode_tok_s": 22.0, "hit_rate": 0.78},
            {"time": 255.0, "prompt_tokens": 500, "reused": 0, "drafts_offered": 0, "drafts_accepted": 0,
             "decode_tok_s": 9.0}]                                # an engine that says nothing about its cache
    global try_json
    keep, try_json = try_json, lambda base, path, key="", timeout=10: {"requests": recs,
                                                                       "totals": {"requests": 4, "prompt_tokens": 16000,
                                                                                  "reused": 8000, "drafts_offered": 110,
                                                                                  "drafts_accepted": 52}}
    try:
        eng = engine_attribution("http://x", "", out, {"requests": 1, "prompt_tokens": 5000, "reused": 0,
                                                       "drafts_offered": 40, "drafts_accepted": 4})
    finally:
        try_json = keep
    w, pt = eng["whole_suite"], eng["per_test"]
    check("records from before the bench are ignored", w["matched_requests"] == 4, w)
    check("the expert cache hit rate is the engine's own hit_rate, not the prompt cache's reuse",
          w["cache_hit_pct"] == 56.0, w["cache_hit_pct"])          # mean of 0.1, 0.8, 0.78; the silent request is skipped
    check("the prompt cache's reuse is its own figure, from the exact counters",
          (w["prompt_reuse_pct"], w["reused_tokens"], w["prompt_tokens"]) == (72.7, 8000, 11000), w)
    check("whole-suite PCIe share averages the requests that reported one, ignoring the silent one",
          w["pcie_share_pct"] == 20.0, w["pcie_share_pct"])
    check("whole-suite drafts come from the engine's counters, not the records",
          (w["drafts_accepted"], w["drafts_offered"], w["draft_accept_pct"]) == (48, 70, 68.6), w)
    check("the agent test gets its own two requests plus the silent one",
          (pt["agent"]["requests"], pt["agent"]["cache_hit_pct"], pt["agent"]["pcie_share_pct"],
           pt["agent"]["prompt_reuse_pct"]) == (3, 79.0, 15.0, 75.2), pt["agent"])   # 7900 of 10500, the silent request included
    check("the chat test gets its own one", (pt["chat"]["requests"], pt["chat"]["cache_hit_pct"], pt["chat"]["pcie_share_pct"],
                                            pt["chat"]["decode_tok_s_median"]) == (1, 10.0, 30.0, 14.0), pt["chat"])

    # 2. the comparison table: what changed, in the direction the reader expects
    def stats(v: float) -> dict:                        # what the suite stores for a repeated timing
        return {"median": v, "min": v, "max": v, "n": 3}
    def fake(hit, pcie, acc, dec, ttft, pp):
        return {"machine": {"gpu": ["Test GPU"], "cpu": "Test CPU", "ram_gib": 32.0, "model": "Test Model",
                            "quant": "Q4_K_M", "context": 8192, "config": {}, "launch": "strata --test"},
                "params": {"runs": 3, "think": False, "clients": 0, "long": False, "started_server": True, "tests": ["agent", "chat"],
                           "arm": "", "note": "", "command": "python tools/strata_bench.py"},
                "results": {"chat": {"prompt_tokens": stats(100), "generated_tokens": stats(100), "decode_tps": stats(dec),
                                     "ttft_s": stats(ttft)},
                            "prompts": {"4k": {"prompt_tokens": stats(4000), "reused_tokens": stats(0), "prompt_tps": stats(pp),
                                               "ttft_s": stats(ttft)}},
                            "agent": {"sessions": [[{"ok": True, "turn": t, "prompt_tokens": 4000, "reused_tokens": 2000,
                                                       "read_tokens": 2000, "prompt_tps": 400.0, "ttft_s": ttft,
                                                       "decode_tps": dec, "generated_tokens": 40} for t in (0, 1)]],
                                      "turns": 2, "hit_pct": stats(hit), "reread_tokens": stats(1000), "new_tokens": stats(2000),
                                      "first_turn_ttft_s": stats(ttft), "turn_ttft_s": stats(ttft), "turn_decode_tps": stats(dec),
                                      "last_turn_prompt_tokens": stats(4000)},
                            "clients": None, "engine": {"whole_suite": {"requests": 4, "cache_hit_pct": hit,
                                                                        "pcie_share_pct": pcie, "draft_accept_pct": acc,
                                                                        "drafts_accepted": 10, "drafts_offered": 20},
                                                        "per_test": {"agent session": {"requests": 6, "cache_hit_pct": hit,
                                                                                        "pcie_share_pct": pcie,
                                                                                        "draft_accept_pct": acc,
                                                                                        "decode_tok_s_median": dec}}},
                            "failures": [], "skipped": []}}
    before, after = fake(40.0, 30.0, 50.0, 20.0, 1.0, 400.0), fake(60.0, 12.0, 70.0, 30.0, 0.5, 600.0)
    after["params"]["arm"] = "cache bigger"
    table = compare_table(before, after)
    def change_of(label: str) -> str:
        line = next((x for x in table.splitlines() if x.startswith(label)), "")
        return line.split()[-1] if line else ""
    check("the comparison names the arm", "cache bigger" in table, table[:80])
    check("the expert cache hit going up is shown as up", change_of("expert cache hit %") == "+50.0%", table)
    check("less over PCIe is shown as down", change_of("experts over PCIe %") == "-60.0%", table)
    check("faster decode is shown as up", change_of("chat decode tok/s") == "+50.0%", table)
    check("a shorter first token is shown as down", change_of("chat first token (s)") == "-50.0%", table)
    check("a dense model's missing figures do not crash the table",
          "-" in compare_table(fake(None, None, None, 20.0, 1.0, 400.0), fake(None, None, None, 21.0, 1.0, 400.0)))

    # 3. the report itself
    res = dict(after, date="2026-01-01", elapsed_s=100.0)
    doc = render_readme(res)
    check("the report says what the arm changed", "This run changed one thing on purpose" in doc, doc[:120])
    check("the report carries the engine's own attribution, and keeps the two caches apart",
          all(s in doc for s in ("already in VRAM", "prompt cache reused", "do not add up to 100")), doc[:200])
    check("the report warns that PCIe share is not a score", "NOT a better run" in doc)
    check("the report says the model was not reset between tests", "deliberately not reset" in doc)
    check("the speed table has a row per test", all(s in doc for s in ("short chat", "4k prompt, fresh", "agent session")), doc[:400])
    check("a run without machine state says so instead of printing an empty section",
          "predates the machine-state sampler" in doc, doc[doc.find("## What the machine"):][:120])
    with_state = dict(res, state={"start": {"uptime_s": 7200.0, "free_ram_gib": 41.2,
                                            "gpu": {"sm_mhz": 1890, "util_gpu_pct": 99, "temp_c": 68, "power_w": 210.5,
                                                    "pstate": 1, "throttle": 128, "throttle_reasons": ["hw power brake"]},
                                            "background_busy": [["msmpeng", 88]]},
                                  "end": {"uptime_s": 7500.0, "gpu": {"sm_mhz": 2910, "util_gpu_pct": 2, "throttle_reasons": []}},
                                  "during": {"samples": 3, "interval_s": 10.0, "sm_mhz": {"min": 1890, "max": 2910, "mean": 2230},
                                             "util_gpu_pct_mean": 66.3, "temp_c_max": 68, "power_w_max": 210.5,
                                             "free_ram_gib_min": 40.9, "throttle_seen": ["hw power brake"],
                                             "background_busy": {"msmpeng": 88}}})
    doc2 = render_readme(with_state)
    check("the clock band, the utilisation and the throttling are in the report",
          all(s in doc2 for s in ("1890-2910 MHz (mean 2230)", "utilisation mean 66.3%", "hw power brake", "msmpeng 88%")),
          doc2[doc2.find("## What the machine"):][:400])
    check("a field that could not be read says so, rather than looking idle",
          "utilisation not read" in render_readme(dict(res, state={"start": {"gpu": {"sm_mhz": 2910}}})), doc[:80])

    # 4. privacy: a stranger's paths, key and addresses must not survive
    red = redactor(key="sk-secret-1234567890")
    line = red("launch C:\\Users\\a3k6g\\AppData\\Local\\strata.exe --model D:\\models\\m.gguf key sk-secret-1234567890 at 192.168.4.22")
    check("the API key is gone", "sk-secret-1234567890" not in line, line)
    check("the home folder is gone", "a3k6g" not in line, line)
    check("the IP address is gone", "192.168.4.22" not in line, line)
    check("the model path is kept, only its folder is hidden", "m.gguf" in line, line)
    deep = json.dumps(redact_obj({"a": {"b": ["C:\\Users\\a3k6g\\x"]}}, red), ensure_ascii=False)
    check("a results tree is redacted all the way down", "a3k6g" not in deep, deep)

    # 5. the fingerprint: the engine only prints it if the server it starts with asks, and reads it from the engine's log
    check("the server the benchmark starts asks for the fingerprint", server_env().get("STRATA_STATE_HASH") == "1", server_env())
    make_record_from = (lambda res, log_lines=None, state_lines=None: bench_record.make_record(res, None, log_lines, None, state_lines)) \
        if bench_record else (lambda *x, **k: check("bench_record importable", False) or {})
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "engine.log"
        log.write_text("strata serve: STATE_HASH L=1 gdn=aaaa ple=bbbb tail=cccc pooled=dddd kv=eeee\n", encoding="utf-8")
        off = log.stat().st_size
        check("a run whose engine printed nothing has no fingerprint",
              make_record_from({"results": {}}, state_lines=engine_log_lines(log, off))["state_hash"] is None, "")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("strata serve: STATE_HASH L=2 gdn=0000000000000001 ple=0000000000000002 tail=0000000000000003 pooled=0000000000000004 kv=0000000000000005\n")
        got = make_record_from({"results": {}}, state_lines=engine_log_lines(log, off))["state_hash"]
        check("only what the engine wrote during this run counts, not what the machine wrote before it",
              got and got["lines"] == 1, got)
        check("the fingerprint from the engine's own log beats the harness's excerpt, which never has one",
              make_record_from({"results": {}}, ["no fingerprint lines here"], state_lines=engine_log_lines(log, off))["state_hash"] == got, got)
    print("SELFTEST", "passed" if bad == 0 else f"FAILED: {bad}")
    return 0 if bad == 0 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Measure this Strata install and write a shareable report (see the top of this file).")
    ap.add_argument("--config", help="the model config to run (default: the newest strata-*.json of this folder)")
    ap.add_argument("--url", help="use the server already running at this address (http://127.0.0.1:8080) instead of starting one")
    ap.add_argument("--port", type=int, help="the port to start the server on (default: the config's)")
    ap.add_argument("--gpu", help="start the server on this GPU only (as the server's --gpu)")
    ap.add_argument("--api-key", default=os.environ.get("STRATA_KEY", ""), help="the server's API key, if it has one (or STRATA_KEY)")
    ap.add_argument("--runs", type=int, default=3, help="runs per test (default 3)")
    ap.add_argument("--clients", type=int, default=0, metavar="N", help="also send N requests at once")
    ap.add_argument("--long", action="store_true", help="also a prompt of about 32,768 tokens")
    ap.add_argument("--think", action="store_true", help="leave thinking on (the default suite turns it off)")
    ap.add_argument("--tests", metavar="LIST",
                    help="only these tests, comma separated (chat,4k,16k,agent); default all four. "
                         "An A/B arm runs the same subset in every arm")
    ap.add_argument("--engine-arg", metavar="ARGS", action="append", default=[],
                    help="arguments to ADD to the engine when the benchmark starts the server, repeatable and applied in order, "
                         "eg --engine-arg " + '"--expert-cache 0"' + " to measure one change (needs --config, no --url)")
    ap.add_argument("--engine-env", metavar="KEY=VALUE", action="append", default=[],
                    help="an environment variable to ADD to the engine's when the benchmark starts the server, repeatable, "
                         "eg --engine-env STRATA_DMA_BATCH=1, for the knobs that are environment and not command line; the "
                         "same arm machinery as --engine-arg (needs --config, no --url)")
    ap.add_argument("--arm", metavar="LABEL", help="what this arm changed: written into the report title, the results.json and "
                                                  "the results folder name, so the arms of one experiment cannot be mixed up")
    ap.add_argument("--note", help="a sentence or two for the report: the question this experiment answers, or what to read in it")
    ap.add_argument("--out", help="where to write the results folder (default: bench/results)")
    ap.add_argument("--compare", metavar="DIR", help="a results folder (or its results.json) to put beside this run")
    ap.add_argument("--compare-only", metavar="NEW", help="print --compare of two existing results folders and stop")
    ap.add_argument("--ready-timeout", type=float, default=1800, help="seconds to wait for the model to load")
    ap.add_argument("--selftest", action="store_true", help="check this tool's own arithmetic on numbers we made up, and stop")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    say = (lambda *x, **k: None) if a.quiet else (lambda *x, **k: print(*x, **k, flush=True))

    if a.compare_only:
        if not a.compare:
            ap.error("--compare-only NEW needs --compare OLD")
        print(compare_table(load_results(Path(a.compare)), load_results(Path(a.compare_only))))
        return 0

    cfg_path, cfg, proc, log_path = None, None, None, None
    if (a.engine_arg or a.engine_env) and (a.url or not a.config):
        print("--engine-arg and --engine-env need the benchmark to start the server itself from a configuration you name: give "
              "--config FILE and drop --url (the extras are added to that configuration, which is written aside unchanged).", file=sys.stderr)
        return 2
    if a.config or not a.url:
        if a.config:
            cfg_path = Path(a.config)
            if not cfg_path.is_absolute() and not cfg_path.exists():
                cfg_path = ROOT / a.config
        else:
            found = find_configs()
            if found:
                cfg_path = found[0]
                if len(found) > 1:
                    say(f"Several models are installed; using {cfg_path.name} (the one used last). Pick another with --config FILE.")
        if cfg_path is None or not cfg_path.is_file():
            print("No Strata model is set up in this folder: run START-HERE.bat (Windows) or ./setup.sh (Linux) first, "
                  "or give --url of a running server.", file=sys.stderr)
            return 2
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        if a.engine_arg or a.engine_env:
            # One arm of an A/B: the SAME configuration plus the arguments and the environment under test, written to a sibling
            # file so the install's own config is never touched and the launch line in the report shows exactly what ran.  A
            # later arm with the same arguments overwrites this file, which is what you want: one file per arm, named after
            # the arm.  The engine reads its own environment, so an arm can be an environment variable too - and the config
            # loader only accepts the keys it knows about (STRATA_*, HIP_*, CUDA_*), so insist on one of those.
            cfg["args"] = list(cfg.get("args") or []) + [x for args in a.engine_arg for x in args.split()]
            for kv in a.engine_env:
                k, sep, v = kv.partition("=")
                if not sep or not k or not k.startswith(("STRATA_", "HIP_", "CUDA_")):
                    print(f"--engine-env {kv}: want KEY=VALUE, KEY one of the ones the configuration's env block accepts", file=sys.stderr)
                    return 2
                (cfg.setdefault("env", {}))[k] = v
            cfg_path = cfg_path.with_name("bench-arm" + ("-" + slug(a.arm, 24) if a.arm else "") + ".json")
            cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
            say(f"Engine arguments added for this run: {' '.join(x for args in a.engine_arg for x in args.split())}")
    key = a.api_key or ((cfg or {}).get("api_key") or "")
    if a.url:
        base = a.url.rstrip("/")
    else:
        port = a.port or int(cfg.get("port", 8080))
        base = f"http://127.0.0.1:{port}"
    red = redactor(key=key)
    started = False
    eng_log, eng_off = None, 0                                      # the engine's own log, measured before the server starts
    try:
        if is_strata(base, key):
            say(f"Using the Strata server already running at {base}.")
        elif a.url:
            print(f"No Strata server answers at {base}.", file=sys.stderr)
            return 2
        else:
            if try_json(base, "/health", key, 3) is not None:
                print(f"Port {port} is used by something that is not Strata: pass --port N.", file=sys.stderr)
                return 2
            log_path = Path(tempfile.gettempdir()) / f"strata-bench-{os.getpid()}.log"
            say(f"Starting {cfg_path.name} on port {port} (loading takes 1-3 minutes; the PC may be slow meanwhile) ...")
            eng_log, eng_off = engine_log_start(cfg)              # before the server starts: the lines after this are ours
            proc = start_server(cfg_path, port, a.gpu, log_path)
            started = True
            if not wait_ready(base, key, proc, a.ready_timeout, say):
                tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:] if log_path.exists() else []
                print("The server did not become ready. Last log lines:\n" + red("\n".join(tail)), file=sys.stderr)
                return 1
        t0 = time.time()
        pre = collect_machine(base, key, cfg)
        state_start = bench_state.snapshot() if bench_state else None             # uptime, GPU clocks and heat, free RAM, busy background processes
        sampler = bench_state.Sampler(interval=10).start() if bench_state else None
        suite = Suite(base, key, pre.get("context"), max(1, a.runs), a.think, log=say, want_clients=a.clients, want_long=a.long,
                      tests=set(x.strip() for x in a.tests.split(",")) if a.tests else None)
        slots = pre.get("parallel_slots") or 1
        if a.clients and a.clients > slots:
            say(f"Note: the server runs {slots} request(s) at a time (--batch), so {a.clients} clients will partly wait their turn.")
        say("Running the suite (about 3-10 minutes) ...")
        out = suite.run()
        # The engine's own cache/draft figures, per test: it reports them per request in its history, which carries no bench
        # tag, so they are matched to the tests by the seconds each test ran (Suite.phase) and the whole-run figures are the
        # difference of the cumulative counters taken before and after - exact whatever the server did before this run.
        out["engine"] = engine_attribution(base, key, out, pre.get("totals") or {})
        w = out["engine"]["whole_suite"]
        say(f"  expert cache: {w['cache_hit_pct'] if w['cache_hit_pct'] is not None else 'not reported'}% of the experts it "
            f"looked for already in VRAM, {w['pcie_share_pct'] if w['pcie_share_pct'] is not None else 'not reported'}% of the "
            f"routed experts over PCIe, prompt cache reused "
            f"{w['prompt_reuse_pct'] if w['prompt_reuse_pct'] is not None else 'not reported'}%; "
            f"drafts accepted {w['draft_accept_pct'] if w['draft_accept_pct'] is not None else 'not reported'}%")
        for name, v in out["engine"]["per_test"].items():
            say(f"    {name}: hit {v['cache_hit_pct']}%, PCIe {v['pcie_share_pct']}%, drafts {v['draft_accept_pct']}%, "
                f"engine decode {v['decode_tok_s_median']} tok/s ({v['requests']} requests)")
        machine = collect_machine(base, key, cfg)             # again: VRAM and the cache after the work
        if not machine.get("gpu"):
            machine["gpu"] = pre.get("gpu")
        res = {"schema": SCHEMA, "tool": "tools/strata_bench.py", "date": datetime.date.today().isoformat(),
               "params": {"runs": suite.runs, "think": a.think, "clients": a.clients, "long": a.long, "started_server": started,
                          "tests": sorted(suite.tests), "arm": a.arm or "", "note": a.note or "",
                          "engine_arg": [x for args in a.engine_arg for x in args.split()],
                          "engine_env": list(a.engine_env),
                          "command": "python tools/strata_bench.py" + "".join(
                              f" {x}" for x in (f"--clients {a.clients}" if a.clients else "", "--long" if a.long else "",
                                                "--think" if a.think else "", f"--runs {a.runs}" if a.runs != 3 else "",
                                                f"--tests {a.tests}" if a.tests else "",
                                                f"--config {Path(a.config).name}" if a.config else "",
                                                *(f'--engine-arg "{args}"' for args in a.engine_arg),
                                                *(f"--engine-env {kv}" for kv in a.engine_env),
                                                f"--arm {a.arm}" if a.arm else "") if x)},
               "elapsed_s": time.time() - t0, "machine": machine, "results": out,
               "state": {"start": state_start, "end": bench_state.snapshot() if bench_state else None,
                         "during": sampler.stop() if sampler else None}}
        parent = Path(a.out) if a.out else ROOT / "bench" / "results"
        log_lines = log_excerpt(log_path) if log_path else []
        d = write_results(res, parent, red, log_lines)
        if bench_record:                                                              # one line per run, beside the results folders
            try:
                bench_record.append(parent / "runs.jsonl",
                                    bench_record.make_record(redact_obj(res, red), d.name, log_lines,
                                                             state_lines=engine_log_lines(eng_log, eng_off)))
            except Exception as e:  # noqa: BLE001 - a record that cannot be written must not lose the run
                say(f"  (runs.jsonl not written: {e})")
        say("\nResults (median, range over the runs):")
        say(red(short_table(res)))
        if out["failures"] or out["skipped"]:
            say(f"  ({len(out['failures'])} failed request(s), {len(out['skipped'])} skipped test(s): see the report)")
        if a.compare:
            say("\nCompared with " + a.compare + ":")
            say(red(compare_table(load_results(Path(a.compare)), redact_obj(res, lambda s: s))))
        try:
            shown = d.relative_to(ROOT)
        except ValueError:
            shown = d
        say(f"\nReport: {shown / 'README.md'}\nShare it: open a pull request that adds the folder {shown} (see docs/COMMUNITY_BENCHMARKS.md), "
            f"or paste README.md into a GitHub issue. Home folders, user and host names and IP addresses are already replaced; "
            f"read it once before you share it.")
        return 1 if out["failures"] and not any(out[k] for k in ("chat", "prompts", "agent")) else 0
    finally:
        if started:
            stop_server(proc)


if __name__ == "__main__":
    sys.exit(main())
