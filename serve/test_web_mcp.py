"""serve/test_web_mcp.py - tools/strata_web_mcp.py, the internet tool: what it refuses, how it reads a page, and its MCP
protocol.  No internet is used: pages come from a throwaway local server (allowed only through the tests' own
`allow_private=True`, which the real server never sets).

    python -m unittest serve.test_web_mcp -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import strata_web_mcp as w  # noqa: E402

PAGE = (b"<html><head><title> A  Test Page </title><style>p{color:red}</style></head><body><h1>Hello</h1>"
        b"<p>See <a href='https://example.org/x'>the docs</a> now.</p><script>secret()</script><ul><li>one</li><li>two</li></ul>"
        b"</body></html>")


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path
        if path == "/page":
            self.reply(200, "text/html; charset=utf-8", PAGE)
        elif path == "/plain":
            self.reply(200, "text/plain", b"just text")
        elif path == "/json":
            self.reply(200, "application/json", b'{"a": 1}')
        elif path == "/bin":
            self.reply(200, "application/octet-stream", b"\x00\x01")
        elif path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/big":
            self.reply(200, "text/plain", b"x" * 3_000_000)
        elif path == "/long":
            self.reply(200, "text/plain", b"0123456789" * 100)
        else:
            self.reply(404, "text/plain", b"nope")

    def reply(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Refusals(unittest.TestCase):
    def refused(self, url):
        with self.assertRaises(w.WebError) as cm:
            w.validate(url)
        return str(cm.exception)

    def test_private_and_local_addresses(self):
        for url in ("http://127.0.0.1:8080/", "http://localhost/", "http://[::1]/", "http://10.0.0.5/", "http://192.168.1.1/",
                    "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/", "http://0.0.0.0/", "http://100.64.0.1/",
                    "http://[::ffff:127.0.0.1]/", "http://[fe80::1]/", "http://[fd00::1]/", "http://224.0.0.1/"):
            self.assertIn("blocked", self.refused(url), url)

    def test_odd_forms(self):
        self.assertIn("only http", self.refused("file:///C:/Windows/win.ini"))
        self.assertIn("only http", self.refused("ftp://example.com/x"))
        self.assertIn("user name", self.refused("https://user:pw@example.com/"))
        self.assertIn("port 22", self.refused("https://example.com:22/"))
        self.assertIn("longer than", self.refused("https://example.com/?" + "a" * 2000))
        self.assertIn("one address", self.refused("https://a.example\nhttps://b.example"))
        self.assertIn("one address", self.refused(""))

    def test_a_public_address_is_accepted(self):
        p, port, ip = w.validate("https://93.184.216.34/page")
        self.assertEqual((p.hostname, port, ip), ("93.184.216.34", 443, "93.184.216.34"))

    def test_is_public(self):
        for ip in ("93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"):
            self.assertTrue(w.is_public(ip), ip)
        for ip in ("127.0.0.1", "10.1.1.1", "192.168.0.9", "169.254.1.1", "::1", "::ffff:10.0.0.1", "100.64.0.1", "0.0.0.0"):
            self.assertFalse(w.is_public(ip), ip)

    def test_search_limits(self):
        with self.assertRaises(w.WebError):
            w.web_search("x" * 400)
        with self.assertRaises(w.WebError):
            w.web_search("   ")


class Reading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path, **kw):
        return w.fetch_url(self.base + path, allow_private=True, **kw)

    def test_html_becomes_text_with_links(self):
        out = self.get("/page")
        self.assertIn("HTTP 200 - A Test Page", out)
        self.assertIn("Hello", out)
        self.assertIn("the docs (https://example.org/x)", out)
        self.assertIn("one", out)
        self.assertNotIn("secret()", out)                                           # scripts are dropped
        self.assertNotIn("color:red", out)                                          # so are styles

    def test_plain_and_json(self):
        self.assertTrue(self.get("/plain").endswith("just text"))
        self.assertIn('{"a": 1}', self.get("/json"))

    def test_a_redirect_is_followed_and_a_loop_is_stopped(self):
        self.assertIn("Hello", self.get("/redir"))
        with self.assertRaises(w.WebError) as cm:
            self.get("/loop")
        self.assertIn("redirects", str(cm.exception))

    def test_binary_is_refused_and_big_pages_are_cut(self):
        with self.assertRaises(w.WebError) as cm:
            self.get("/bin")
        self.assertIn("not a text page", str(cm.exception))
        out = self.get("/big", max_chars=50000)
        self.assertIn("cut at 2 MB", out)

    def test_long_pages_come_in_parts(self):
        first = self.get("/long", max_chars=500)
        self.assertIn("Characters 0-500 of 1000", first)
        self.assertIn("start=500", first)
        rest = self.get("/long", start=500, max_chars=500)
        self.assertIn("Characters 500-1000 of 1000", rest)
        self.assertNotIn("start=", rest)

    def test_a_redirect_into_a_private_address_is_refused(self):
        # every hop goes through validate(): the public site that redirects to the metadata address gets nowhere
        with self.assertRaises(w.WebError):
            w.validate("http://169.254.169.254/latest/meta-data/")
        p, port, ip = w.validate("https://93.184.216.34/ok")
        self.assertTrue(w.is_public(ip))


class Parsing(unittest.TestCase):
    def test_bing_results_are_unwrapped(self):
        page = ('<ol><li class="b_algo" data-id><h2 class=""><a target="_blank" href="https://www.bing.com/ck/a?!&amp;&amp;p=x&amp;'
                'u=a1aHR0cHM6Ly93d3cucHl0aG9uLm9yZy8&amp;ntb=1">Welcome to <strong>Python</strong>.org</a></h2>'
                '<div class="b_caption"><p class="b_lineclamp2">Calculations are simple</p></div></li>'
                '<li class="b_algo"><h2><a href="https://direct.example/page">Direct</a></h2></li></ol>')
        r = w.parse_bing(page, 5)
        self.assertEqual(r[0], {"title": "Welcome to Python.org", "url": "https://www.python.org/", "snippet": "Calculations are simple"})
        self.assertEqual(r[1]["url"], "https://direct.example/page")
        self.assertEqual(len(w.parse_bing(page, 1)), 1)

    def test_duckduckgo_results(self):
        page = ('<div class="result results_links"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa&amp;rut=z">'
                'Example <b>A</b></a><a class="result__snippet" href="x">About <b>a</b></a></div>')
        r = w.parse_results(page, 5)
        self.assertEqual((r[0]["title"], r[0]["url"], r[0]["snippet"]), ("Example A", "https://example.org/a", "About a"))


class ProxyRules(unittest.TestCase):
    """The filtering proxy every browser byte goes through: public addresses only, the allowed ports, GET/HEAD/CONNECT."""

    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.site.serve_forever, daemon=True).start()
        cls.site_port = cls.site.server_address[1]
        cls.echo = socketserver_echo()
        cls.echo_port = cls.echo.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for s in (cls.site, cls.echo):
            s.shutdown()
            s.server_close()

    def raw(self, proxy, payload: bytes) -> bytes:
        import socket
        c = socket.create_connection(("127.0.0.1", proxy.server_address[1]), timeout=10)
        c.sendall(payload)
        out = b""
        c.settimeout(5)
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    break
                out += d
        except OSError:
            pass
        c.close()
        return out

    def with_proxy(self, allow_private):
        p = w._Proxy(allow_private)
        threading.Thread(target=p.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (p.shutdown(), p.server_close()))
        return p

    def test_private_destinations_are_refused(self):
        p = self.with_proxy(False)
        for payload in (f"CONNECT 127.0.0.1:{self.site_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode(),
                        f"GET http://127.0.0.1:{self.site_port}/page HTTP/1.1\r\nHost: x\r\n\r\n".encode(),
                        b"CONNECT 169.254.169.254:80 HTTP/1.1\r\nHost: x\r\n\r\n",
                        b"CONNECT localhost:443 HTTP/1.1\r\nHost: x\r\n\r\n",
                        b"CONNECT 192.168.1.1:443 HTTP/1.1\r\nHost: x\r\n\r\n",
                        b"CONNECT example.com:22 HTTP/1.1\r\nHost: x\r\n\r\n",
                        b"POST http://example.com/ HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"):
            out = self.raw(p, payload)
            self.assertTrue(out.startswith(b"HTTP/1.1 403"), (payload[:40], out[:60]))
            self.assertNotIn(b"Hello", out)

    def test_a_plain_http_request_is_forwarded(self):
        p = self.with_proxy(True)                                  # the tests' own site is on loopback
        out = self.raw(p, f"GET http://127.0.0.1:{self.site_port}/page HTTP/1.1\r\nHost: x\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
        self.assertTrue(out.startswith(b"HTTP/1.0 200") or out.startswith(b"HTTP/1.1 200"), out[:60])
        self.assertIn(b"Hello", out)

    def test_connect_tunnels_bytes(self):
        import socket
        p = self.with_proxy(True)
        c = socket.create_connection(("127.0.0.1", p.server_address[1]), timeout=10)
        c.sendall(f"CONNECT 127.0.0.1:{self.echo_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.assertTrue(c.recv(200).startswith(b"HTTP/1.1 200"))
        c.sendall(b"ping through the tunnel")
        self.assertEqual(c.recv(200), b"ping through the tunnel")
        c.close()


def socketserver_echo():
    import socketserver

    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            while True:
                d = self.request.recv(4096)
                if not d:
                    return
                self.request.sendall(d)
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@unittest.skipUnless(w.find_browser(), "no Edge or Chrome installed")
class Browser(unittest.TestCase):
    """browse_url with a real headless browser, on a local page (allow_private is the tests' own switch)."""

    @classmethod
    def setUpClass(cls):
        class Js(Site):
            def do_GET(self):
                if self.path == "/js":
                    self.reply(200, "text/html", b"<html><head><title>JS page</title></head><body><p>before</p>"
                               b"<script>document.body.innerHTML = '<h1>rendered by javascript</h1>';</script></body></html>")
                else:
                    super().do_GET()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Js)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_javascript_is_run(self):
        out = w.browse_url(self.base + "/js", wait_ms=3000, allow_private=True)
        self.assertIn("Rendered in a browser - JS page", out)
        self.assertIn("rendered by javascript", out)
        self.assertNotIn("before", out)                                             # the script replaced the body
        self.assertIn("Hello", w.browse_url(self.base + "/page", wait_ms=2000, allow_private=True))

    def test_refusals_come_before_any_browser_starts(self):
        for url in ("http://127.0.0.1:8080/", "http://10.0.0.1/", "file:///C:/Windows/win.ini", "https://example.com:22/"):
            with self.assertRaises(w.WebError):
                w.browse_url(url)

    def test_no_leftover_profiles(self):
        import glob, tempfile
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "strata-browse-*")))
        w.browse_url(self.base + "/page", wait_ms=1000, allow_private=True)
        self.assertEqual(set(glob.glob(os.path.join(tempfile.gettempdir(), "strata-browse-*"))), before)


class Protocol(unittest.TestCase):
    def test_mcp_session(self):
        p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "strata_web_mcp.py")], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            def ask(i, method, params=None):
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
                p.stdin.flush()
                return json.loads(p.stdout.readline())
            self.assertEqual(ask(1, "initialize", {"protocolVersion": "2025-06-18"})["result"]["serverInfo"]["name"], "strata-web")
            tools = ask(2, "tools/list")["result"]["tools"]
            self.assertEqual([t["name"] for t in tools], ["web_search", "fetch_url", "browse_url"])
            self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
            r = ask(3, "tools/call", {"name": "fetch_url", "arguments": {"url": "http://127.0.0.1/"}})["result"]
            self.assertTrue(r["isError"])
            self.assertIn("blocked", r["content"][0]["text"])
            r = ask(4, "tools/call", {"name": "nope", "arguments": {}})["result"]
            self.assertTrue(r["isError"])
        finally:
            p.stdin.close()
            p.wait(timeout=10)
            p.stdout.close()


if __name__ == "__main__":
    unittest.main()
