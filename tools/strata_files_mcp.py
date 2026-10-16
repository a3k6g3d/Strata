#!/usr/bin/env python3
"""tools/strata_files_mcp.py - read, change and create files from Strata's chat (an MCP server over stdio).

    read_file(path, head, tail)                             the whole file as text (or its first / last lines)
    write_file(path, content)                               create it, or replace it (a file you have not read is refused)
    edit_file(path, oldText, newText, replace_all)         change text that must match exactly once

Give the folders it may touch as arguments, one per folder:

    "mcp_servers": {"files": {"command": "python", "args": ["tools/strata_files_mcp.py", "C:\\\\Users\\\\me\\\\Documents\\\\notes"]}}

Anything outside those folders is refused, and so is anything on Strata's no-read list (`mcp.blocked_paths`, handed over
in STRATA_BLOCKED_JSON); links and junctions are followed to what they lead to before that is checked, so a link cannot
step out of the allow-list. A change under one of `mcp.protected_paths` still asks you in every mode - Strata checks the
path in the call, not this server.

Three tools and no more: the official filesystem server has fourteen, and its tool names are a guess for Strata's
permission classifier. These three are named for what they do, and `read_file` / `edit_file` are classed read and write
by their names, so the permission modes work out of the box: Read-only refuses the changes, Edit-changes lets them,
Ask-about-every-change asks for each one, and it asks again for a protected place even in Full access.

What it will not do:
  - replace a file it has not read in this session first, so a guess cannot quietly overwrite real content (an
    edit_file needs the text to match exactly, which is the same guard in a stronger form);
  - touch a binary file or one over 5 MB (the same limits as the code search server);
  - make a folder for a new file - the folder must exist;
  - keep a backup. Version your work (git) or point it at a folder you can restore.

A change keeps the file as it was: its line endings (CRLF or LF), its byte-order mark and its final newline are detected
and written back the same way, so editing a Windows file does not turn it into a Unix file. The text you match is
compared with line endings ignored, so the same edit works on a CRLF file and an LF one.
"""
from __future__ import annotations

import difflib
import fnmatch
import json
import os
import sys
import threading

MAX_FILE = 5_000_000            # read: nothing bigger than this is opened at all
MAX_WRITE = 1_000_000           # write / edit: the new content, in characters
MAX_DIFF = 6_000                # how much of the change is shown back
READ_LIMIT = 40_000             # read_file: characters of text in one answer

_BLOCK = json.loads(os.environ.get("STRATA_BLOCKED_JSON") or "{}")
PREFIXES: list[str] = list(_BLOCK.get("prefixes", []))
GLOBS: list[str] = list(_BLOCK.get("globs", []))
ROOTS: list[str] = []           # the folders this instance may touch (from the command line)
READ: set[str] = set()          # paths read in this session: a change needs one


class FilesError(Exception):
    """A refusal whose message the model reads."""


def configure(prefixes, globs, roots) -> None:
    """The no-read list and the allow-list (tests; the server reads them from the environment and the command line)."""
    PREFIXES[:] = prefixes
    GLOBS[:] = globs
    # A drive root is stored without its trailing separator, so the `root + os.sep` below is one backslash and not two:
    # "J:\\" as it stands would match nothing at all, because a real path under it has a single backslash after the colon.
    ROOTS[:] = [os.path.normcase(os.path.realpath(r)).rstrip("\\/") or r for r in roots]
    READ.clear()


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def is_blocked(path: str) -> bool:
    """Is `path` on the user's no-read list?  (Resolved first: a link to a blocked place is blocked.)"""
    p = _norm(os.path.realpath(path))
    return any(p == x or p.startswith(x + os.sep) for x in PREFIXES) or any(fnmatch.fnmatchcase(p, g) for g in GLOBS)


def locate(path: str, must_exist: bool = True) -> str:
    """The real path of `path`, checked against the allow-list and the no-read list.  A path whose folder does not exist
    is reported as such, because that is what a model needs to know before it tries to create a file."""
    if not path or not str(path).strip():
        raise FilesError("give a path")
    raw = os.path.expandvars(os.path.expanduser(str(path).strip()))
    real = os.path.normcase(os.path.realpath(raw))
    if not ROOTS:
        raise FilesError("this file server was started without any folders; name them as arguments")
    if not any(real == r or real.startswith(r + os.sep) for r in ROOTS):
        raise FilesError("that path is outside the folders this server may use: " + ", ".join(ROOTS))
    if is_blocked(real):
        raise FilesError("that file is on the user's no-read list")
    if must_exist:
        if not os.path.exists(real):
            raise FilesError(f"no such file: {raw}" + ("" if os.path.isdir(os.path.dirname(real) or ".")
                                                       else f" (and its folder does not exist: {os.path.dirname(raw)})"))
        if os.path.isdir(real):
            raise FilesError(f"that is a folder: {raw}")
    elif os.path.isdir(real):
        raise FilesError(f"that path is a folder: {raw}")
    return raw


def _decode(raw: bytes) -> tuple[str, str, str]:
    """The text of a file, and how it was stored: (text with \\n endings, newline it used, encoding it used)."""
    if b"\x00" in raw[:8192]:
        raise FilesError("that file is binary (it has a NUL byte); these tools work on text files")
    bom = raw.startswith(b"\xef\xbb\xbf")
    enc = "utf-8-sig" if bom else "utf-8"
    try:
        text = (raw.decode("utf-8-sig") if bom else raw.decode("utf-8")).replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        raise FilesError("that file is not UTF-8 text; read it another way or convert it first") from None
    return text, ("crlf" if b"\r\n" in raw else "lf"), ("utf-8-bom" if bom else enc)


def _encode(text: str, newline: str, encoding: str) -> bytes:
    body = text.replace("\n", "\r\n") if newline == "crlf" else text
    return body.encode("utf-8-sig" if encoding == "utf-8-bom" else "utf-8")


def _open(path: str) -> tuple[str, str, str]:
    try:
        if os.path.getsize(path) > MAX_FILE:
            raise FilesError("that file is over 5 MB")
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise FilesError(f"no such file: {path}") from None
    except OSError as e:
        raise FilesError(f"could not read it: {e}") from None
    return _decode(raw)


def _save(path: str, text: str, newline: str, encoding: str) -> None:
    """Write it through a temporary file in the same folder, so a failure cannot leave half a file behind."""
    tmp = path + ".strata-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(_encode(text, newline, encoding))
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise FilesError(f"could not write it: {e}") from None


def read_file(path: str, head: int = 0, tail: int = 0) -> str:
    real = locate(path)
    text, newline, _enc = _open(real)
    READ.add(real)
    lines = text.split("\n")
    n = len(lines)
    if head or tail:
        h = max(0, min(int(head or 0), 2000))
        t = max(0, min(int(tail or 0), 2000))
        keep = lines[:h] + (["[...]"] if h and t and n > h + t else []) + (lines[n - t:] if t else [])
        body = "\n".join(keep)
        what = f"the first {h}" + (f" and last {t}" if t else "") if h else f"the last {t}"
        note = f"{real}: {what} of {n} lines\n\n"
    else:
        note = f"{real}: {n} lines, {len(text):,} characters\n\n"
        body = text
    if len(body) > READ_LIMIT:
        body = body[:READ_LIMIT] + f"\n[... cut at {READ_LIMIT:,} characters: pass head or tail to read part of it]"
    return note + body + ("" if newline == "lf" else "\n\n(these lines use CR LF endings; write them back as they are)")


def write_file(path: str, content: str) -> str:
    real = locate(path, must_exist=False)
    content = "" if content is None else str(content)
    if len(content) > MAX_WRITE:
        raise FilesError(f"the new content is longer than {MAX_WRITE:,} characters; it is refused")
    existed = os.path.exists(real)
    if existed:
        if real not in READ:
            raise FilesError(f"{path} exists and has not been read in this session; read it first, so you know what you "
                             "are replacing (read_file, then write_file again)")
        old, newline, encoding = _open(real)
    else:
        if not os.path.isdir(os.path.dirname(real) or ""):
            raise FilesError(f"its folder does not exist: {os.path.dirname(path)}")
        old, newline, encoding = "", "lf", "utf-8"
    if not content.endswith("\n") and old.endswith("\n"):
        content += "\n"                                # keep the file's habit of ending its last line
    _save(real, content, newline, encoding)
    return (f"{'replaced' if existed else 'created'} {path}: {len(old):,} characters -> {len(content):,}"
            + (f" ({old.count(chr(10))} lines -> {content.count(chr(10))})" if "\n" in content else ""))


def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    real = locate(path)
    if not str(old_text or ""):
        raise FilesError("give the text to change (an empty one would match everywhere)")
    old, newline, encoding = _open(real)
    want = str(old_text).replace("\r\n", "\n").replace("\r", "\n")
    have = str(new_text or "").replace("\r\n", "\n").replace("\r", "\n")
    hits = old.count(want)
    if not hits:
        raise FilesError(f"that text is not in the file. Read it again (read_file, or the code search server's "
                         "read_lines) and copy the lines exactly - the match must be character for character.")
    if hits > 1 and not replace_all:
        raise FilesError(f"that text appears {hits} times, so it is not clear which one to change; give more of the "
                         "surrounding lines, or set replace_all to change them all")
    after = old.replace(want, have) if replace_all else old.replace(want, have, 1)
    _save(real, after, newline, encoding)
    READ.add(real)
    diff = "\n".join(l for l in difflib.unified_diff(old.split("\n"), after.split("\n"), lineterm="", n=2)
                     if not l.startswith(("---", "+++", "@@")))
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF] + "\n[... the rest of the change is not shown]"
    return (f"changed {path}: {hits if replace_all else 1} place{'s' if (hits if replace_all else 1) != 1 else ''}"
            + f" ({len(old):,} -> {len(after):,} characters)"
            + (f"\n\n{diff}" if diff else ""))


TOOLS = [
    {"name": "read_file", "description": "Read the complete contents of a file as text, or only its first (head) or last "
                                         "(tail) lines for a long one. Returns the whole file, so for a big one pass head or "
                                         "tail, or use the code search server's read_lines to take a range of lines.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"},
                                                       "head": {"type": "integer", "description": "only the first N lines"},
                                                       "tail": {"type": "integer", "description": "only the last N lines"}},
                                          "required": ["path"]},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
    {"name": "write_file", "description": "Create a new file, or replace an existing one completely. An existing file must "
                                          "have been read first, so real content is never overwritten by a guess; the "
                                          "folder must already exist, and no backup is kept.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                     "required": ["path", "content"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}},
    {"name": "edit_file", "description": "Change text inside a file. oldText must match the file exactly (character for "
                                         "character, and only once unless replace_all is true) - read the file first and "
                                         "copy it. Line endings and the final newline are kept as the file had them. "
                                         "Returns the lines it changed.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"},
                                                       "oldText": {"type": "string", "description": "exactly what is there now"},
                                                       "newText": {"type": "string", "description": "what to put instead"},
                                                       "replace_all": {"type": "boolean", "description": "change every place it appears"}},
                                          "required": ["path", "oldText", "newText"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
]


def call(name: str, args: dict) -> dict:
    try:
        if name == "read_file":
            text = read_file(args.get("path", ""), args.get("head", 0), args.get("tail", 0))
        elif name == "write_file":
            text = write_file(args.get("path", ""), args.get("content", ""))
        elif name == "edit_file":
            text = edit_file(args.get("path", ""), args.get("oldText", ""), args.get("newText", ""),
                             bool(args.get("replace_all")))
        else:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except FilesError as e:
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
                                                         "serverInfo": {"name": "strata-files", "version": "1"}}}
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
    roots = sys.argv[1:]
    if not roots:
        sys.stderr.write("strata_files_mcp: name the folders it may use, e.g. "
                         "python tools/strata_files_mcp.py C:\\Users\\me\\notes\n")
        raise SystemExit(2)
    configure(PREFIXES, GLOBS, roots)
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
