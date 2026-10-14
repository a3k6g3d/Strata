"""Does the request monitor page show what a request cost, and the expert cache's share beside it?

The page only exists when the server runs with the monitor on (`"api_monitor": true` / `--api-monitor`), and the
live server is usually started without it - so this starts its own mock engine on a spare port rather than
pointing at 8080 and reading a 404 as "the page is broken".  Pass a url to point it at a real server instead.

What it is guarding: the hit rate is the number that says whether the expert cache is helping or costing, and it
used to live only in the server log.  It sits beside the decode speed for exactly that reason - the measured runs
in docs/DETAILS.md at 94.7% and 93.9% hit decoded at 13.7 and 102 tok/s - so a field that silently disappears
leaves people tuning a cache by feel.

    python tools/ui/ui-monitor-check.py [url]     # default: a mock server this script starts itself
"""
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from serve.frontend import ChatTemplate
from serve.server import ByteTokenizer, MockEngine, Service, serve

FAILURES = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def start_server():
    """A mock engine with the monitor on, and one completed request to look at."""
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, "Thought.</think>\nHello.", max_context=16384), tok,
                  ChatTemplate(Path(__file__).resolve().parents[2] / "serve" / "chat_template.jinja"))
    svc.api_monitor = True
    httpd = serve(svc, port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    req = urllib.request.Request(base + "/v1/chat/completions", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"messages": [{"role": "user", "content": "Say hello."}],
                                                  "max_tokens": 8}).encode())
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
    return httpd, base


def main():
    owned = None
    url = sys.argv[1] if len(sys.argv) > 1 else None
    if not url:
        owned, base = start_server()
        url = base + "/api-monitor"
        print(f"mock server on {base} (the live server usually runs without --api-monitor)")
    print(f"page: {url}\n")

    o = Options()
    o.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    for a in ("--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1440,900", "--hide-scrollbars"):
        o.add_argument(a)
    o.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    d = webdriver.Chrome(options=o)
    try:
        d.set_window_rect(0, 0, 1440, 900)
        d.get(url)
        time.sleep(2.0)
        errors = [e["message"] for e in d.get_log("browser")
                  if e["level"] in ("SEVERE", "WARNING") or "error" in e["message"].lower()]
        check("the page loads without console errors", not errors, "; ".join(errors[:2]))

        rows = d.find_elements(By.CSS_SELECTOR, ".requests .request")
        check("a request row is listed", len(rows) >= 1, f"{len(rows)} row(s)")
        if not rows:
            return
        rows[0].click()
        time.sleep(0.6)

        facts = d.execute_script("""
            return [...document.querySelectorAll('.facts .fact')].map(f =>
                [f.querySelector('span')?.textContent.trim(), f.querySelector('b')?.id,
                 f.querySelector('b')?.textContent.trim()]);
        """)
        named = {label: (fid, value) for label, fid, value in facts}
        print("  facts shown:", ", ".join(label for label, _, _ in facts))

        check("the request card has an Expert cache figure", "Expert cache" in named,
              "the card shows: " + ", ".join(named))
        if "Expert cache" in named:
            fid, value = named["Expert cache"]
            check("it is filled by the script, not left as the markup's placeholder", fid == "detail-hit")
            # the mock engine reports no cache counters, so the honest answer here is the em-dash placeholder.
            # A real engine fills it with "NN.N%" (plus "· +N.N% over PCIe"); serve/test_monitor.py covers that.
            check("with no cache figures from the engine it shows a dash, not a fake 0%",
                  value in ("\u2014", "-"), repr(value))
            check("the figure says a high hit rate is not a good result by itself",
                  "not a good result" in (d.execute_script(
                      "return document.getElementById('detail-hit')?.getAttribute('title') || ''")),
                  "the tooltip is the only place that says it")
            # the shape a real engine's figures take (the mock one has none): `cache` is monitor.js's own formatter
            shown = d.execute_script("return cache({hit_rate: 0.75, pcie_share: 0.2})")
            check("a real engine's figures read as the log says them", shown == "75.0% \u00b7 +20.0% over PCIe", repr(shown))
            check("an engine with no PCIe share to report says only the hit rate",
                  d.execute_script("return cache({hit_rate: 0.914})") == "91.4%", "a '+0%' would invent a figure")

        # the other figures the card is built from must survive the same render pass, or the new field
        # displaced one of them in the markup
        for label in ("Decode speed", "Prompt tokens", "Output tokens", "First token"):
            check(f"the {label.lower()} figure is still on the card", label in named)
        check("decode speed is filled in", named.get("Decode speed", ("", ""))[0] == "detail-speed")
    finally:
        d.quit()
        if owned is not None:
            owned.shutdown()
            owned.server_close()

    print("\n" + ("all good" if not FAILURES else "FAILED: " + "; ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
