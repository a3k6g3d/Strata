#!/usr/bin/env python3
"""tools/wait_for.py - block until a long run says it is done, then show the tail.

Instead of polling a log with `Start-Sleep 240; Get-Content -Tail 20` over and over, wait ONCE for what you are waiting for:

    python tools/wait_for.py --file run.log --pattern "ALL DONE|Traceback"
    python tools/wait_for.py --pid 12345 --file run.log --timeout 3600
    python tools/wait_for.py --file run.log --pattern "launch 3 done" --tail 40

It returns as soon as the first of these happens, and prints the last --tail lines of the file (the matching line is marked) so the reply to
the wait is the state of the run:

    exit 0   the pattern appeared (only text written after the wait started counts, unless --from-start)
    exit 1   the process --pid ended (the pattern did not appear first)
    exit 2   --timeout seconds passed
    exit 3   nothing to wait for (no --pattern and no --pid)

A log that does not exist yet is waited for.  UTF-16 logs (PowerShell's `>` and Out-File) are read as such.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259                                  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_text(path: str) -> str | None:
    """The file's text, or None when it does not exist (yet).  A UTF-16 file (with its byte-order mark) is decoded as UTF-16."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


def wait(file: str | None = None, pattern: str | None = None, pid: int | None = None, timeout: float = 0.0, poll: float = 1.0,
         from_start: bool = False, alive=pid_alive, clock=time.monotonic, sleep=time.sleep) -> tuple:
    """-> (reason, the matching line or None): reason is "pattern", "exited" or "timeout" (or "nothing" when there was nothing to wait for)."""
    if not pattern and not pid:
        return "nothing", None
    rx = re.compile(pattern) if pattern else None
    start_text = read_text(file) if file else None
    seen = 0 if from_start else len(start_text or "")                      # only what is written after this point counts
    deadline = clock() + timeout if timeout and timeout > 0 else None
    while True:
        if file and rx:
            text = read_text(file)
            if text is not None:
                if len(text) < seen:                                       # the file was started over
                    seen = 0
                for line in text[seen:].splitlines():
                    if rx.search(line):
                        return "pattern", line
                seen = len(text)
        if pid and not alive(pid):
            return "exited", None
        if deadline is not None and clock() >= deadline:
            return "timeout", None
        sleep(poll)


def tail_lines(file: str | None, n: int) -> list:
    text = read_text(file) if file else None
    return (text or "").splitlines()[-n:] if n > 0 else []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", help="the log to read")
    ap.add_argument("--pattern", help="a regular expression: return when a line of the log matches it")
    ap.add_argument("--pid", type=int, help="return when this process has ended")
    ap.add_argument("--timeout", type=float, default=0.0, help="give up after this many seconds (default: never)")
    ap.add_argument("--tail", type=int, default=20, help="lines of the log to print at the end (default 20)")
    ap.add_argument("--poll", type=float, default=1.0, help="seconds between looks (default 1)")
    ap.add_argument("--from-start", action="store_true", help="a match anywhere in the file counts, not only in text written from now on")
    a = ap.parse_args(argv)
    if a.pattern and not a.file:
        ap.error("--pattern needs --file")
    t0 = time.monotonic()
    reason, line = wait(a.file, a.pattern, a.pid, a.timeout, a.poll, a.from_start)
    if reason == "nothing":
        print("nothing to wait for: give --pattern (with --file) and/or --pid", file=sys.stderr)
        return 3
    note = {"pattern": f"matched after {time.monotonic() - t0:.0f} s: {line}", "exited": f"process {a.pid} ended after {time.monotonic() - t0:.0f} s",
            "timeout": f"timed out after {a.timeout:.0f} s"}[reason]
    print(f"[wait_for] {note}")
    for tl in tail_lines(a.file, a.tail):
        print(("> " if reason == "pattern" and tl == line else "  ") + tl)
    return {"pattern": 0, "exited": 1, "timeout": 2}[reason]


if __name__ == "__main__":
    sys.exit(main())
