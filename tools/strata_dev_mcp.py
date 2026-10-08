#!/usr/bin/env python3
"""tools/strata_dev_mcp.py - code and file search for Strata's chat (an MCP server over stdio, read-only).

    grep(pattern, path, glob, ignore_case, regex, context, max_results)   which files and lines contain something
    glob(pattern, path, max_results)                                       which files match a pattern (** works)
    read_lines(path, start, end)                                           a numbered range of lines of one file

The official filesystem server finds files by name and reads whole files; it cannot search inside files. These three
are what makes a model useful on a code base: find the line, then read around it.

Safe by construction: nothing here writes or runs anything. Strata passes its no-read list in STRATA_BLOCKED_JSON, and
the walk skips those folders and files as it goes (the model never sees a name from them); links and junctions are not
followed; binary files, files over 5 MB and the usual junk folders (.git, node_modules, __pycache__, venvs) are skipped;
a search stops after 25 seconds; an answer is cut at 30,000 characters.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import threading
import time

MAX_FILE = 5_000_000
MAX_CHARS = 30_000
DEADLINE = 25.0
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox",
             "$recycle.bin", "system volume information"}

_BLOCK = json.loads(os.environ.get("STRATA_BLOCKED_JSON") or "{}")
PREFIXES: list[str] = list(_BLOCK.get("prefixes", []))
GLOBS: list[str] = list(_BLOCK.get("globs", []))


class DevError(Exception):
    """A refusal or failure whose message the model reads."""


def configure(prefixes, globs) -> None:
    """The no-read list (tests; the server reads it from STRATA_BLOCKED_JSON)."""
    PREFIXES[:] = prefixes
    GLOBS[:] = globs


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def is_blocked(path: str, resolve: bool = False) -> bool:
    """Is `path` on the no-read list?  `resolve`: follow links first (used for folders and the starting point)."""
    p = os.path.normcase(os.path.realpath(path)) if resolve else _norm(path)
    return any(p == x or p.startswith(x + os.sep) for x in PREFIXES) or any(fnmatch.fnmatchcase(p, g) for g in GLOBS)


def _top(path: str) -> str:
    if not path or not str(path).strip():
        raise DevError("give a path")
    path = os.path.expandvars(os.path.expanduser(str(path).strip()))
    if is_blocked(path, resolve=True):
        raise DevError("that location is on the user's no-read list")
    if not os.path.exists(path):
        raise DevError(f"no such file or folder: {path}")
    return path


def walk(top: str, include_all: bool = False, stats: dict | None = None):
    """Yield the paths of the regular files under `top` (or `top` itself), skipping what must not be read."""
    stats = stats if stats is not None else {}
    if os.path.isfile(top):
        yield top
        return
    for root, dirs, files in os.walk(top, followlinks=False):
        keep = []
        for d in dirs:
            full = os.path.join(root, d)
            if not include_all and d.lower() in SKIP_DIRS:
                continue
            if os.path.islink(full) or os.path.normcase(os.path.realpath(full)) != _norm(full):
                stats["links"] = stats.get("links", 0) + 1          # a link, junction or short name: not followed
                continue
            if is_blocked(full, resolve=True):
                stats["blocked"] = stats.get("blocked", 0) + 1
                continue
            keep.append(d)
        dirs[:] = keep
        for f in files:
            full = os.path.join(root, f)
            if os.path.islink(full):
                stats["links"] = stats.get("links", 0) + 1
            elif is_blocked(full):
                stats["blocked"] = stats.get("blocked", 0) + 1
            else:
                yield full


def _text_lines(path: str):
    """The lines of a text file, or None for a binary or too-large one."""
    try:
        if os.path.getsize(path) > MAX_FILE:
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", "replace").splitlines()


def _clip(s: str, n: int = 300) -> str:
    return s if len(s) <= n else s[:n] + " ..."


def grep(pattern: str, path: str = ".", glob_filter: str = "", ignore_case: bool = False, regex: bool = True,
         context: int = 0, max_results: int = 200, include_all: bool = False) -> str:
    if not pattern:
        raise DevError("give a pattern to search for")
    top = _top(path)
    try:
        rx = re.compile(pattern if regex else re.escape(pattern), re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise DevError(f"bad pattern: {e}") from None
    context = max(0, min(int(context or 0), 5))
    limit = max(1, min(int(max_results or 200), 1000))
    stats: dict = {}
    out: list[str] = []
    matches = files_hit = searched = skipped = 0
    end = time.monotonic() + DEADLINE
    stopped = ""
    for f in walk(top, include_all, stats):
        if glob_filter and not fnmatch.fnmatch(os.path.basename(f).lower(), glob_filter.lower()) \
                and not fnmatch.fnmatch(f.replace("\\", "/").lower(), glob_filter.lower()):
            continue
        if time.monotonic() > end:
            stopped = "stopped after 25 seconds"
            break
        lines = _text_lines(f)
        if lines is None:
            skipped += 1
            continue
        searched += 1
        hit_here = False
        for i, line in enumerate(lines):
            if not rx.search(line):
                continue
            if not hit_here:
                hit_here = True
                files_hit += 1
            matches += 1
            if matches <= limit:
                for j in range(max(0, i - context), i):
                    out.append(f"{f}-{j + 1}- {_clip(lines[j])}")
                out.append(f"{f}:{i + 1}: {_clip(line)}")
                for j in range(i + 1, min(len(lines), i + 1 + context)):
                    out.append(f"{f}-{j + 1}- {_clip(lines[j])}")
                if context:
                    out.append("--")
        if matches > limit * 3 and not stopped:
            stopped = "stopped counting"
            break
    notes = [f"{matches} match{'es' if matches != 1 else ''} in {files_hit} file{'s' if files_hit != 1 else ''}"
             + (f" (showing the first {limit})" if matches > limit else ""),
             f"searched {searched} files; skipped {skipped} binary or over 5 MB"
             + (f", {stats['blocked']} on the no-read list" if stats.get("blocked") else "")
             + (f", {stats['links']} links" if stats.get("links") else "")]
    if stopped:
        notes.append(stopped)
    body = "\n".join(out)
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + "\n[... cut: narrow the search with a glob or a folder]"
    return ("\n".join(notes) + ("\n\n" + body if body else "")).strip()


def _glob_regex(pattern: str):
    p = pattern.replace("\\", "/")
    i, rx = 0, ""
    while i < len(p):
        c = p[i]
        if p.startswith("**/", i):
            rx += "(?:.*/)?"
            i += 3
            continue
        if p.startswith("**", i):
            rx += ".*"
            i += 2
            continue
        rx += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    return re.compile("^" + rx + "$", re.IGNORECASE if os.name == "nt" else 0)


def glob(pattern: str, path: str = ".", max_results: int = 300, include_all: bool = False) -> str:
    if not pattern:
        raise DevError("give a pattern, e.g. **/*.py")
    top = _top(path)
    if os.path.isfile(top):
        raise DevError("glob needs a folder")
    rx = _glob_regex(pattern)
    limit = max(1, min(int(max_results or 300), 2000))
    stats: dict = {}
    found = []
    end = time.monotonic() + DEADLINE
    stopped = ""
    for f in walk(top, include_all, stats):
        if time.monotonic() > end:
            stopped = "stopped after 25 seconds"
            break
        rel = os.path.relpath(f, top).replace("\\", "/")
        if rx.match(rel) or ("/" not in pattern.replace("\\", "/") and rx.match(os.path.basename(f))):
            try:
                found.append((os.path.getmtime(f), f))
            except OSError:
                continue
    found.sort(reverse=True)
    shown = [f for _, f in found[:limit]]
    head = f"{len(found)} file{'s' if len(found) != 1 else ''} match" + (f" (newest {limit} shown)" if len(found) > limit else "")
    if stats.get("blocked"):
        head += f"; {stats['blocked']} on the no-read list were left out"
    if stopped:
        head += f"; {stopped}"
    return head + ("\n\n" + "\n".join(shown) if shown else "")


def read_lines(path: str, start: int = 1, end: int = 0, max_lines: int = 400) -> str:
    top = _top(path)
    if os.path.isdir(top):
        raise DevError("that is a folder; use glob or the file tools to list it")
    lines = _text_lines(top)
    if lines is None:
        raise DevError("that file is binary or over 5 MB")
    n = len(lines)
    start = max(1, int(start or 1))
    last = min(n, int(end) if end else start + max_lines - 1, start + max_lines - 1)
    if start > n:
        raise DevError(f"the file has {n} lines")
    width = len(str(last))
    body = "\n".join(f"{i:>{width}}\t{_clip(lines[i - 1], 1000)}" for i in range(start, last + 1))
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + "\n[... cut]"
    more = f"; ask again with start={last + 1} for the rest" if last < n else ""
    return f"{top}: lines {start}-{last} of {n}{more}\n\n{body}"


TOOLS = [
    {"name": "grep", "description": "Search inside files for text or a regular expression, under a folder or in one file. Returns "
                                    "path:line: text. Use glob to limit it to some files (e.g. *.py) and context for surrounding lines.",
     "inputSchema": {"type": "object", "properties": {
         "pattern": {"type": "string"}, "path": {"type": "string", "description": "a folder or a file; default the current folder"},
         "glob": {"type": "string", "description": "only files matching this, e.g. *.py"},
         "ignore_case": {"type": "boolean"}, "regex": {"type": "boolean", "description": "default true; false searches for the text as it is"},
         "context": {"type": "integer", "description": "lines around each match, 0-5"}, "max_results": {"type": "integer"}},
         "required": ["pattern"]}, "annotations": {"readOnlyHint": True}},
    {"name": "glob", "description": "Find files by name pattern under a folder, e.g. **/*.py or src/**/test_*.cpp. Newest first.",
     "inputSchema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"},
                                                       "max_results": {"type": "integer"}}, "required": ["pattern"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "read_lines", "description": "Read a numbered range of lines of one text file (default the first 400; pass start and end). "
                                           "Use it after grep to read around a match.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"},
                                                       "end": {"type": "integer"}}, "required": ["path"]},
     "annotations": {"readOnlyHint": True}},
]


def call(name: str, args: dict) -> dict:
    try:
        if name == "grep":
            text = grep(args.get("pattern", ""), args.get("path", "."), args.get("glob", ""), bool(args.get("ignore_case")),
                        args.get("regex", True) is not False, args.get("context", 0), args.get("max_results", 200))
        elif name == "glob":
            text = glob(args.get("pattern", ""), args.get("path", "."), args.get("max_results", 300))
        elif name == "read_lines":
            text = read_lines(args.get("path", ""), args.get("start", 1), args.get("end", 0))
        else:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except DevError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (ValueError, TypeError, OSError) as e:
        return {"content": [{"type": "text", "text": f"could not do that: {e}"}], "isError": True}


def handle(msg: dict):
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if "id" not in msg:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                                                          "capabilities": {"tools": {}},
                                                          "serverInfo": {"name": "strata-dev", "version": "1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": rid, "result": call(params.get("name"), params.get("arguments") or {})}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}}


def main() -> None:
    for f in (sys.stdin, sys.stdout, sys.stderr):        # MCP's stdio is UTF-8, whatever the Windows code page is
        f.reconfigure(encoding="utf-8")
    lock = threading.Lock()

    def answer(msg):
        r = handle(msg)
        if r is not None:
            with lock:
                sys.stdout.write(json.dumps(r) + "\n")
                sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        if msg.get("method") == "tools/call":
            threading.Thread(target=answer, args=(msg,), daemon=True).start()
        else:
            answer(msg)


if __name__ == "__main__":
    main()
