"""Does the page load cleanly?  Prints every console message and page error.

Worth having now that the app is ES modules: a bad import path or a typo in an `export` does not show up as a
visible failure - the script simply never runs, and the page looks like a blank chat with no explanation.
Nothing here should print except the lines you expect from the app itself.

    python -I ui-console-check.py [url]        # default http://127.0.0.1:8081/
"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

o = Options()
o.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
for a in ("--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1440,900", "--hide-scrollbars"):
    o.add_argument(a)
o.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})

d = webdriver.Chrome(options=o)
try:
    d.set_window_rect(0, 0, 1440, 900)
    d.get(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8081/")
    time.sleep(2.0)
    for e in d.get_log("browser"):
        if e["level"] in ("SEVERE", "WARNING") or "error" in e["message"].lower():
            print(e["level"], e["message"][:300])
    # the app is only running if it filled in things only it knows how to fill in.  The names are the ones the
    # app actually puts on the page (serve/web/app.js): a chat row is .sess, not .st-session - a selector that
    # no longer matches prints 0 and looks like a broken app, which is how a good check gets ignored.
    print("chat boxes:", d.execute_script("return document.querySelectorAll('.st-msg').length"))
    print("session rows:", d.execute_script("return document.querySelectorAll('.sess').length"))
    print("metric cards:", d.execute_script("return document.querySelectorAll('.st-metric').length"))
    print("tabs:", [t.text.strip() for t in d.find_elements(By.CLASS_NAME, "st-tab")])
finally:
    d.quit()
