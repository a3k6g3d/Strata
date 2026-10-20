"""One launch, to see a real state_hash land in a results.json (docs/BENCH_NOISE.md item 5).

The harness only fills the fingerprint when it starts the server itself -- a server that was already
running was started without STRATA_STATE_HASH and stays that way (the limit is written down in the doc).
So this: stop the live server, run the harness on the agent suite (two runs), and make sure a server is
running again at the end.

Run it from your own terminal, or from a Claude tool runner -- either is safe now, because the first thing
this file does is re-launch itself out of the launcher's process tree (a CIM process create, so the child's
parent is the WMI service) and exit.  It has to be outside the tree because it kills that tree.  The first
run died exactly that way: launched by a tool runner, which on this machine hangs under the server, so the
driver's own taskkill took it down in the same second.

Two things this machine does that the first two runs tripped over, both handled below:

  - netstat reports pid 0 for the listener on 8080, so the process has to be found by its command line;
  - the desktop launcher (strata-desktop -> run-strata.ps1) restarts the server about a minute after it
    dies, so the port is never quiet long enough to wait for quiet -- kill, then go, fast.

  python tools/state_hash_check.py
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "bench" / "results" / "state-hash-check"
LOG = ROOT / "bench" / "results" / "state-hash-check.log"
PORT = 8080
SERVER_CMD = [sys.executable, "-u", "-m", "serve.server", "--engine", "strata",
              "--config", "J:/Strata-work/strata.json", "--port", str(PORT)]
HARNESS = [sys.executable, str(ROOT / "tools" / "strata_bench.py"),
           "--config", "J:/Strata-work/strata.json", "--tests", "agent", "--runs", "2",
           "--out", str(OUT)]

_log = open(LOG, "a", encoding="utf-8", buffering=1)


def say(msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S}  {msg}"
    print(line)
    _log.write(line + "\n")


def ps(cmd: str) -> str:
    """PowerShell, with the filter passed as one argument -- quoting it the other way is a trap here."""
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                              capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def server_pids() -> list:
    """Every python process running serve.server, by its command line.

    Name is filtered too, on purpose: the powershell that runs this very query has 'serve.server' in its
    own command line and would otherwise be reported as a server -- and then killed as one.
    """
    out = ps("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
             "$_.CommandLine -like '*serve.server*' } | ForEach-Object { $_.ProcessId }")
    return [int(x) for x in out.split() if x.strip().isdigit()]


def port_listening() -> bool:
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    for ln in out.splitlines():
        f = ln.split()
        if len(f) == 5 and f[1].endswith(f":{PORT}") and f[3] == "LISTENING":
            return True
    return False


def kill(pid: int) -> None:
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                   capture_output=True, text=True, timeout=60)


def relaunch_outside_the_tree() -> None:
    """Re-launch this same file through the WMI service and exit, so the one that does the work is not
    inside the process tree that the work kills.  `--escaped` is what the child gets; it must not loop."""
    cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}" --escaped'
    out = ps("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
             f"-Arguments @{{CommandLine='{cmd}'}}; $r.ProcessId")
    print(f"re-launched outside this launcher's tree: pid {out.strip() or '(no answer)'}")


def main() -> int:
    if "--escaped" not in sys.argv:
        relaunch_outside_the_tree()
        return 0
    say("check up (nothing stopped yet)")
    say(f"  my process tree: {Path(sys.executable).name}({os.getpid()})")
    pids = server_pids()
    say(f"  what I will stop: {pids or 'nothing'} -- {' '.join(SERVER_CMD)}")
    say(f"  what I will run : {' '.join(HARNESS)}")
    say(f"  where I will put it: {OUT}")

    # Poll the process, not the port: on this machine the listener belongs to pid 0 (the desktop app
    # holds it), so the port can look alive with no server behind it, and the desktop launcher restarts
    # a server it finds dead within a couple of minutes.  The harness has to reach the port first.
    for pid in pids:
        say(f"stopping pid {pid} ...")
        kill(pid)
    if pids:
        for _ in range(30):
            time.sleep(1)
            if not server_pids():
                break
            new = [p for p in server_pids() if p not in pids]
            if new:  # the launcher restarted one: kill the new one too, and go
                say(f"the launcher restarted the server as pid {new}; stopping that one too")
                for p in new:
                    kill(p)
                break
        else:
            say("a serve.server process is still there after 30 s -- running anyway; the harness will say "
                "whether it started its own server")
        say(f"serve.server now: {server_pids() or 'none'}, port listening: {port_listening()}")

    say("running the harness (it starts its own server, which is the point)")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(HARNESS, cwd=str(ROOT), capture_output=True, text=True, timeout=3600)
    say(f"harness exited {r.returncode} after {(time.time() - t0) / 60:.1f} min")

    if pids:
        back = server_pids()
        if back:
            say(f"a server is running again (pid {back}) -- the launcher restarted it; leaving that one alone")
        else:
            say(f"restarting the server in {ROOT}: {' '.join(SERVER_CMD)}")
            subprocess.Popen(SERVER_CMD, cwd=str(ROOT),
                             stdout=open(ROOT / "bench" / "results" / "state-hash-check-server.log",
                                         "a", encoding="utf-8"),
                             stderr=subprocess.STDOUT)
            for _ in range(60):
                if port_listening():
                    say("server back")
                    break
                time.sleep(1)
            else:
                say("server not back on the port after 60 s -- look at state-hash-check-server.log")

    say("what got recorded:")
    found = sorted(OUT.rglob("runs.jsonl"))
    for name in found:
        try:
            rows = [json.loads(ln) for ln in name.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except (OSError, ValueError) as exc:
            say(f"  {name}: unreadable ({exc})")
            continue
        say(f"  {name}: {len(rows)} runs")
        for row in rows:
            say(f"    {row.get('start')} arm {row.get('arm')!r}: state_hash = {row.get('state_hash')!r}")
    if not found:
        say("  no runs.jsonl anywhere under the output folder")
    return 0


if __name__ == "__main__":
    sys.exit(main())
