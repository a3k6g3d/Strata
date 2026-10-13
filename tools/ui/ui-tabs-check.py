"""Where do the view tabs sit in the header, at several widths?  One number to read: off (tab centre minus viewport centre)."""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

o = Options()
o.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
for a in ("--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1440,900", "--hide-scrollbars"):
    o.add_argument(a)

MEAS = """const t=document.querySelector('.st-tabs').getBoundingClientRect(),
  b=document.querySelector('.st-header__brand').getBoundingClientRect(),
  c=document.querySelector('.st-header__spacer').getBoundingClientRect(),
  de=document.documentElement;
return {off: Math.round(t.x + t.width/2 - de.clientWidth/2),
        gapAfterBrand: Math.round(t.x - b.right), gapBeforeControls: Math.round(c.right - t.right),
        overflow: de.scrollWidth - de.clientWidth}"""

d = webdriver.Chrome(options=o)
try:
    d.set_window_rect(0, 0, 1440, 900)
    d.get(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8081/")
    time.sleep(1.4)
    for w in (1440, 1200, 900, 860, 390):
        d.execute_cdp_cmd("Emulation.setDeviceMetricsOverride",
                          {"width": w, "height": 800, "deviceScaleFactor": 1, "mobile": w < 900})
        time.sleep(0.5)
        print(w, d.execute_script(MEAS), flush=True)
        d.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
finally:
    d.quit()
