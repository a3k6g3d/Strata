import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

try:
    import httpx
    from serve import discord_bot as D
except ImportError:                                    # httpx missing: the bot is optional
    httpx = None


@unittest.skipIf(httpx is None, "httpx not installed")
class HelperTests(unittest.TestCase):
    def test_allow_list(self):
        cfg = {"allowed_user_ids": [123, "456"]}
        self.assertTrue(D.is_allowed(123, cfg))
        self.assertTrue(D.is_allowed("456", cfg))
        self.assertFalse(D.is_allowed(999, cfg))
        self.assertFalse(D.is_allowed(123, {}))                 # no list: nobody
        self.assertFalse(D.is_allowed(123, {"allowed_user_ids": []}))

    def test_permission_and_thinking_are_clamped(self):
        for bad in ("edit", "full", "", None, "root"):
            self.assertEqual(D.clamp_permission(bad), "ask")
        for ok in ("off", "read", "ask"):
            self.assertEqual(D.clamp_permission(ok), ok)
        self.assertEqual(D.clamp_thinking("xhigh"), "medium")
        self.assertEqual(D.clamp_thinking("off"), "off")

    def test_commands(self):
        self.assertEqual(D.parse_command("!mode read"), ("mode", "read"))
        self.assertEqual(D.parse_command("  !NEW "), ("new", ""))
        self.assertIsNone(D.parse_command("hello !new"))
        self.assertIsNone(D.parse_command("what is !important"))

    def test_split_message(self):
        self.assertEqual(D.split_message(""), [])
        self.assertEqual(D.split_message("short"), ["short"])
        text = ("line of text\n" * 400)
        parts = D.split_message(text, 500)
        self.assertTrue(all(len(p) <= 520 for p in parts), [len(p) for p in parts])
        self.assertEqual("".join(parts).replace("\n", "").replace(" ", ""), text.replace("\n", "").replace(" ", ""))
        code = "```\n" + "x = 1\n" * 300 + "```"
        for p in D.split_message(code, 400):
            self.assertEqual(p.count("```") % 2, 0, p[:60])    # every piece closes the block it opened

    def test_history_is_trimmed_from_a_user_message(self):
        h = [{"role": "assistant", "content": "a"}] + [{"role": r, "content": "x" * 10} for _ in range(60) for r in ("user", "assistant")]
        out = D.trim_history(h)
        self.assertEqual(out[0]["role"], "user")
        self.assertLessEqual(len(out), D.MAX_HISTORY_MESSAGES)
        big = [{"role": "user", "content": "y" * 50000}, {"role": "assistant", "content": "z" * 50000}, {"role": "user", "content": "q"}]
        self.assertEqual(D.trim_history(big), [{"role": "user", "content": "q"}])

    def test_describe_call_shows_a_command_in_full_and_cuts_long_ones(self):
        self.assertIn("Get-Date", D.describe_call("run_command", {"command": "Get-Date"}))
        self.assertIn("```\nGet-Date\n```", D.describe_call("run_command", json.dumps({"command": "Get-Date"})))
        self.assertLess(len(D.describe_call("x", {"command": "a" * 5000})), 1100)
        self.assertNotIn("```\n```", D.describe_call("x", {"command": "```"}).replace("```\n'''\n```", ""))

    def test_sse_lines(self):
        self.assertIsNone(D.parse_sse_line(": keep-alive"))
        self.assertIsNone(D.parse_sse_line("data: [DONE]"))
        self.assertIsNone(D.parse_sse_line("data: {broken"))
        self.assertEqual(D.parse_sse_line('data: {"a": 1}'), {"a": 1})

    def test_token_from_env_then_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "tok.txt"
            f.write_text("filetoken\nsecond line", encoding="utf-8")
            cfg = {"token_env": "STRATA_TEST_DISCORD_TOKEN", "token_file": "tok.txt"}
            os.environ.pop("STRATA_TEST_DISCORD_TOKEN", None)
            self.assertEqual(D.read_token(cfg, Path(d)), "filetoken")
            os.environ["STRATA_TEST_DISCORD_TOKEN"] = "envtoken"
            try:
                self.assertEqual(D.read_token(cfg, Path(d)), "envtoken")
            finally:
                del os.environ["STRATA_TEST_DISCORD_TOKEN"]
            self.assertIsNone(D.read_token({"token_env": "STRATA_TEST_DISCORD_TOKEN"}, Path(d)))

    def test_request_body_never_asks_for_more_than_the_mode_allows(self):
        b = D.chat_body([], "hi", [], "medium", "ask")
        self.assertEqual((b["strata_mcp"], b["strata_permission"], b["reasoning_effort"]), (True, "ask", "medium"))
        self.assertNotIn("strata_mcp", D.chat_body([], "hi", [], "off", "off"))
        img = D.chat_body([], "look", ["data:image/png;base64,AAAA"], "low", "read")["messages"][-1]["content"]
        self.assertEqual([p["type"] for p in img], ["text", "image_url"])

    def test_start_says_why_it_does_not_start(self):
        self.assertIsNone(D.start({"enabled": False}, "http://x"))
        self.assertIsNone(D.start({"enabled": True, "allowed_user_ids": []}, "http://x"))
        os.environ.pop("STRATA_DISCORD_TOKEN", None)
        self.assertIsNone(D.start({"enabled": True, "allowed_user_ids": [1], "token_env": "STRATA_NO_SUCH_VAR"}, "http://x"))


class _Msg:
    def __init__(self):
        self.edits = []

    async def edit(self, content=None, **kw):
        self.edits.append(content)


class _Channel:
    id = 42

    def __init__(self):
        self.sent = []
        self.note = _Msg()

    async def send(self, content=None, **kw):
        self.sent.append(content)
        return self.note if len(self.sent) == 1 else _Msg()

    async def typing(self):
        pass


@unittest.skipIf(httpx is None, "httpx not installed")
class AnswerTests(unittest.TestCase):
    def run_answer(self, stream_text, status=200, text="hello", history=None):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(status, content=stream_text.encode(), headers={"content-type": "text/event-stream"})

        d = tempfile.TemporaryDirectory()
        bot = D.StrataDiscordBot({"allowed_user_ids": [1], "thinking": "low"}, "http://strata", "secret", Path(d.name))
        bot.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ch = _Channel()
        msg = type("M", (), {"channel": ch})()
        st = bot.state(42)
        st.history = history or []

        async def go():
            await bot.answer(msg, st, text, [])
            await bot.http.aclose()
        asyncio.run(go())
        return bot, st, ch, seen, d

    def chunk(self, **delta):
        return "data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n"

    def test_a_streamed_answer_is_written_and_remembered(self):
        stream = self.chunk(reasoning_content="thinking") + self.chunk(content="Hel") + self.chunk(content="lo there") + "data: [DONE]\n\n"
        bot, st, ch, seen, d = self.run_answer(stream)
        self.assertEqual(ch.note.edits[-1], "Hello there")
        self.assertEqual(seen["auth"], "Bearer secret")
        self.assertEqual((seen["body"]["strata_permission"], seen["body"]["reasoning_effort"]), ("ask", "low"))
        self.assertEqual(st.history, [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "Hello there"}])
        saved = json.loads((Path(d.name) / "42.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, st.history)
        d.cleanup()

    def test_a_long_answer_becomes_several_messages(self):
        stream = self.chunk(content="word " * 800)
        bot, st, ch, seen, d = self.run_answer(stream)
        self.assertGreaterEqual(len(ch.sent), 2)                # the placeholder, then the continuation
        d.cleanup()

    def test_a_server_error_is_shown_and_not_remembered(self):
        bot, st, ch, seen, d = self.run_answer('{"error": {"message": "no model"}}', status=500)
        self.assertIn("no model", ch.note.edits[-1])
        self.assertEqual(st.history, [])
        d.cleanup()

    def test_tool_events_are_tracked_and_an_approval_is_asked(self):
        asked = []

        async def fake_ask(channel, tool, ev):
            asked.append((tool["name"], ev["approval"]))

        ev = lambda **k: "data: " + json.dumps({"strata_mcp": k}) + "\n\n"
        stream = (ev(event="call", id="t1", name="exec__run_command", arguments={"command": "dir"}) +
                  ev(event="approval", id="t1", approval="tok1", exec=True) +
                  ev(event="result", id="t1", ok=True) + self.chunk(content="done"))
        seen = {}

        def handler(request):
            return httpx.Response(200, content=stream.encode())

        bot = D.StrataDiscordBot({"allowed_user_ids": [1]}, "http://strata")
        bot.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        bot.ask_approval = fake_ask
        ch = _Channel()
        st = bot.state(7)

        async def go():
            await bot.answer(type("M", (), {"channel": ch})(), st, "run it", [])
            await asyncio.sleep(0)
            await bot.http.aclose()
        asyncio.run(go())
        self.assertEqual(asked, [("exec__run_command", "tok1")])
        self.assertEqual(ch.note.edits[-1], "done")


if __name__ == "__main__":
    unittest.main()
