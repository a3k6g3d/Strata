#!/usr/bin/env python3
"""tools/strata_web_mcp.py - a small, careful internet tool for Strata's chat (an MCP server over stdio).

    web_search(query, max_results)       Bing's results (DuckDuckGo's as a fallback): title, address, snippet
    fetch_url(url, start, max_chars)     one page as readable text (HTML is reduced to text and links)

Listed in the run config's "mcp_servers" like any MCP server (see docs/DETAILS.md, "Tools from MCP servers"); name it
in `"mcp": {"network_servers": ["web"]}` and Strata treats its tools as *network* tools: after the model has read
anything on this PC in a chat, a request to the internet waits for the user's click (a page could otherwise talk the
model into putting a file's content into an address).

What this server will and will not do - standard library only, no accounts, no API keys:
  - GET only, http and https, the ports 80, 443, 8080 and 8443; no cookies, no login, no request bodies;
  - public addresses only: a name or redirect that leads to this PC, a private network, a link-local or a reserved
    address (the router's page, 127.0.0.1, a cloud metadata address, Strata itself) is refused. The connection goes to
    the address that was checked, so a name that changes its answer between the check and the connection gains nothing;
  - at most 5 redirects, each one checked again; 20 seconds; 2 MB; text pages only (HTML, plain text, JSON, XML);
  - an address longer than 1,500 characters or a search longer than 300 is refused: a request is not a way to carry a
    document out.
"""
from __future__ import annotations

import base64
import html
import html.parser
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import threading
import urllib.parse

MAX_BYTES = 2_000_000
TIMEOUT = 20.0
MAX_REDIRECTS = 5
MAX_URL = 1500
MAX_QUERY = 300
PORTS = (80, 443, 8080, 8443)
UA = "Mozilla/5.0 (compatible; Strata-web/1.0)"
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml", "application/rss+xml",
              "application/atom+xml", "application/ld+json")


class WebError(Exception):
    """A refusal or failure whose message the model reads."""


def is_public(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve(host: str, port: int, allow_private: bool = False) -> list[str]:
    """The addresses a name leads to; refuses when any of them is not a public one."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise WebError(f"could not find {host!r}: {e}") from None
    ips = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise WebError(f"could not find {host!r}")
    if not allow_private and not all(is_public(ip) for ip in ips):
        raise WebError(f"blocked: {host!r} leads to this PC or a private network, not the public internet")
    return ips


def validate(url: str, allow_private: bool = False):
    """-> (parsed url, the public address to connect to); raises WebError for anything this server will not fetch."""
    url = (url or "").strip()
    if not url or "\n" in url or "\r" in url:
        raise WebError("give one address, e.g. https://example.com/page")
    if len(url) > MAX_URL:
        raise WebError(f"the address is longer than {MAX_URL} characters; it is refused (an address is not a way to send a document)")
    if "://" not in url:
        url = "https://" + url
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise WebError(f"only http and https addresses are fetched, not {p.scheme!r}")
    if p.username or p.password:
        raise WebError("addresses with a user name or password are refused")
    host = p.hostname
    if not host:
        raise WebError("the address has no host name")
    port = p.port or (443 if p.scheme == "https" else 80)
    if not allow_private and port not in PORTS:
        raise WebError(f"port {port} is not allowed (80, 443, 8080 and 8443 are)")
    return p, port, resolve(host, port, allow_private)[0]


class _Http(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _Https(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)   # the certificate is for the name


def fetch(url: str, allow_private: bool = False):
    """GET `url` -> (final url, status, content type, body bytes, truncated).  Redirects are followed and every hop is
    checked again; the body is cut at MAX_BYTES."""
    for hop in range(MAX_REDIRECTS + 1):
        p, port, ip = validate(url, allow_private)
        conn = (_Https if p.scheme == "https" else _Http)(p.hostname, port, ip, TIMEOUT)
        target = (p.path or "/") + (("?" + p.query) if p.query else "")
        try:
            conn.request("GET", target, headers={"Host": p.netloc.rsplit("@", 1)[-1], "User-Agent": UA,
                                                 "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.4",
                                                 "Accept-Encoding": "identity", "Connection": "close"})
            r = conn.getresponse()
            if r.status in (301, 302, 303, 307, 308) and r.getheader("Location"):
                url = urllib.parse.urljoin(url, r.getheader("Location"))
                r.read(1024)
                continue
            ctype = (r.getheader("Content-Type") or "").split(";")[0].strip().lower()
            body = r.read(MAX_BYTES + 1)
            return url, r.status, ctype, r.getheader("Content-Type") or "", body[:MAX_BYTES], len(body) > MAX_BYTES
        except (OSError, http.client.HTTPException, ssl.SSLError) as e:
            raise WebError(f"the request failed: {e}") from None
        finally:
            conn.close()
    raise WebError(f"more than {MAX_REDIRECTS} redirects")


class _Text(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header", "footer",
             "table", "ul", "ol", "pre", "blockquote", "form", "nav", "main", "aside", "dt", "dd"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._href = None
        self._link: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP and tag != "head":
            self._skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a":
            h = dict(attrs).get("href") or ""
            self._href = h if h.startswith(("http://", "https://")) else None
            self._link = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and tag != "head" and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a" and self._href:
            label = " ".join("".join(self._link).split())
            if label:
                self.out.append(f" ({self._href})")
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self.out.append(data)
        if self._href is not None:
            self._link.append(data)


def html_to_text(page: str) -> tuple[str, str]:
    """-> (title, readable text with the links written as `text (address)`)."""
    p = _Text()
    try:
        p.feed(page)
        p.close()
    except Exception:  # noqa: BLE001 - a broken page still yields what was read
        pass
    text = html.unescape("".join(p.out))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return " ".join(p.title.split()), text


def decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w.-]+)", content_type, re.I) or re.search(rb"<meta[^>]+charset=[\"']?([\w.-]+)", body[:4096], re.I)
    enc = (m.group(1).decode() if m and isinstance(m.group(1), bytes) else m.group(1)) if m else "utf-8"
    try:
        return body.decode(enc, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def fetch_url(url: str, start: int = 0, max_chars: int = 12000, allow_private: bool = False) -> str:
    final, status, ctype, raw_ctype, body, cut = fetch(url, allow_private)
    if not any(ctype.startswith(t) for t in TEXT_TYPES):
        raise WebError(f"that address answered with {ctype or 'an unknown type'}, which is not a text page (HTTP {status})")
    text = decode(body, raw_ctype)
    title = ""
    if ctype in ("text/html", "application/xhtml+xml") or "<html" in text[:2000].lower():
        title, text = html_to_text(text)
    start = max(0, int(start or 0))
    max_chars = max(500, min(int(max_chars or 12000), 50000))
    chunk = text[start:start + max_chars]
    head = [f"URL: {final}", f"HTTP {status}" + (f" - {title}" if title else "")]
    end = start + len(chunk)
    head.append(f"Characters {start}-{end} of {len(text)}" + (" (the page was cut at 2 MB)" if cut else "")
                + (f"; ask again with start={end} for the rest" if end < len(text) else ""))
    return "\n".join(head) + "\n\n" + chunk


def parse_results(page: str, limit: int) -> list[dict]:
    """DuckDuckGo's HTML result page -> [{title, url, snippet}]."""
    out = []
    for block in re.split(r'<div[^>]+class="[^"]*\bresult\b[^"]*"', page)[1:]:
        a = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        href = html.unescape(a.group(1))
        if href.startswith("//"):
            href = "https:" + href
        q = urllib.parse.urlsplit(href)
        if "uddg" in urllib.parse.parse_qs(q.query):
            href = urllib.parse.parse_qs(q.query)["uddg"][0]
        if not href.startswith(("http://", "https://")):
            continue
        s = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', block, re.S)
        clean = lambda t: " ".join(html.unescape(re.sub(r"<[^>]+>", "", t)).split())  # noqa: E731
        out.append({"title": clean(a.group(2)), "url": href, "snippet": clean(s.group(1)) if s else ""})
        if len(out) >= limit:
            break
    return out


def _unwrap_bing(href: str) -> str:
    """Bing's result links go through bing.com/ck/a?...&u=a1<base64 of the real address>."""
    q = urllib.parse.urlsplit(href)
    if q.netloc.endswith("bing.com") and q.path.startswith("/ck/"):
        u = urllib.parse.parse_qs(q.query).get("u", [""])[0]
        if u.startswith("a1"):
            try:
                return base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode("utf-8", "replace")
            except (ValueError, TypeError):
                return ""
    return href


def parse_bing(page: str, limit: int) -> list[dict]:
    """Bing's result page -> [{title, url, snippet}]."""
    clean = lambda t: " ".join(html.unescape(re.sub(r"<[^>]+>", "", t)).split())  # noqa: E731
    out = []
    for block in re.split(r'<li class="b_algo"', page)[1:]:
        block = block.split("</li>")[0]
        a = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        url = _unwrap_bing(html.unescape(a.group(1)))
        if not url.startswith(("http://", "https://")):
            continue
        s = re.search(r'<div class="b_caption"[^>]*>.*?<p[^>]*>(.*?)</p>', block, re.S)
        out.append({"title": clean(a.group(2)), "url": url, "snippet": clean(s.group(1)) if s else ""})
        if len(out) >= limit:
            break
    return out


def web_search(query: str, max_results: int = 8, allow_private: bool = False) -> str:
    query = " ".join((query or "").split())
    if not query:
        raise WebError("give a search query")
    if len(query) > MAX_QUERY:
        raise WebError(f"the query is longer than {MAX_QUERY} characters; it is refused")
    limit = max(1, min(int(max_results or 8), 15))
    providers = (("Bing", "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "en"}), parse_bing),
                 ("DuckDuckGo", "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}), parse_results))
    problems, results = [], []
    for name, url, parser in providers:
        try:
            final, status, ctype, raw, body, cut = fetch(url, allow_private)
        except WebError as e:
            problems.append(f"{name}: {e}")
            continue
        page = decode(body, raw)
        if status != 200:
            problems.append(f"{name}: HTTP {status}")
            continue
        results = parser(page, limit)
        if results:
            break
        problems.append(f"{name}: no results parsed")
    if not results:
        if all("no results parsed" in p for p in problems):
            return f"No results for {query!r}."
        raise WebError("the search providers refused the request (" + "; ".join(problems) + "); fetch_url on a specific page still works")
    return "\n\n".join(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results, 1))


TOOLS = [
    {"name": "web_search", "description": "Search the web (DuckDuckGo). Returns titles, addresses and snippets; use fetch_url to read a result.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "description": "1-15, default 8"}},
                     "required": ["query"]},
     "annotations": {"readOnlyHint": True, "openWorldHint": True}},
    {"name": "fetch_url", "description": "Fetch one web page (http/https, public sites only) and return it as readable text with the links. "
                                          "Long pages come in parts: pass start to continue.",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}, "start": {"type": "integer"}, "max_chars": {"type": "integer", "description": "500-50000, default 12000"}},
                     "required": ["url"]},
     "annotations": {"readOnlyHint": True, "openWorldHint": True}},
]


def call(name: str, args: dict) -> dict:
    try:
        if name == "web_search":
            text = web_search(args.get("query", ""), args.get("max_results", 8))
        elif name == "fetch_url":
            text = fetch_url(args.get("url", ""), args.get("start", 0), args.get("max_chars", 12000))
        else:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except WebError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (ValueError, TypeError) as e:
        return {"content": [{"type": "text", "text": f"bad arguments: {e}"}], "isError": True}


def handle(msg: dict):
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if "id" not in msg:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                                                          "capabilities": {"tools": {}},
                                                          "serverInfo": {"name": "strata-web", "version": "1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": rid, "result": call(params.get("name"), params.get("arguments") or {})}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}}


def main() -> None:
    for f in (sys.stdin, sys.stdout, sys.stderr):        # MCP's stdio is UTF-8, whatever the Windows code page is
        f.reconfigure(encoding="utf-8")
    lock = threading.Lock()

    def answer(msg):
        r = handle(msg)
        if r is not None:
            with lock:
                sys.stdout.write(json.dumps(r) + "\n")
                sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        if msg.get("method") == "tools/call":            # on a thread: a slow page must not block the next request
            threading.Thread(target=answer, args=(msg,), daemon=True).start()
        else:
            answer(msg)


if __name__ == "__main__":
    main()
