"""Does Strata's own Browser pane show a page of this PC now?

Loads the live app (http://127.0.0.1:8080/, the one with the side panel), opens the panel,
types an address of this PC, and reports what the pane decided: the frame's address, or the
notice it showed instead.  Nothing model-related is asked of the server.

Run:  py -I tools/ui/ui-panel-check.py   (the live app on 8080; a review server on 8081 is what it shows in the pane)
"""
import os, sys, time
SHOTS = os.environ.get("STRATA_UI_SHOTS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")
os.makedirs(SHOTS, exist_ok=True)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

APP = "http://127.0.0.1:8080/"
TARGETS = ["http://127.0.0.1:8081/",       # this PC, an odd port: should show (the rule is the address, not the port)
           "http://127.0.0.1:9999/",       # this PC, nothing listening: allowed, and fails at the connection
           "http://10.0.0.2:8443/",        # another machine on the local network: should be refused
           "http://192.168.1.1/"]          # a LAN address: should be refused
LOCAL = "http://127.0.0.1:8080/"          # Strata's own page: refused, and not by the network rule

o = Options()
o.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
for a in ("--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1440,900"):
    o.add_argument(a)
d = webdriver.Chrome(options=o)
d.set_page_load_timeout(60)


def panel_to(url):
    """Type an address into the pane and return what the pane ended up doing."""
    d.get(APP)
    time.sleep(2.5)
    d.find_element(By.ID, "panel-toggle").click()
    time.sleep(0.6)
    box = d.find_element(By.ID, "panel-url")
    box.clear()
    box.send_keys(url)
    box.submit() if box.get_attribute("form") else d.execute_script(
        "document.getElementById('panel-form').requestSubmit()")
    time.sleep(2.0)                       # the server is asked, and it opens the connection itself; a toast lives 3500ms,
                                          # so sampling later than this misses the reason for a refusal
    out = d.execute_script("""
      const f = document.getElementById('panel-frame');
      const notice = document.getElementById('panel-notice');
      const toasts = [...document.querySelectorAll('.toast, [class*=toast]')].map(t => t.textContent.trim()).filter(Boolean);
      // src alone can be a leftover from the last page; hidden is what says whether it is actually being shown
      return {src: f ? (f.src || '') : null, shown: !!f && !f.hidden,
              notice: notice && !notice.hidden ? notice.textContent.trim() : '',
              toasts};
    """)
    d.save_screenshot(os.path.join(SHOTS, "panel-check.png")) if os.path.isdir(SHOTS) else None
    return out


print("app under test:", APP, flush=True)
try:
    for u in TARGETS + [LOCAL]:
        r = panel_to(u)
        src, shown, notice, toasts = r["src"], r["shown"], r["notice"], r["toasts"]
        said = (notice + " " + " ".join(toasts)).strip()
        if shown and src and u in src:
            print(f"  SHOWN    {u}   frame -> {src}", flush=True)
        elif shown and src:
            print(f"  SHOWN?   {u}   frame -> {src} (not the address asked for)", flush=True)
        else:
            print(f"  REFUSED  {u}   {said or 'no frame shown, and no reason given'}", flush=True)
finally:
    d.quit()
