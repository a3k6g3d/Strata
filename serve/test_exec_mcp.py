"""serve/test_exec_mcp.py - tools/strata_exec_mcp.py: what it runs, how it ends a runaway, what it returns, and its MCP
protocol.  These tests run only harmless commands (echo, arithmetic, a short sleep) in a temp folder.

    python -m unittest serve.test_exec_mcp -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import strata_exec_mcp as x  # noqa: E402

WIN = os.name == "nt"


@unittest.skipUnless(WIN, "run_command is tested here against PowerShell and cmd")
class Commands(unittest.TestCase):
    def test_output_and_exit_code(self):
        out = x.run_command("Write-Output 'hello from strata'")
        self.assertIn("exit code 0", out)
        self.assertIn("--- stdout ---\nhello from strata", out)
        self.assertNotIn("--- stderr ---", out)

    def test_a_failing_command_reports_its_exit_code_and_stderr(self):
        out = x.run_command("[Console]::Error.WriteLine('bad thing'); exit 3")
        self.assertIn("exit code 3", out)
        self.assertIn("--- stderr ---\nbad thing", out)

    def test_cmd_and_utf8(self):
        self.assertIn("hello cmd", x.run_command("echo hello cmd", shell="cmd"))
        self.assertIn("héllo 世界", x.run_command("Write-Output 'héllo 世界'"))

    def test_the_folder_it_runs_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = x.run_command("(Get-Location).Path", cwd=tmp)
            self.assertIn(os.path.basename(tmp), out)
        with self.assertRaises(x.ExecError):
            x.run_command("echo x", cwd=os.path.join(tempfile.gettempdir(), "no_such_folder_strata"))

    def test_a_runaway_is_stopped_with_its_whole_tree(self):
        t = time.monotonic()
        out = x.run_command("Start-Sleep -Seconds 30", timeout_s=1)
        self.assertIn("timed out after 1 s and was stopped", out)
        self.assertLess(time.monotonic() - t, 12)
        marker = f"strata_exec_probe_{os.getpid()}"
        # a child that outlives its parent must die with it: start a long sleeper under the shell, then time out
        x.run_command(f"Start-Process -FilePath powershell -ArgumentList '-NoProfile','-Command','# {marker}; Start-Sleep 60' -NoNewWindow -Wait", timeout_s=2)
        left = subprocess.run(["powershell", "-NoProfile", "-Command",
                               f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{marker}*' -and $_.ProcessId -ne $PID }} | Measure-Object | % Count"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "0", "the child was left running")

    def test_input_is_closed(self):
        out = x.run_command("$x = [Console]::In.ReadLine(); if ($null -eq $x) { 'no input' } else { 'got input' }", timeout_s=20)
        self.assertIn("no input", out)

    def test_output_is_cut_but_keeps_both_ends(self):
        out = x.run_command("Write-Output 'START'; Write-Output ('x' * 60000); Write-Output 'END'")
        self.assertIn("characters cut", out)
        self.assertIn("START", out)
        self.assertIn("END", out)
        self.assertLess(len(out), x.MAX_OUT + 400)

    def test_refusals(self):
        for bad, text in ((lambda: x.run_command(""), "give a command"), (lambda: x.run_command("a" * 9000), "longer than"),
                          (lambda: x.run_command("echo x", shell="bash"), "shell must be")):
            with self.assertRaises(x.ExecError) as cm:
                bad()
            self.assertIn(text, str(cm.exception))


class Python(unittest.TestCase):
    def test_output(self):
        out = x.run_python("print(17 * 23)")
        self.assertIn("exit code 0", out)
        self.assertIn("--- stdout ---\n391", out)

    def test_an_error_returns_the_traceback(self):
        out = x.run_python("raise ValueError('boom')")
        self.assertIn("exit code 1", out)
        self.assertIn("ValueError: boom", out)

    def test_isolated_fresh_interpreter_no_input_and_cleanup(self):
        before = set(Path(tempfile.gettempdir()).glob("strata-py-*"))
        out = x.run_python("import sys\nprint(sys.flags.isolated)\ntry:\n    input()\nexcept EOFError:\n    print('no input')")
        self.assertIn("1\nno input", out)
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("strata-py-*")), before)      # the program's folder is removed

    def test_timeout_and_refusals(self):
        self.assertIn("timed out after 1 s", x.run_python("import time\ntime.sleep(30)", timeout_s=1))
        for bad in ("", "   ", "x = 1\n" * 40000):
            with self.assertRaises(x.ExecError):
                x.run_python(bad)

    def test_utf8_and_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = x.run_python("import os\nprint('é世', os.path.basename(os.getcwd()))", cwd=tmp)
            self.assertIn("é世 " + os.path.basename(tmp), out)


class Protocol(unittest.TestCase):
    def test_mcp_session(self):
        p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "strata_exec_mcp.py")], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            def ask(i, method, params=None):
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
                p.stdin.flush()
                return json.loads(p.stdout.readline())
            self.assertEqual(ask(1, "initialize", {})["result"]["serverInfo"]["name"], "strata-exec")
            tools = ask(2, "tools/list")["result"]["tools"]
            self.assertEqual([t["name"] for t in tools], ["run_command", "run_python"])
            self.assertTrue(all(t["annotations"]["destructiveHint"] and not t["annotations"]["readOnlyHint"] for t in tools))
            r = ask(3, "tools/call", {"name": "run_python", "arguments": {"code": "print('over the wire')"}})["result"]
            self.assertFalse(r["isError"])
            self.assertIn("over the wire", r["content"][0]["text"])
            r = ask(4, "tools/call", {"name": "run_command", "arguments": {"command": ""}})["result"]
            self.assertTrue(r["isError"])
            self.assertTrue(ask(5, "tools/call", {"name": "nope", "arguments": {}})["result"]["isError"])
        finally:
            p.stdin.close()
            p.wait(timeout=10)
            p.stdout.close()


if __name__ == "__main__":
    unittest.main()
