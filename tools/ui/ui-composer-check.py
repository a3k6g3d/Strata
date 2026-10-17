"""How tall is the message box, and did the controls shrink with it?  The composer sits at the bottom of
every screen you are on, so its height is taken off the answer you are reading - four lines of it, once.
One number per width: the box's height in px.  `py -I tools/ui/ui-composer-check.py` (needs the review
server on 8081, or point it at a page: `py -I tools/ui/ui-composer-check.py http://127.0.0.1:8080/`)."""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

o = Options()
o.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
for a in ("--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1440,900", "--hide-scrollbars"):
    o.add_argument(a)

# the box as the user first sees it: empty, so rows="1" and the min-height decide it.
MEAS = """const c=document.querySelector('.st-composer'), t=document.querySelector('#input'),
  bar=document.querySelector('.st-composer__bar'), b=bar.querySelector('.st-btn'),
  de=document.documentElement;
return {box: Math.round(c.getBoundingClientRect().height),
        text: Math.round(t.getBoundingClientRect().height),
        bar: Math.round(bar.getBoundingClientRect().height),
        btn: Math.round(b.getBoundingClientRect().height),
        lines: Math.round((de.clientHeight - t.getBoundingClientRect().height) / 24),
        coarse: matchMedia('(pointer: coarse)').matches,
        overflow: de.scrollWidth - de.clientWidth}"""

# A fingertip is not a bug: on touch the controls go back up to 44px on purpose, so the box is allowed
# to be taller there - but only there, and only that much.
MAX_DESKTOP, MAX_TOUCH, MIN_FINGERTIP = 92, 150, 44

d = webdriver.Chrome(options=o)
fails = []
try:
    d.set_window_rect(0, 0, 1440, 900)
    d.get(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8081/")
    time.sleep(1.4)
    for w, mobile in ((1440, False), (900, False), (390, True)):
        # mobile: true alone does not make (pointer: coarse) match - without the touch flag the phone
        # row measures the desktop branch and a touch-only rule looks fine when it is not being read.
        if mobile:
            d.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        d.execute_cdp_cmd("Emulation.setDeviceMetricsOverride",
                          {"width": w, "height": 800, "deviceScaleFactor": 1, "mobile": mobile})
        time.sleep(0.5)
        m = d.execute_script(MEAS)
        d.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
        if mobile:
            d.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": False})
        cap = MAX_TOUCH if m["coarse"] else MAX_DESKTOP
        ok = m["box"] <= cap and m["overflow"] == 0 and (not m["coarse"] or m["btn"] >= MIN_FINGERTIP)
        print(f"[{'PASS' if ok else 'FAIL'}] {w}px  box {m['box']}px  (text {m['text']} + bar {m['bar']}, "
              f"button {m['btn']}px, coarse={m['coarse']}, sideways {m['overflow']}px)  cap {cap}px", flush=True)
        if not ok:
            fails.append((w, m, cap))
finally:
    d.quit()
print("composer height:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
