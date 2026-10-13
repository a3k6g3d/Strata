"""The side panel's helper: can a web page be shown inside Strata's own window?

Many sites forbid being put in a frame (X-Frame-Options, or a Content-Security-Policy `frame-ancestors` that leaves this page out).
The page cannot see those headers of another site, so the server asks: GET /panel/frameable?url=... answers whether the panel can
show it, or should say so and offer the page in the user's own browser instead.  The request is made the way the web tool's fetch is:
http(s) only, the usual ports, no user name in the address, every redirect checked again, and an address that leads to a private
network is refused.  One exception, asked for by the caller (`allow_loopback`): a page of *this* PC may be shown, so that a page
you are working on - the app itself on another port, a development server - can be looked at in the panel.  It is still held to
the allowed ports, still only this PC and never another machine of the local network, and every redirect is checked again.
"""
import http.client
import ipaddress
import socket
import ssl
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import strata_web_mcp as W  # noqa: E402

TIMEOUT = 6.0


def frameable_from_headers(headers, parent: str = "") -> tuple[bool, str]:
    """headers: anything with .get(name) -> str|None.  parent: the origin that would hold the frame (the page's own)."""
    xfo = (headers.get("X-Frame-Options") or "").strip().lower()
    if xfo in ("deny", "sameorigin") or xfo.startswith("allow-from"):
        return False, f"the site forbids being shown inside another page (X-Frame-Options: {xfo})"
    csp = headers.get("Content-Security-Policy") or ""
    for directive in csp.split(";"):
        parts = directive.strip().split()
        if parts and parts[0].lower() == "frame-ancestors":
            allowed = [t.lower() for t in parts[1:]]
            if "*" in allowed or "https:" in allowed or "http:" in allowed or (parent and parent.lower().rstrip("/") in allowed):
                return True, ""
            return False, "the site only lets certain pages show it (Content-Security-Policy: frame-ancestors)"
    return True, ""


def is_loopback(ip_text: str) -> bool:
    """Does this address lead to this very PC?  (::1 and a mapped ::ffff:127.0.0.1 count as 127.0.0.1.)"""
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def validate(url: str, allow_private: bool = False, allow_loopback: bool = False):
    """W.validate, with this PC let through when `allow_loopback`.  Everything else of the rule stays: the port must still be an
    allowed one, and a private address that is not this PC (a printer, a neighbour's machine) is still refused - the panel is not a
    way to probe the local network.  Raises W.WebError, like W.validate."""
    try:
        return W.validate(url, allow_private)
    except W.WebError as e:
        # only the "leads to this PC or a private network" refusal is worth a second look; a bad scheme, a user name in the
        # address or an odd port is the same refusal it always was
        if not allow_loopback or "private network" not in str(e):
            raise
        p, port, ip = W.validate(url, True)          # allow_private lets any address through, and skips the port rule
        if port not in W.PORTS:                      # ... so the port is checked again here
            raise W.WebError(f"port {port} is not allowed (80, 443, 8080 and 8443 are)") from None
        if not is_loopback(ip):
            raise W.WebError(f"blocked: {p.hostname!r} is another machine on the local network, not this PC") from None
        return p, port, ip


def check(url: str, parent: str = "", allow_private: bool = False, allow_loopback: bool = False) -> dict:
    """{"frameable": bool, "reason": str, "url": the address after redirects, "status": int}.  W.WebError for a refused address."""
    for _ in range(W.MAX_REDIRECTS + 1):
        try:
            p, port, ip = validate(url, allow_private, allow_loopback)
        except ValueError as e:                                 # an address that does not parse
            raise W.WebError(f"not a usable address: {e}") from None
        conn = (W._Https if p.scheme == "https" else W._Http)(p.hostname, port, ip, TIMEOUT)
        target = (p.path or "/") + (("?" + p.query) if p.query else "")
        try:
            for method in ("HEAD", "GET"):                      # a few servers answer HEAD with an error
                conn.request(method, target, headers={"Host": p.netloc.rsplit("@", 1)[-1], "User-Agent": W.UA, "Accept": "text/html,*/*;q=0.5",
                                                      "Accept-Encoding": "identity", "Connection": "close"})
                r = conn.getresponse()
                if method == "HEAD" and r.status in (405, 501):
                    r.read(256)
                    conn.close()
                    conn = (W._Https if p.scheme == "https" else W._Http)(p.hostname, port, ip, TIMEOUT)
                    continue
                break
            if r.status in (301, 302, 303, 307, 308) and r.getheader("Location"):
                url = urllib.parse.urljoin(url, r.getheader("Location"))
                continue
            ok, why = frameable_from_headers(type("H", (), {"get": staticmethod(lambda n, r=r: r.getheader(n))}), parent)
            return {"frameable": ok, "reason": why, "url": url, "status": r.status}
        except (OSError, http.client.HTTPException, ssl.SSLError, socket.timeout) as e:
            raise W.WebError(f"could not reach the site: {e}") from None
        finally:
            conn.close()
    raise W.WebError("too many redirects")
