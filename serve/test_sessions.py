"""The chat page's sessions: the store (serve/sessions.py) and the /sessions routes of the server."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serve.frontend import ChatTemplate  # noqa: E402
from serve.server import ByteTokenizer, MockEngine, Service, serve  # noqa: E402
from serve.sessions import SessionError, SessionStore  # noqa: E402


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(os.path.join(self.tmp.name, "sessions"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_list_get_delete(self):
        meta = self.store.save("s-a", "First chat", [{"role": "user", "text": "hi"}])
        self.assertEqual((meta["id"], meta["title"], meta["messages"]), ("s-a", "First chat", 1))
        self.store.save("s-b", "Second", [])
        self.assertEqual([m["id"] for m in self.store.list()], ["s-b", "s-a"])       # newest first
        got = self.store.get("s-a")
        self.assertEqual(got["messages"], [{"role": "user", "text": "hi"}])
        self.assertIsNone(self.store.get("s-none"))
        self.assertTrue(self.store.delete("s-a"))
        self.assertFalse(self.store.delete("s-a"))
        self.assertEqual([m["id"] for m in self.store.list()], ["s-b"])

    def test_created_is_kept_and_updated_moves(self):
        first = self.store.save("s-a", "t", [])
        second = self.store.save("s-a", "t2", [{"role": "user", "text": "x"}])
        self.assertEqual(second["created"], first["created"])
        self.assertGreaterEqual(second["updated"], first["updated"])
        self.assertEqual(second["title"], "t2")

    def test_ids_can_never_name_a_path(self):
        for bad in ("../x", "a/b", "a\\b", "", "A", "s_a", "x" * 65, ".hidden", None, 3):
            with self.assertRaises(SessionError):
                self.store.save(bad, "t", [])
            with self.assertRaises(SessionError):
                self.store.get(bad)
        with self.assertRaises(SessionError):
            self.store.save("s-a", "t", "not a list")

    def test_a_broken_file_is_skipped_by_the_list_and_titles_are_cleaned(self):
        self.store.save("s-a", "  a   very\nlong  " + "x" * 300, [])
        (Path(self.store.root) / "s-bad.meta.json").write_text("{not json", encoding="utf-8")
        (Path(self.store.root) / "foreign.meta.json").write_text(json.dumps({"id": "../evil"}), encoding="utf-8")
        listed = self.store.list()
        self.assertEqual([m["id"] for m in listed], ["s-a"])
        self.assertLessEqual(len(listed[0]["title"]), 120)
        self.assertNotIn("\n", listed[0]["title"])


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=4096), tok,
                          ChatTemplate(ROOT / "serve/chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        cls.svc.sessions = SessionStore(os.path.join(cls.tmp.name, "sessions"))
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def call(self, path, body=None, headers=None, ctype="application/json"):
        h = {**({"Content-Type": ctype} if body is not None else {}), **(headers or {})}
        r = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_round_trip(self):
        code, meta = self.call("/sessions/s-http1", {"title": "Hello", "messages": [{"role": "user", "text": "hi"}]})
        self.assertEqual((code, meta["messages"]), (200, 1))
        code, listing = self.call("/sessions")
        self.assertIn("s-http1", [s["id"] for s in listing["sessions"]])
        code, got = self.call("/sessions/s-http1")
        self.assertEqual((code, got["title"], got["messages"][0]["text"]), (200, "Hello", "hi"))
        self.assertEqual(self.call("/sessions/s-http1/delete", {})[1], {"ok": True})
        self.assertEqual(self.call("/sessions/s-http1")[0], 404)

    def test_bad_ids_and_bodies_are_refused(self):
        self.assertEqual(self.call("/sessions/..%2Fx", {"title": "t", "messages": []})[0], 400)
        self.assertEqual(self.call("/sessions/s-x", {"title": "t", "messages": "no"})[0], 400)
        self.assertEqual(self.call("/sessions/S-UPPER")[0], 400)

    def test_only_strata_own_page(self):
        evil = {"Origin": "http://evil.example"}
        self.assertEqual(self.call("/sessions", headers=evil)[0], 403)                   # reading
        self.assertEqual(self.call("/sessions/s-e", {"title": "t", "messages": []}, headers=evil)[0], 403)   # writing
        self.assertEqual(self.call("/sessions/s-e", {"title": "t", "messages": []}, ctype="text/plain")[0], 415)

    def test_without_a_sessions_folder_the_routes_say_so(self):
        keep, self.svc.sessions = self.svc.sessions, None
        try:
            self.assertEqual(self.call("/sessions")[0], 404)
            self.assertEqual(self.call("/sessions/s-a", {"title": "t", "messages": []})[0], 404)
        finally:
            self.svc.sessions = keep


if __name__ == "__main__":
    unittest.main()
