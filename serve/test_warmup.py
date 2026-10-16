import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from serve import warmup


class Stub(BaseHTTPRequestHandler):
    loaded_after = 2            # /v1/status answers "not loaded" this many times first
    seen: list = []
    status_calls = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        Stub.status_calls += 1
        body = json.dumps({"loaded": Stub.status_calls > Stub.loaded_after}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        Stub.seen.append((json.loads(self.rfile.read(n)), self.headers.get("Authorization")))
        body = b'{"choices": []}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Warmup(unittest.TestCase):
    def test_it_is_off_unless_enabled(self):
        for cfg in (None, {}, {"enabled": False}, {"enabled": "yes"}, "on", [1]):
            self.assertIsNone(warmup.settings(cfg))
            self.assertIsNone(warmup.start(cfg, "http://127.0.0.1:1"))

    def test_settings_fall_back_to_the_built_in_prompts_and_clamp_the_length(self):
        s = warmup.settings({"enabled": True})
        self.assertEqual((s["prompts"], s["max_tokens"]), (warmup.DEFAULT_PROMPTS, 100))
        self.assertEqual(warmup.settings({"enabled": True, "prompts": ["a", " "], "max_tokens": 5000})["max_tokens"], 100)
        self.assertEqual(warmup.settings({"enabled": True, "prompts": ["a", "b"], "max_tokens": 64})["prompts"], ["a", "b"])
        self.assertEqual(len(warmup.settings({"enabled": True, "prompts": ["x"] * 40})["prompts"]), 12)

    def test_it_waits_for_the_load_then_sends_each_prompt_once(self):
        Stub.seen, Stub.status_calls, Stub.loaded_after = [], 0, 2
        httpd = HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            cfg = warmup.settings({"enabled": True, "prompts": ["one", "two", "three"], "max_tokens": 50})
            done = warmup.run(cfg, f"http://127.0.0.1:{httpd.server_port}", "key", wait_s=30, poll_s=0.05)
        finally:
            httpd.shutdown()
        self.assertEqual(done, 3)
        self.assertGreater(Stub.status_calls, 2)                                   # it polled until the model was loaded
        self.assertEqual([b["messages"][0]["content"] for b, _ in Stub.seen], ["one", "two", "three"])
        self.assertTrue(all(b["max_tokens"] == 50 and b["strata_checkpoint"] is False for b, _ in Stub.seen))
        self.assertTrue(all(auth == "Bearer key" for _, auth in Stub.seen))

    def test_it_gives_up_when_the_model_never_loads(self):
        self.assertEqual(warmup.run(warmup.settings({"enabled": True}), "http://127.0.0.1:1", wait_s=0.3, poll_s=0.05), 0)


if __name__ == "__main__":
    unittest.main()
