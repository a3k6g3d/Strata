"""Does the web console page show the lines the server printed, and keep showing them correctly?

The page (/console, GET /api/log) is the server's own window in a browser tab: it exists because the lines that
explain a slow request or a model that is still loading used to be readable only in that window, and since 0.1.42
the engine's stderr goes to a log file.  So the thing worth guarding is not the markup - it is that a line printed
by the server arrives in the page exactly once, and that a line the browser has already shown is never shown twice.

Like ui-monitor-check.py, this starts its own mock engine: the tee that fills the buffer is installed by main(),
which the live server may or may not have been started with, and pointing at 8080 would read a missing buffer as
"the page is broken".  Pass a url to point it at a real server instead.

    python tools/ui/ui-webconsole-check.py [url]   # default: a mock server this script starts itself
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from serve import server
from serve.frontend import ChatTemplate
from serve.server import ByteTokenizer, MockEngine, Service, serve

ROOT = Path(__file__).resolve().parents[2]
FAILURES = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def start_server():
    """A mock engine, one completed request, and one printed warning - stdout teed into the console buffer the way
    main() does it, so the page has something real to show.  The tee stays installed for the caller's browser run:
    lines the server prints while the browser is polling are the point of the check."""
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, "Thought.</think>\nHello.", max_context=16384), tok,
                  ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
    httpd = serve(svc, port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    sys.stdout = server.ConsoleTee(sys.__stdout__ or sys.stdout)
    req = urllib.request.Request(base + "/v1/chat/completions", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"messages": [{"role": "user", "content": "Say hello."}],
                                                  "max_tokens": 8}).encode())
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
    print("[strata] WARNING: the engine has said nothing for 30 s", flush=True)
    return httpd, base


def lines(d):
    return d.execute_script("return [...document.querySelectorAll('#log .line')].map(l => "
                            "[l.textContent, l.className]);")


def main():
    owned = None
    url = sys.argv[1] if len(sys.argv) > 1 else None
    if not url:
        owned, base = start_server()
        url = base + "/console"
        print(f"mock server on {base} (the tee main() installs is installed here too)")
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
        time.sleep(2.5)
        errors = [e["message"] for e in d.get_log("browser")
                  if e["level"] in ("SEVERE", "WARNING") or "error" in e["message"].lower()]
        check("the page loads without console errors", not errors, "; ".join(errors[:2]))

        shown = lines(d)
        check("the lines the server printed are on the page", len(shown) >= 2, f"{len(shown)} line(s)")
        check("the request's own line is among them",
              any("done:" in t for t, _ in shown), repr([t for t, _ in shown][:3]))
        check("the warning line is marked, so it stands out in a screen of plain text",
              any("bad" in c for t, c in shown if "WARNING" in t),
              repr([c for t, c in shown if "WARNING" in t]))
        check("it says it is following", "following" in d.find_element(By.ID, "state").text.lower(),
              d.find_element(By.ID, "state").text)
        check("the line count is named", "of 500" in d.find_element(By.ID, "count").text,
              d.find_element(By.ID, "count").text)

        # the page polls on a timer; a second poll must not repeat what is already shown
        before = len(lines(d))
        print("[strata] answering: 7 of max 6400 tokens", flush=True)
        time.sleep(2.5)
        after = lines(d)
        check("a line printed while the page was open arrives", len(after) > before, f"{before} -> {len(after)}")
        check("lines already shown are not shown again", len(after) == len(set(t for t, _ in after)),
              f"{len(after)} nodes, {len(set(t for t, _ in after))} distinct")
        d.save_screenshot(str(ROOT / "tools" / "ui" / "shots" / "console-desktop.png"))

        # Pause is the reason the page is usable: a long answer prints faster than you can read it
        d.find_element(By.ID, "pause").click()
        time.sleep(0.4)
        print("[strata] a line that must not appear while paused", flush=True)
        time.sleep(2.5)
        check("Pause holds the view where it is", not any("must not appear" in t for t, _ in lines(d)))
        check("the button says how to get the live view back", d.find_element(By.ID, "pause").text == "Resume",
              d.find_element(By.ID, "pause").text)
        d.find_element(By.ID, "pause").click()
        time.sleep(2.5)
        check("Resume catches up on what arrived while paused",
              any("must not appear" in t for t, _ in lines(d)))

        for label, control in (("Following", "follow"), ("Copy", "copy"), ("Clear view", "clear")):
            check(f"the {label.lower()} control is there", d.find_element(By.ID, control).is_displayed())

        # the page has to work on a phone too: it is the page you open when the server window is on another machine
        d.execute_script("window.__desktop = document.documentElement.scrollWidth")
        d.set_window_rect(0, 0, 500, 800)
        d.execute_script("""const e = document.createElement('meta');
            e.name='viewport'; e.content='width=390'; document.head.append(e);""")
        d.execute_script("document.documentElement.style.width='390px'")
        time.sleep(1.0)
        wide = d.execute_script("""
            return [...document.querySelectorAll('body *')].filter(el => {
                const r = el.getBoundingClientRect();
                return r.right > 392 && r.width > 0;
            }).map(el => el.tagName + '.' + el.className + '@' + Math.round(el.getBoundingClientRect().right));
        """)
        check("nothing hangs off the side at phone width", not wide, "; ".join(wide[:3]))
        check("the log itself is still on screen at phone width",
              d.execute_script("const r = document.getElementById('log').getBoundingClientRect();"
                               "return r.width > 200 && r.right <= 392"))
        d.save_screenshot(str(ROOT / "tools" / "ui" / "shots" / "console-phone.png"))
    finally:
        d.quit()
        if owned is not None:
            owned.shutdown()
            sys.stdout = sys.__stdout__

    print("\n" + ("all good" if not FAILURES else "FAILED: " + "; ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
