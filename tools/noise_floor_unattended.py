#!/usr/bin/env python3
"""Stop the live server, run the noise floor, bring the server back.

Written because the run has to survive the thing it interrupts: the live Strata on
port 8080 is the server this session is talking through, so stopping it drops the
session, and a driver that is a child of the session dies with it.  Hence detached.

The order is fixed by the harness's own guard (strata_ab.py refuses to start while
something answers on the port, for the reason in its docstring): stop, wait for the
GPU to actually be free, run, restart.  Restarting before the run would put the
server back inside the arms' seconds.

Nothing here is a guess about the live server: its command line is read from the
process table at the moment it is stopped, and written to the log verbatim, so the
restart is the same command that was running - not one reconstructed from a config
file that may have moved since.
"""
from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "bench" / "results" / "noise-floor-unattended.log"

PORT = 8080
# the arm: the live config, the suite the sweep on disk used, 3 launches x 2 runs
HARNESS = [sys.executable, "-u", str(ROOT / "tools" / "strata_ab.py"),
           "--config", "J:/Strata-work/strata.json", "--tests", "agent",
           "--runs", "6", "--noise-floor", "3"]
READY_TIMEOUT = 900          # seconds to wait for the GPU to actually empty
RESTART_WAIT = 240           # seconds to give the server to come back up


def say(msg: str) -> None:
    line = time.strftime("%H:%M:%S  ") + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def answers(port: int, timeout: float = 5.0) -> bool:
    """Does something answer on the port?  urllib, not Invoke-WebRequest: PowerShell
    in NonInteractive mode throws a prompt error instead of a connection error, which
    is exactly how a live port got read as quiet once already."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout):
            return True
    except Exception:
        return False


def live_server(port: int):
    """The process serving this port: a python running serve.server with --port <port>."""
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
        except Exception:
            continue
        if "serve.server" in cmd and f"--port {port}" in cmd:
            return p, cmd
    return None, None


def mine() -> set:
    """Every pid on this driver's own ancestry and descent lines."""
    me = psutil.Process()
    out = {me.pid}
    d = me
    while True:                                   # up: the driver must not kill its own parent
        try:
            p = d.parent()
        except Exception:
            p = None
        if p is None:
            break
        out.add(p.pid)
        d = p
    try:                                         # down: nor anything it started
        out |= {c.pid for c in me.children(recursive=True)}
    except Exception:
        pass
    return out


def children(p) -> list:
    """The server's descendants, minus this driver's own family.

    Recursive, because the engine and the vision sidecar are children of the server and
    must go with it.  But the session talking through that server is ALSO under it - the
    tool that launched this driver sits three levels down from the server process - so a
    driver that kills the whole subtree kills the run it is driving.  Hence the exclusion.
    """
    keep = mine()
    try:
        return [c for c in p.children(recursive=True) if c.pid not in keep]
    except Exception:
        return []


def main() -> int:
    say(f"driver up (pid {psutil.Process().pid}); log: {LOG}")

    p, cmd = live_server(PORT)
    if p is None:
        say("no live server found on the port - nothing to stop; running anyway")
    else:
        kids = children(p)
        say(f"stopping pid {p.pid} ({p.name()}): {cmd}")
        for k in kids:
            say(f"  child pid {k.pid} ({k.name()})")
        # terminate the children first, then the server: the engine is the thing holding
        # 11.5 GiB of VRAM, and killing the parent first can leave it orphaned on the card
        for k in kids:
            try:
                k.terminate()
            except Exception as e:
                say(f"  could not terminate pid {k.pid}: {e}")
        try:
            p.terminate()
        except Exception as e:
            say(f"  could not terminate pid {p.pid}: {e}")
        gone = psutil.wait_procs([p] + kids, timeout=60)
        for g in gone:
            say(f"  pid {g.pid} gone" + (f" (rc {g.returncode})" if g.returncode is not None else ""))
        still = [g for g in (kids + [p]) if g.is_running() and g.pid != psutil.Process().pid]
        for g in still:
            say(f"  pid {g.pid} still up after 60 s; it may still hold VRAM")

    # wait for the GPU to be genuinely empty, not merely for the port to be quiet:
    # a process can outlive the port it served, and an arm measured beside a corpse
    # is the mistake the guard exists to stop
    t0 = time.time()
    while time.time() - t0 < READY_TIMEOUT:
        left = [q for q in psutil.process_iter(["name"])
                if (q.info["name"] or "").lower().startswith("strata")
                and q.pid != psutil.Process().pid]
        if not answers(PORT) and not left:
            say(f"GPU free: no server on {PORT}, no strata process, after {time.time() - t0:.0f} s")
            break
        time.sleep(5)
    else:
        say(f"gave up waiting {READY_TIMEOUT} s for the GPU to empty; not running - the numbers would be meaningless")
        return 1

    say("running: " + " ".join(HARNESS[1:]))
    t = time.time()
    rc = subprocess.call(HARNESS)
    say(f"harness exited {rc} after {(time.time() - t) / 60:.1f} min")

    if cmd:
        say("bringing the live server back: " + cmd)
        head, *rest = cmd.split()
        try:
            subprocess.Popen([head, *rest])
        except Exception as e:
            say(f"  could not restart it: {e} - start it by hand")
            return 1
        t0 = time.time()
        while time.time() - t0 < RESTART_WAIT:
            if answers(PORT, timeout=5):
                say(f"server answers again after {time.time() - t0:.0f} s")
                return 0
            time.sleep(5)
        say(f"server did not answer within {RESTART_WAIT} s - check it by hand")
        return 1
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
