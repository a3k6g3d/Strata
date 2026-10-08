"""serve/test_dev_mcp.py - tools/strata_dev_mcp.py, the code-search tool: what it finds, what it never touches, and its
MCP protocol.  Everything runs on a throwaway folder; nothing needs a server, a model or the internet.

    python -m unittest serve.test_dev_mcp -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import strata_dev_mcp as d  # noqa: E402


def write(path, text, binary=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if binary else "w", encoding=None if binary else "utf-8", newline=None if binary else "\n") as f:
        f.write(text)


class Tree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        r = self.root
        write(os.path.join(r, "a.py"), "import os\n# the needle is here\nprint('x')\n")
        write(os.path.join(r, "sub", "b.txt"), "no match\nanother NEEDLE line\nlast\n")
        write(os.path.join(r, "sub", "deep", "c.py"), "needle = 1\n")
        write(os.path.join(r, "secret", "d.txt"), "the needle is secret\n")                  # a blocked folder
        write(os.path.join(r, "keys.pem"), "needle in a key\n")                               # a blocked pattern
        write(os.path.join(r, "node_modules", "x.js"), "needle\n")                            # junk folder: skipped
        write(os.path.join(r, "bin.dat"), b"\x00\x01needle\x00", binary=True)                 # binary: skipped
        write(os.path.join(r, "big.txt"), "needle " + "x" * 5_100_000)                        # over 5 MB: skipped
        d.configure([os.path.normcase(os.path.join(r, "secret"))], ["*.pem"])

    def tearDown(self):
        d.configure([], [])
        self.tmp.cleanup()

    def test_grep_finds_text_and_never_the_blocked_places(self):
        out = d.grep("needle", self.root, ignore_case=True)
        self.assertIn(os.path.join(self.root, "a.py") + ":2:", out)
        self.assertIn(os.path.join(self.root, "sub", "b.txt") + ":2:", out)
        self.assertIn(os.path.join(self.root, "sub", "deep", "c.py") + ":1:", out)
        for hidden in ("d.txt", "secret", "keys.pem", "x.js", "bin.dat", "big.txt"):
            self.assertNotIn(hidden, out.split("\n\n", 1)[-1], hidden)                       # not in any result line
        self.assertIn("3 matches in 3 files", out)
        self.assertIn("2 on the no-read list", out)                                           # secret/ and keys.pem
        self.assertIn("skipped 2 binary or over 5 MB", out)

    def test_grep_options(self):
        self.assertIn("1 match in 1 file", d.grep("needle", self.root, glob_filter="*.txt", ignore_case=True))
        self.assertIn("1 match in 1 file", d.grep("NEEDLE", self.root))                       # case matters by default: only b.txt
        self.assertIn("3 matches", d.grep("needle", self.root, ignore_case=True))
        self.assertIn("1 match in 1 file", d.grep("print('x')", self.root, regex=False))      # regex=False: the text as it is
        self.assertIn("0 matches", d.grep("print('x')", self.root))                           # as a regex the ( ) are a group
        ctx = d.grep("needle is here", os.path.join(self.root, "a.py"), context=1)            # a single file, with context
        self.assertIn("a.py-1- import os", ctx)
        self.assertIn("a.py:2: # the needle is here", ctx)
        self.assertIn("a.py-3- print('x')", ctx)
        capped = d.grep("o", self.root, max_results=2)
        self.assertIn("(showing the first 2)", capped)

    def test_grep_errors(self):
        for bad, text in ((lambda: d.grep("(", self.root), "bad pattern"), (lambda: d.grep("", self.root), "pattern"),
                          (lambda: d.grep("x", os.path.join(self.root, "nope")), "no such"),
                          (lambda: d.grep("x", os.path.join(self.root, "secret")), "no-read list"),
                          (lambda: d.grep("x", os.path.join(self.root, "keys.pem")), "no-read list")):
            with self.assertRaises(d.DevError) as cm:
                bad()
            self.assertIn(text, str(cm.exception))

    def test_glob(self):
        out = d.glob("**/*.py", self.root)
        self.assertIn("2 files match", out)
        self.assertIn(os.path.join(self.root, "sub", "deep", "c.py"), out)
        self.assertIn("1 file match", d.glob("sub/*.txt", self.root))
        self.assertIn("2 files match", d.glob("*.txt", self.root))                            # a bare name pattern is recursive (b.txt, big.txt)
        self.assertNotIn("keys.pem", d.glob("**/*", self.root))
        self.assertNotIn("d.txt", d.glob("**/*", self.root))
        self.assertIn("on the no-read list were left out", d.glob("**/*", self.root))
        with self.assertRaises(d.DevError):
            d.glob("**/*", os.path.join(self.root, "secret"))

    def test_read_lines(self):
        p = os.path.join(self.root, "sub", "b.txt")
        out = d.read_lines(p, 2, 3)
        self.assertIn("lines 2-3 of 3", out)
        self.assertIn("2\tanother NEEDLE line", out)
        self.assertNotIn("no match", out)
        self.assertIn("lines 1-3 of 3", d.read_lines(p))
        for bad, text in ((lambda: d.read_lines(p, 9), "has 3 lines"), (lambda: d.read_lines(self.root), "folder"),
                          (lambda: d.read_lines(os.path.join(self.root, "bin.dat")), "binary"),
                          (lambda: d.read_lines(os.path.join(self.root, "secret", "d.txt")), "no-read list")):
            with self.assertRaises(d.DevError) as cm:
                bad()
            self.assertIn(text, str(cm.exception))
        write(os.path.join(self.root, "long.txt"), "\n".join(f"line {i}" for i in range(1, 1001)))
        self.assertIn("lines 1-400 of 1000; ask again with start=401", d.read_lines(os.path.join(self.root, "long.txt")))


class Protocol(unittest.TestCase):
    def test_mcp_session_and_the_block_list_from_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            write(os.path.join(root, "ok.txt"), "find me\n")
            write(os.path.join(root, "hidden", "no.txt"), "find me too\n")
            env = dict(os.environ, STRATA_BLOCKED_JSON=json.dumps({"prefixes": [os.path.normcase(os.path.join(root, "hidden"))], "globs": []}))
            p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "strata_dev_mcp.py")], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env)
            try:
                def ask(i, method, params=None):
                    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
                    p.stdin.flush()
                    return json.loads(p.stdout.readline())
                self.assertEqual(ask(1, "initialize", {})["result"]["serverInfo"]["name"], "strata-dev")
                tools = ask(2, "tools/list")["result"]["tools"]
                self.assertEqual([t["name"] for t in tools], ["grep", "glob", "read_lines"])
                self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
                r = ask(3, "tools/call", {"name": "grep", "arguments": {"pattern": "find me", "path": root}})["result"]
                text = r["content"][0]["text"]
                self.assertFalse(r["isError"])
                self.assertIn("1 match in 1 file", text)
                self.assertNotIn("no.txt", text)
                r = ask(4, "tools/call", {"name": "read_lines", "arguments": {"path": os.path.join(root, "hidden", "no.txt")}})["result"]
                self.assertTrue(r["isError"])
                self.assertIn("no-read list", r["content"][0]["text"])
            finally:
                p.stdin.close()
                p.wait(timeout=10)
                p.stdout.close()


if __name__ == "__main__":
    unittest.main()
