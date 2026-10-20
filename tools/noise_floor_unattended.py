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
    """One line to the log, flushed.

    The log is the only thing that survives this run: the driver's stdout is whatever
    launched it, and a WMI-launched process has no console at all.  The last run died
    between two prints in the same second, which is why the fix is a print per step
    rather than a summary at the end.
    """
    line = time.strftime("%H:%M:%S  ") + msg
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
    try:
        print(line, flush=True)
    except Exception:
        pass


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
    """The process serving this port: a python running serve.server with --port <port>.

    Returns (process, argv_list, display_string).  The argv list is what gets restarted:
    re-splitting the display string would break on "C:\\Program Files\\..." at its space,
    which is exactly this server's path.
    """
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            argv = p.info["cmdline"] or []
        except Exception:
            continue
        cmd = " ".join(argv)
        if "serve.server" in cmd and f"--port {port}" in cmd:
            return p, argv, cmd
    return None, [], None


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


def all_children(p) -> list:
    try:
        return p.children(recursive=True)
    except Exception:
        return []


def children(p) -> list:
    """The server's descendants, minus this driver's own family.

    Recursive, because the engine and the vision sidecar are children of the server and
    must go with it.  But the session talking through that server is ALSO under it - the
    tool that launched this driver sits three levels down from the server process - so a
    driver that kills the whole subtree kills the run it is driving.  Hence the exclusion.
    """
    keep = mine()
    return [c for c in all_children(p) if c.pid not in keep]


def tree_of_me() -> list:
    """This driver's own ancestry, written down before anything is killed.

    The last run put pid 25444 - which was the session's own tool runner - on the kill
    list, so the exclusion did not see it, so the driver was NOT under the session at
    that moment; and it still died in the same second it killed that pid.  The log could
    not tell those two apart.  So the driver writes its own parent chain down first.
    """
    out = []
    d = psutil.Process()
    while d is not None:
        try:
            out.append((d.pid, d.name()))
            d = d.parent()
        except Exception:
            break
    return out


def check(p, argv, cmd) -> int:
    """What --check prints: what would be stopped, and what would be spared. Kills nothing."""
    print("process tree (this one first):")
    for pid, name in tree_of_me():
        print(f"   {name}({pid})")
    print()
    if p is None:
        print("no live server found on the port - nothing to stop; the run would go ahead anyway")
        return 0
    print("would stop:")
    print(f"   pid {p.pid} ({p.name()})  cwd: {p.cwd()}")
    print(f"     {cmd}")
    print("   and restart it as exactly that, in that directory")
    keep = mine()
    print("would kill (the server's subtree, minus my own line):")
    for c in all_children(p):
        if c.pid not in keep:
            print(f"     {c.pid:<8} {c.name()}")
    print("would spare (mine):")
    for c in all_children(p):
        if c.pid in keep:
            print(f"     {c.pid:<8} {c.name()}")
    print()
    print("Anything NOT under the server is left alone - including a Claude session that")
    print("is not talking through it. If this list names something you care about, that is")
    print("what gets stopped; check it now rather than after the run.")
    return 0


def main() -> int:
    say(f"driver up (pid {psutil.Process().pid}); log: {LOG}")
    say("my process tree: " + " <- ".join(f"{n}({p})" for p, n in tree_of_me()))

    p, argv, cmd = live_server(PORT)
    srv_cwd = None
    if p is None:
        say("no live server found on the port - nothing to stop; running anyway")
    else:
        try:
            srv_cwd = p.cwd()
        except Exception:
            srv_cwd = None
        say(f"the server's working directory, to restart it in: {srv_cwd or 'unreadable'}")
        keep = mine()
        kids = children(p)
        say(f"stopping pid {p.pid} ({p.name()}): {cmd}")
        say("  spared (my own line): " + (", ".join(f"{c.pid}:{c.name()}" for c in all_children(p) if c.pid in keep) or "(none)"))
        for k in kids:
            say(f"  will kill pid {k.pid} ({k.name()})")
        if "--check" in sys.argv:
            return check(p, argv, cmd)
        # terminate the children first, then the server: the engine is the thing holding
        # 11.5 GiB of VRAM, and killing the parent first can leave it orphaned on the card
        for k in kids:
            say(f"  killing pid {k.pid} ({k.name()}) ...")
            try:
                k.terminate()
                say(f"  pid {k.pid} terminated")
            except Exception as e:
                say(f"  could not terminate pid {k.pid}: {e}")
        say(f"  killing the server pid {p.pid} ...")
        try:
            p.terminate()
            say(f"  server pid {p.pid} terminated")
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
    # the harness's own printout has to go somewhere durable: a WMI-launched process has
    # no console, so without this the run happens and its story is never told
    out_path = LOG.with_name("noise-floor-harness-out.log")
    with open(out_path, "w", encoding="utf-8") as out_fh:
        rc = subprocess.call(HARNESS, stdout=out_fh, stderr=subprocess.STDOUT)
    say(f"harness exited {rc} after {(time.time() - t) / 60:.1f} min; output: {out_path}")

    if argv:
        say("bringing the live server back: " + cmd)
        try:
            subprocess.Popen(argv, cwd=srv_cwd)
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
