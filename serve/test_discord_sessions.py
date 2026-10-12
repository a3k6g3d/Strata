"""The Discord bot shares its conversations with the Strata app: each Discord chat is saved as a session (so it is in the app's chat list),
is continued from what the app has, and !chats / !open switch to another one."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

try:
    import httpx
    from serve import discord_bot as D
except ImportError:
    httpx = None


class _Msg:
    def __init__(self):
        self.edits = []

    async def edit(self, content=None, **kw):
        self.edits.append(content)


class _Channel:
    id = 4242

    def __init__(self):
        self.sent = []
        self.first = _Msg()

    async def send(self, content=None, **kw):
        self.sent.append(content)
        return self.first if len(self.sent) == 1 else _Msg()

    async def typing(self):
        pass


def chunk(**delta):
    return "data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n"


@unittest.skipIf(httpx is None, "httpx not installed")
class Helpers(unittest.TestCase):
    def test_a_saved_chat_becomes_a_request_history(self):
        saved = [{"role": "user", "text": "hi", "images": []}, {"role": "assistant", "text": "hello", "reasoning": "x", "tools": []},
                 {"role": "assistant", "text": "", "tools": []}, {"role": "user", "text": "half", "partial": True}, {"role": "tool", "text": "t"}]
        self.assertEqual(D.api_history(saved), [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
        self.assertEqual(D.api_history(None), [])

    def test_messages_have_the_web_apps_shape(self):
        u, a = D.web_message("user", "hi"), D.web_message("assistant", "yo", "via Discord")
        for m in (u, a):
            self.assertTrue({"role", "text", "images", "files", "time"} <= set(m))
        self.assertEqual((a["reasoning"], a["tools"], a["meta"]), ("", [], "via Discord"))
        self.assertTrue(u["time"].isdigit())

    def test_title_and_id(self):
        self.assertEqual(D.session_title("  what   is\nthis? "), "Discord · what is this?")
        self.assertTrue(D.session_title("x" * 80).endswith("…"))
        sid = D.new_session_id(123456789012345678, 1791800000)
        self.assertEqual(sid, "s-discord-123456789012345678-1791800000")
        self.assertRegex(sid, r"^[a-z0-9][a-z0-9-]{0,63}$")                 # a valid session id for the server's store


class Server:
    """A fake Strata server: /sessions (kept in a dict) and a streamed chat completion."""
    def __init__(self, sessions=None, answer="ok"):
        self.sessions, self.answer, self.requests, self.saved = sessions or {}, answer, [], []

    def __call__(self, request):
        path, method = request.url.path, request.method
        if path == "/v1/chat/completions":
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, content=(chunk(content=self.answer) + "data: [DONE]\n\n").encode())
        if path == "/sessions" and method == "GET":
            rows = sorted(({"id": k, "title": v["title"], "messages": len(v["messages"]), "updated": i} for i, (k, v) in enumerate(self.sessions.items())),
                          key=lambda r: -r["updated"])
            return httpx.Response(200, json={"sessions": rows})
        if path.startswith("/sessions/"):
            sid = path.split("/")[-1]
            if method == "GET":
                return httpx.Response(200, json=self.sessions[sid]) if sid in self.sessions else httpx.Response(404, json={})
            body = json.loads(request.content)
            self.sessions[sid] = {"id": sid, **body}
            self.saved.append((sid, body))
            return httpx.Response(200, json={"id": sid})
        return httpx.Response(404, json={})


def make_bot(server, tmp):
    bot = D.StrataDiscordBot({"allowed_user_ids": [1]}, "http://strata", "", Path(tmp))
    bot.http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return bot


@unittest.skipIf(httpx is None, "httpx not installed")
class Sharing(unittest.TestCase):
    def ask(self, bot, text, cid=4242):
        ch = _Channel()
        msg = type("M", (), {"channel": ch})()
        st = bot.state(cid)

        async def go():
            await bot.answer(msg, st, text, [])
        asyncio.run(go())
        return st, ch

    def test_a_discord_chat_is_saved_as_a_session_the_app_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = Server(answer="Four.")
            bot = make_bot(srv, tmp)
            st, ch = self.ask(bot, "What is 2+2?")
            self.assertEqual(len(srv.saved), 1)
            sid, body = srv.saved[0]
            self.assertTrue(sid.startswith("s-discord-4242-"))
            self.assertEqual(body["title"], "Discord · What is 2+2?")
            self.assertEqual([(m["role"], m["text"]) for m in body["messages"]], [("user", "What is 2+2?"), ("assistant", "Four.")])
            self.assertEqual(st.session, sid)
            self.assertEqual(json.loads((Path(tmp) / "4242.json").read_text(encoding="utf-8"))["session"], sid)    # remembered across restarts

    def test_a_second_message_continues_the_same_session_and_picks_up_what_the_app_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = Server(answer="A")
            bot = make_bot(srv, tmp)
            st, _ = self.ask(bot, "first")
            sid = st.session
            # the user continues the chat in the Strata app: a question and its answer are added to the saved session
            srv.sessions[sid]["messages"] += [D.web_message("user", "from the app"), D.web_message("assistant", "app answer")]
            st, _ = self.ask(bot, "back on discord")
            sent = srv.requests[-1]["messages"]
            self.assertEqual([m["content"] for m in sent], ["first", "A", "from the app", "app answer", "back on discord"])
            self.assertEqual(st.session, sid)                                                      # still one session
            self.assertEqual(len(srv.sessions), 1)
            self.assertEqual(len(srv.sessions[sid]["messages"]), 6)

    def test_the_bot_still_answers_when_the_chat_list_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = Server()
            srv_call = srv.__call__

            def no_sessions(request):
                return httpx.Response(404, json={}) if request.url.path.startswith("/sessions") else srv_call(request)
            bot = make_bot(no_sessions, tmp)
            st, ch = self.ask(bot, "hello")
            self.assertEqual(ch.first.edits[-1], "ok")
            self.assertEqual(st.history[-1], {"role": "assistant", "content": "ok"})
            self.assertIsNone(st.session)                                                          # not saved: the local copy keeps the chat

    def command(self, bot, text, cid=4242):
        ch = _Channel()
        msg = type("M", (), {"channel": ch})()
        name, arg = D.parse_command(text)
        asyncio.run(bot.command(msg, bot.state(cid), name, arg))
        return ch.sent[-1]

    def test_new_open_and_chats(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = {"id": "s-abc", "title": "A chat from the app", "messages": [D.web_message("user", "q1"), D.web_message("assistant", "a1")]}
            srv = Server(sessions={"s-abc": old}, answer="next")
            bot = make_bot(srv, tmp)
            listing = self.command(bot, "!chats")
            self.assertIn("1. A chat from the app (2 messages)", listing)
            self.assertIn("Continuing **A chat from the app**", self.command(bot, "!open 1"))
            st, _ = self.ask(bot, "continue it")
            self.assertEqual(st.session, "s-abc")
            self.assertEqual([m["content"] for m in srv.requests[-1]["messages"]], ["q1", "a1", "continue it"])
            self.assertEqual(len(srv.sessions["s-abc"]["messages"]), 4)
            self.assertIn("New conversation", self.command(bot, "!new"))
            self.assertIsNone(bot.state(4242).session)
            st, _ = self.ask(bot, "fresh start")                                                     # a new session, the old one untouched
            self.assertNotEqual(st.session, "s-abc")
            self.assertEqual(len(srv.sessions["s-abc"]["messages"]), 4)
            self.assertEqual([m["content"] for m in srv.requests[-1]["messages"]], ["fresh start"])
            self.assertIn("Use `!chats` first", self.command(bot, "!open 9"))

    def test_an_old_history_file_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "4242.json").write_text(json.dumps([{"role": "user", "content": "old"}, {"role": "assistant", "content": "chat"}]), encoding="utf-8")
            bot = make_bot(Server(), tmp)
            st = bot.state(4242)
            self.assertEqual((st.history, st.session), ([{"role": "user", "content": "old"}, {"role": "assistant", "content": "chat"}], None))


if __name__ == "__main__":
    unittest.main()
