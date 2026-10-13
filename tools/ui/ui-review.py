"""Review server for the UI work: serves this checkout's web files and proxies every API call
to the live server on 8080, so the interface is looked at against the real engine without
reloading any model.

It answers on 8081, which Strata's own Browser pane now accepts (serve/panel.py allows
127.0.0.1 on any port, and refuses every other local address on purpose -- a page of the local
network must not be probed through it).  It can also be opened in your own browser, which is what
the Selenium harnesses do.

    python -I ui-review.py            # stop with Ctrl+C
"""
import http.client
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# this checkout's web files: the script lives in tools/ui, so the repository root is three levels up.  (It was
# two when this file sat in a loose folder beside the checkout; get it wrong and every web file falls through
# to the live server below, so the review quietly shows the live app instead of the branch under review.)
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "serve", "web")
LIVE_HOST, LIVE_PORT, UPSTREAM = "127.0.0.1", 8080, "http://127.0.0.1:8080"
REVIEW_PORT = 8081        # 8080 is the live server itself; 8081 is the next free one, and the pane allows it
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".woff2": "font/woff2", ".json": "application/json", ".png": "image/png",
        ".txt": "text/plain; charset=utf-8"}
# everything the engine owns is proxied; everything the branch owns is served from disk.  The rule is by
# verb+prefix, not by a file list: the page asks for /app.js, /app.css, /js/*.js, /tokens.css, /sprite.svg...
# and a list would silently ship the live app's copies of anything left out -- which looks identical enough
# to pass a review, and quietly reviews the wrong code.
PROXY_PREFIX = ("/api/", "/health", "/mcp", "/panel")
_model = None


def model_name():
    """The live server's model id, fetched once, lazily (never at import time)."""
    global _model
    if _model is None:
        try:
            with urllib.request.urlopen(UPSTREAM + "/api/v1/models", timeout=10) as r:
                _model = json.load(r)["models"][0]["id"]
        except Exception:
            return "strata"      # live server busy or down: the proxy will fail anyway
    return _model


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def local_path(self):
        p = self.path.split("?")[0]
        if p.startswith(PROXY_PREFIX):
            return None
        if p in ("/", ""):
            p = "/index.html"
        f = os.path.normpath(os.path.join(WEB, p.lstrip("/").replace("web/", "")))
        return f if f.startswith(WEB) and os.path.isfile(f) else None

    def do_GET(self): self.route(False)
    def do_POST(self): self.route(True)
    # the app itself only speaks GET and POST (a chat is saved with POST sessions/<id>, deleted
    # with POST sessions/<id>/delete); these verbs are here so the proxy never answers 501 for a
    # request the live server would have accepted
    def do_PUT(self): self.route(True)
    def do_PATCH(self): self.route(True)
    def do_DELETE(self): self.route(True)
    def do_OPTIONS(self): self.route(True)

    def route(self, is_post):   # NOT handle(): BaseHTTPRequestHandler calls handle() itself, with no argument
        f = self.local_path()
        if f:
            return self.serve_static(f)
        if self.path == "/api/models":   # the UI's own model list
            try:
                data = json.dumps({"models": [model_name()]}).encode()
            except Exception as e:
                return self.send_error(502, str(e))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            return self.wfile.write(data)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if is_post else b""
        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "origin", "connection", "accept-encoding", "content-length")}
        # the live server counts a request as coming from its own page when the Origin matches the Host it was
        # served on (serve/server.py: _foreign_origin).  Here the Host becomes 127.0.0.1:8080 while the Origin
        # would still say 8081, so every "own page" route -- saving a chat, answering a tool approval, the
        # config -- would be refused with 403.  The page really is this app, on this PC, so the Origin is
        # rewritten to the address the app is really served on; an Origin that is not the review server's own
        # is left alone, so a page of another site arriving here is still refused as it would be direct.
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin.split("://", 1)[-1].rstrip("/") in (f"127.0.0.1:{REVIEW_PORT}", f"localhost:{REVIEW_PORT}"):
            hdrs["Origin"] = f"http://{LIVE_HOST}:{LIVE_PORT}"
        c = http.client.HTTPConnection(LIVE_HOST, LIVE_PORT, timeout=600)
        try:
            c.request(self.command, self.path, body, hdrs)
            r = c.getresponse()
            self.send_response(r.status, r.reason)
            for k, v in r.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:  # stream: the answer arrives token by token
                chunk = r.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as e:
            print("proxy error:", e)
        finally:
            c.close()

    def serve_static(self, f):
        try:
            with open(f, "rb") as fh:
                data = fh.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(f)[1].lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # always the newest edit
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    # only this PC: the Browser pane will not frame a local address at all (panel.check refuses
    # loopback and the private ranges, on purpose -- a page of the local network must not be probed
    # through it), so there is no reason to answer the local network here and every reason not to.
    print(f"review: http://127.0.0.1:{REVIEW_PORT}/   (open in your own browser; Strata's Browser pane"
          f" refuses local addresses)\n        files from {WEB}, API -> {LIVE_HOST}:{LIVE_PORT}", flush=True)
    if not os.path.isdir(WEB):
        print(f"  !! no such folder: {WEB}\n         every web file will come from the live server instead of this "
              f"checkout, so what you see is not what you edited", flush=True)
    ThreadingHTTPServer(("127.0.0.1", REVIEW_PORT), Handler).serve_forever()
