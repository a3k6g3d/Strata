#!/usr/bin/env python3
"""tools/strata_exec_mcp.py - run a command or a Python program from Strata's chat (an MCP server over stdio).

    run_command(command, shell, cwd, timeout_s)    one PowerShell (or cmd) command line; output and exit code come back
    run_python(code, cwd, timeout_s)               one Python program (a fresh interpreter, `python -I`); its output comes back

THIS RUNS CODE ON YOUR PC WITH YOUR WINDOWS ACCOUNT'S RIGHTS. It is not a sandbox and cannot be made into one here: a
command can read or change anything you can, reach the network and start other programs. What keeps that safe is in
Strata, not in this file - list this server in the run config under `"mcp": {"exec_servers": ["exec"]}` and Strata
treats every one of its tools as kind "exec":
  - the call WAITS FOR YOUR CLICK, every time, in every mode that can ask (Full access too); there is no "Always allow";
  - Read-only and No-tools modes refuse it;
  - the chat shows the exact command or program before you click;
  - the text is also checked, best effort, against the no-read list (a blocked place named in a command is refused);
    that check cannot see a path a program builds while it runs, so it is a seat belt and the click is the guard.
The no-read list and the C: protection look at the paths a call names; they do not follow what a command then does.

What this server itself does: no input is ever given to the program (stdin is closed); a call ends after `timeout_s`
(default 600, at most 3600) with the whole process tree stopped; each of stdout and stderr is cut at 20,000 characters
(the start and the end are kept); a command line over 8,000 characters is refused.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

MAX_COMMAND = 8000
MAX_OUT = 20_000
DEFAULT_TIMEOUT = 600.0
MAX_TIMEOUT = 3600.0


class ExecError(Exception):
    """A refusal whose message the model reads."""


def _clip(text: str) -> str:
    if len(text) <= MAX_OUT:
        return text
    half = MAX_OUT // 2
    return text[:half] + f"\n[... {len(text) - MAX_OUT:,} characters cut ...]\n" + text[-half:]


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            proc.kill()


def _folder(cwd) -> str | None:
    if not cwd:
        return None
    path = os.path.expandvars(os.path.expanduser(str(cwd)))
    if not os.path.isdir(path):
        raise ExecError(f"no such folder: {path}")
    return path


def _run(argv: list[str], cwd: str | None, timeout_s) -> str:
    try:
        timeout = max(1.0, min(float(timeout_s or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    except (TypeError, ValueError):
        raise ExecError("timeout_s must be a number of seconds") from None
    extra = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {"start_new_session": True}
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, **extra)
    except OSError as e:
        raise ExecError(f"could not start it: {e}") from None
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        out, err = proc.communicate()
    took = time.monotonic() - started
    head = (f"timed out after {timeout:.0f} s and was stopped" if timed_out else f"exit code {proc.returncode}") + f" ({took:.1f} s)"
    parts = [head]
    for label, data in (("stdout", out), ("stderr", err)):
        text = data.decode("utf-8", "replace").replace("\r\n", "\n").strip("\n")
        if text:
            parts.append(f"--- {label} ---\n{_clip(text)}")
    if len(parts) == 1:
        parts.append("(no output)")
    return "\n".join(parts)


def run_command(command: str, shell: str = "", cwd: str = "", timeout_s=DEFAULT_TIMEOUT) -> str:
    command = str(command or "").strip()
    if not command:
        raise ExecError("give a command")
    if len(command) > MAX_COMMAND:
        raise ExecError(f"the command is longer than {MAX_COMMAND} characters; it is refused")
    where = _folder(cwd)
    shell = (shell or ("powershell" if os.name == "nt" else "sh")).lower()
    if shell == "powershell" and os.name == "nt":
        exe = shutil.which("powershell") or "powershell"
        argv = [exe, "-NoProfile", "-NonInteractive", "-Command",
                "$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); " + command]
    elif shell == "cmd" and os.name == "nt":
        argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", "chcp 65001 >nul & " + command]
    elif shell == "sh" and os.name != "nt":
        argv = ["/bin/sh", "-c", command]
    else:
        raise ExecError("shell must be 'powershell' or 'cmd' on Windows, 'sh' elsewhere")
    return _run(argv, where, timeout_s)


def run_python(code: str, cwd: str = "", timeout_s=DEFAULT_TIMEOUT) -> str:
    code = str(code or "")
    if not code.strip():
        raise ExecError("give the program to run")
    if len(code) > 100_000:
        raise ExecError("the program is longer than 100,000 characters; it is refused")
    where = _folder(cwd)
    tmp = tempfile.mkdtemp(prefix="strata-py-")
    try:
        path = os.path.join(tmp, "main.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        # -I: isolated (no user site, no PYTHON* variables, no script folder on the path); -X utf8: UTF-8 output
        return _run([sys.executable, "-I", "-X", "utf8", path], where or tmp, timeout_s)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


TOOLS = [
    {"name": "run_command", "description": "Run one command line on the user's PC (PowerShell by default; shell='cmd' for cmd.exe) and "
                                            "return its exit code, stdout and stderr. Every call waits for the user's approval, who sees "
                                            "the exact command. No input can be given to it; it is stopped after timeout_s (default 600, "
                                            "max 300).",
     "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}, "shell": {"type": "string", "enum": ["powershell", "cmd"]},
                                                       "cwd": {"type": "string", "description": "the folder to run in"},
                                                       "timeout_s": {"type": "number"}}, "required": ["command"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}},
    {"name": "run_python", "description": "Run a Python program on the user's PC (a fresh interpreter, python -I) and return its exit code, "
                                           "stdout and stderr. Print what you want to see. Every call waits for the user's approval, who "
                                           "sees the program. No input can be given to it; it is stopped after timeout_s (default 600, max 3600).",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}, "cwd": {"type": "string"}, "timeout_s": {"type": "number"}},
                     "required": ["code"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}},
]


def call(name: str, args: dict) -> dict:
    try:
        if name == "run_command":
            text = run_command(args.get("command", ""), args.get("shell", ""), args.get("cwd", ""), args.get("timeout_s", DEFAULT_TIMEOUT))
        elif name == "run_python":
            text = run_python(args.get("code", ""), args.get("cwd", ""), args.get("timeout_s", DEFAULT_TIMEOUT))
        else:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except ExecError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}


def handle(msg: dict):
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if "id" not in msg:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                                                          "capabilities": {"tools": {}},
                                                          "serverInfo": {"name": "strata-exec", "version": "1"}}}
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
        if msg.get("method") == "tools/call":            # on a thread: a long command must not block the next request
            threading.Thread(target=answer, args=(msg,), daemon=True).start()
        else:
            answer(msg)


if __name__ == "__main__":
    main()
