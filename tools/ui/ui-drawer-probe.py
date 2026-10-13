import os, sys, time
SHOTS = os.environ.get("STRATA_UI_SHOTS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")
os.makedirs(SHOTS, exist_ok=True)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
o = Options()
o.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
for a in ("--headless=new","--disable-gpu","--no-sandbox","--window-size=1440,900","--hide-scrollbars"): o.add_argument(a)
MEAS = ("const de=document.documentElement,dr=document.querySelector('.st-drawer'),r=dr.getBoundingClientRect();"
        "return {vw:de.clientWidth,innerW:window.innerWidth,scrollW:de.scrollWidth,x:Math.round(r.x),"
        "right:Math.round(r.right),onscreen:r.x>=0&&r.x<de.clientWidth,focus:document.activeElement.id,aria:dr.getAttribute('aria-hidden')}")
d = webdriver.Chrome(options=o)
try:
    d.set_window_rect(0,0,1440,900); d.get(os.environ.get("STRATA_UI_URL", "http://127.0.0.1:8081/")); time.sleep(1.6)
    for label, vw in (("desktop 1424", None),):
        print(label, "closed ", d.execute_script(MEAS), flush=True)
        d.execute_script("document.querySelector('#sampling-btn').click()"); time.sleep(0.6)
        print(label, "open   ", d.execute_script(MEAS), flush=True)
        d.save_screenshot(os.path.join(SHOTS, "drawer-desktop.png"))
        d.execute_script("document.querySelector('#drawer-close').click()"); time.sleep(0.5)
        print(label, "closed2", d.execute_script(MEAS), flush=True)
    d.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width":390,"height":780,"deviceScaleFactor":1,"mobile":True})
    time.sleep(0.8)
    print("phone 390 closed ", d.execute_script(MEAS), flush=True)
    d.execute_script("document.querySelector('#sampling-btn').click()"); time.sleep(0.7)
    print("phone 390 open   ", d.execute_script(MEAS), flush=True)
    d.save_screenshot(os.path.join(SHOTS, "drawer-phone.png"))
    d.execute_script("document.querySelector('#scrim').click()"); time.sleep(0.6)
    print("phone 390 closed2", d.execute_script(MEAS), flush=True)
    print("console errors:", [e["message"][:120] for e in d.get_log("browser") if e["level"]=="SEVERE"], flush=True)
finally:
    d.quit()
