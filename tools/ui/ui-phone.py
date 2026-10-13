"""UI harnesses for Strata: a browser looking at the page, and a server that shows one branch's files against the live
engine.  They need selenium and Chrome (py -I <this file>), and they talk to a running Strata - nothing here starts or
loads a model.  STRATA_UI_URL says which page to look at (default the review server on 8081, else the live app on 8080);
shots land in tools/ui/shots/.

Who is making the page wider than a phone?  Looks at one or more pages at 390px and lists what sticks
out past the right edge - the only way to find it, because the overflow is a computed width, not a mistake you can
see in the source.
"""
import os
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

o = Options()
o.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
for a in ("--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1440,900", "--hide-scrollbars"):
    o.add_argument(a)

PROBE = """
  const vw = document.documentElement.clientWidth;
  const out = [];
  for (const e of document.querySelectorAll('body *')) {
    const r = e.getBoundingClientRect();
    if (r.right > vw + 1 && r.width > 0) {
      const s = getComputedStyle(e);
      out.push({tag: e.tagName.toLowerCase(), id: e.id || null, cls: (e.className.baseVal !== undefined ? e.className.baseVal : e.className || '').toString().slice(0, 42),
                right: Math.round(r.right), w: Math.round(r.width), minw: s.minWidth, pos: s.position,
                txt: (e.textContent || '').trim().slice(0, 24)});
    }
  }
  out.sort((a, b) => b.right - a.right);
  return {vw, scrollW: document.documentElement.scrollWidth,
          overflow: document.documentElement.scrollWidth - vw,
          bodyScrollW: document.body.scrollWidth, offenders: out.slice(0, 8)};
"""

d = webdriver.Chrome(options=o)
try:
    urls = (sys.argv[1:] or [os.environ.get("STRATA_UI_URL", "http://127.0.0.1:8080/")])
    for url in urls:                                                              # more than one: compare them
        tag = url
        d.set_window_rect(0, 0, 1440, 900)
        d.get(url)
        time.sleep(1.6)
        d.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 780, "deviceScaleFactor": 1, "mobile": True})
        d.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 1})
        time.sleep(0.7)
        r = d.execute_script(PROBE)
        print("\n===== %s  (%s) =====" % (tag, url), flush=True)
        print("viewport %s, scrollWidth %s -> overflow %s px" % (r["vw"], r["scrollW"], r["overflow"]), flush=True)
        for x in r["offenders"]:
            print("   %-10s %-42s right=%-5s w=%-5s minw=%-8s pos=%-8s %r" %
                  (x["tag"], x["cls"], x["right"], x["w"], x["minw"], x["pos"], x["txt"]), flush=True)
        d.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
finally:
    d.quit()
