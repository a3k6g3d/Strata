"""Files sent to Discord: only from the workspace folder, only after a click when the model asks, and never past the no-read list."""
import asyncio
import os
import tempfile
import types
import unittest
from pathlib import Path

try:
    import httpx
    from serve import discord_bot as D
except ImportError:
    httpx = None


class Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw.get("file")))


def fake_discord():
    return types.SimpleNamespace(File=lambda path: ("FILE", Path(path).name))


@unittest.skipIf(httpx is None, "httpx not installed")
class Sendable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        (self.root / "sub").mkdir(parents=True)
        (self.root / "chart.png").write_bytes(b"png")
        (self.root / "sub" / "log.txt").write_text("hi", encoding="utf-8")
        (Path(self.tmp.name) / "outside.txt").write_text("secret", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_file_inside_the_workspace_may_be_sent(self):
        real, why = D.resolve_sendable("chart.png", self.root)
        self.assertEqual((real.name, why), ("chart.png", None))
        self.assertEqual(D.resolve_sendable("sub/log.txt", self.root)[0].name, "log.txt")
        self.assertEqual(D.resolve_sendable("`sub\\log.txt`", self.root)[0].name, "log.txt")     # a quoted or backslashed name

    def test_nothing_outside_the_workspace_may_be_sent(self):
        for bad in ("../outside.txt", str(Path(self.tmp.name) / "outside.txt"), "sub/../../outside.txt", "C:\\Windows\\win.ini", "", "."):
            real, why = D.resolve_sendable(bad, self.root)
            self.assertIsNone(real, bad)
            self.assertTrue(why, bad)
        self.assertEqual(D.resolve_sendable("missing.png", self.root)[1], "no such file in the workspace folder")
        self.assertIsNotNone(D.resolve_sendable("chart.png", None)[1])                              # no workspace folder: nothing is sendable

    def test_a_link_out_of_the_workspace_does_not_escape(self):
        link = self.root / "escape.txt"
        try:
            os.symlink(Path(self.tmp.name) / "outside.txt", link)
        except (OSError, NotImplementedError):
            self.skipTest("this account cannot create symbolic links")
        self.assertIsNone(D.resolve_sendable("escape.txt", self.root)[0])

    def test_big_and_secret_files_are_refused(self):
        (self.root / "big.bin").write_bytes(b"0" * (D.MAX_SEND_BYTES + 1))
        self.assertIn("MB", D.resolve_sendable("big.bin", self.root)[1])
        (self.root / ".env").write_text("TOKEN=x", encoding="utf-8")
        self.assertIn("no-read list", D.resolve_sendable(".env", self.root)[1])
        (self.root / "id_rsa").write_text("key", encoding="utf-8")
        self.assertIn("no-read list", D.resolve_sendable("id_rsa", self.root)[1])


@unittest.skipIf(httpx is None, "httpx not installed")
class Sending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        (self.root / "chart.png").write_bytes(b"png")
        self.bot = D.StrataDiscordBot({"allowed_user_ids": [1]}, "http://strata", "", None, None, send_root=self.root)
        self.bot.discord = fake_discord()
        self.asked = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def command(self, name, arg=""):
        ch = Channel()
        msg = types.SimpleNamespace(channel=ch)
        self.run_async(self.bot.command(msg, self.bot.state(5), name, arg))
        return ch.sent

    def test_the_send_command_uploads_only_a_workspace_file(self):
        sent = self.command("send", "chart.png")
        self.assertEqual(sent, [("`chart.png`", ("FILE", "chart.png"))])
        sent = self.command("send", "../x.txt")
        self.assertIn("Cannot send that", sent[0][0])
        self.assertIsNone(sent[0][1])

    def test_the_files_command_lists_what_can_be_sent(self):
        self.assertIn("chart.png", self.command("files")[0][0])
        (self.root / "chart.png").unlink()
        self.assertIn("no files", self.command("files")[0][0])

    def test_a_marker_in_an_answer_asks_first_and_sends_only_on_yes(self):
        answers = [True, False]

        async def confirm(channel, text):
            self.asked.append(text)
            return answers.pop(0)
        self.bot.confirm = confirm
        (self.root / "two.txt").write_text("2", encoding="utf-8")
        ch = Channel()
        cleaned = self.run_async(self.bot.send_marked_files(ch, "Here you go\n[[send: chart.png]]\n[[send: two.txt]]\n[[send: ../outside.txt]]"))
        self.assertEqual(cleaned, "Here you go")
        self.assertEqual(len(self.asked), 2)                                                        # the outside path was never offered
        uploads = [c for c in ch.sent if c[1]]
        self.assertEqual(uploads, [("`chart.png`", ("FILE", "chart.png"))])                          # one yes, one no
        self.assertTrue(any("workspace folder" in (c[0] or "") and "outside.txt" in c[0] for c in ch.sent))

    def test_no_more_than_three_files_are_offered(self):
        async def confirm(channel, text):
            return False
        self.bot.confirm = confirm
        ch = Channel()
        self.run_async(self.bot.send_marked_files(ch, "".join(f"[[send: f{i}.txt]]" for i in range(5))))
        self.assertTrue(any("first 3" in (c[0] or "") for c in ch.sent))

    def test_the_request_tells_the_model_how_to_offer_a_file(self):
        body = D.chat_body([], "hi", [], "low", "ask", system=D.SEND_HINT)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("[[send:", body["messages"][0]["content"])
        self.assertEqual(D.chat_body([], "hi", [], "low", "ask")["messages"][0]["role"], "user")    # no hint without a workspace folder


if __name__ == "__main__":
    unittest.main()
