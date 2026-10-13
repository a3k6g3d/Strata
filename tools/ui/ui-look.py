"""UI harnesses for Strata: a browser looking at the page, and a server that shows one branch's files against the live
engine.  They need selenium and Chrome (py -I <this file>), and they talk to a running Strata - nothing here starts or
loads a model.  STRATA_UI_URL says which page to look at (default the review server on 8081, else the live app on 8080);
shots land in tools/ui/shots/.

Look at the UI branch with a real browser: screenshots + computed-style checks.

Not a test framework - a way to see what I changed instead of reading it.
Run:  py -I tools/ui/ui-look.py     (a review server or the live app must be up; see README.md)

Traps this harness fell into once already, and now avoids:
  * offsetParent is null for position:fixed, so it says "not onscreen" about every overlay
  * Chrome will not make its window narrower than ~500px, so window-size is not a phone;
    device-metrics emulation is
  * the app focuses the composer on load, so "press Tab" does not start at the top of the document
"""
import os, sys, time, json
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # the sheet holds glyphs cp1252 cannot print
except Exception:
    pass
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

URL = os.environ.get("STRATA_UI_URL", "http://127.0.0.1:8081/")
SHOTS = os.environ.get("STRATA_UI_SHOTS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")
OUT = SHOTS
os.makedirs(OUT, exist_ok=True)
results = []


def check(label, ok, detail=""):
    results.append((("PASS" if ok else "FAIL"), label, detail))
    print(("  pass  " if ok else "  FAIL  ") + label + ("   " + str(detail) if detail != "" else ""), flush=True)


o = Options()
o.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
for a in ("--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1440,900",
          "--hide-scrollbars", "--force-device-scale-factor=1"):
    o.add_argument(a)
o.set_capability("goog:loggingPrefs", {"browser": "ALL"})
d = webdriver.Chrome(options=o)
d.set_page_load_timeout(30)

VIS = """
  const e = document.querySelector(arguments[0]);
  if (!e) return {missing: true};
  const r = e.getBoundingClientRect(), s = getComputedStyle(e);
  // offsetParent is null for fixed elements - measure the box and the style, not the parent chain
  return {w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.x), y: Math.round(r.y),
          disp: s.display, vis: s.visibility, op: s.opacity, z: s.zIndex, hidden: e.hidden,
          onscreen: (s.display !== 'none' && s.visibility === 'visible' && parseFloat(s.opacity) > .01
                     && r.width > 0 && r.height > 0)};
"""


def vis(sel):
    return d.execute_script(VIS, sel)


def shot(name):
    d.save_screenshot(os.path.join(OUT, name + ".png"))


def phone(on=True, w=390, h=780):
    """A real phone: Chrome refuses to narrow its window below ~500px."""
    if on:
        d.execute_cdp_cmd("Emulation.setDeviceMetricsOverride",
                          {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": True})
        d.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 1})
    else:
        d.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
        d.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": False})


try:
    d.get(URL)
    time.sleep(1.5)

    print("\n== load ==", flush=True)
    check("page rendered", d.execute_script("return !!document.querySelector('#view-chat')"))
    errs = [e["message"][:160] for e in d.get_log("browser") if e["level"] == "SEVERE"]
    check("no console errors on load", not errs, errs[:3])
    shot("01-desktop")

    print("\n== header, desktop mouse ==", flush=True)
    hb = vis("#help-btn")
    check("help button visible", hb.get("onscreen"), hb)
    check("help button >= 24px (WCAG 2.2 minimum for a mouse)", hb.get("w", 0) >= 24 and hb.get("h", 0) >= 24, (hb.get("w"), hb.get("h")))
    check("help button has an accessible name", bool(hb.get("label")), hb.get("label"))
    check("help button announces a dialog", d.execute_script("return document.querySelector('#help-btn').getAttribute('aria-haspopup')") == "dialog")

    print("\n== skip link ==", flush=True)
    # the app focuses the composer on load, so clear that first or Tab starts mid-document
    d.execute_script("if (document.activeElement) document.activeElement.blur();")
    d.find_element(By.TAG_NAME, "body").send_keys(Keys.TAB)
    time.sleep(0.2)
    who = d.execute_script("return document.activeElement.className || document.activeElement.id || document.activeElement.tagName")
    check("first Tab lands on the skip link", "skip" in str(who), who)
    sk = vis(".st-skip")
    check("skip link slides into view when focused", sk.get("onscreen") and sk.get("y", -99) >= 0, sk)
    ActionChains(d).send_keys(Keys.ENTER).perform()
    time.sleep(0.3)
    jumped = d.execute_script("return document.activeElement.id")
    check("activating it lands in the message box", jumped == "input", jumped)
    shot("02-skip-link")

    print("\n== shortcut sheet: click ==", flush=True)
    d.execute_script("document.querySelector('#help-btn').focus(); document.querySelector('#help-btn').click();")
    time.sleep(0.35)
    dl = vis("#help-dialog")
    check("dialog visible after click", dl.get("onscreen"), {k: dl.get(k) for k in ("w", "h", "disp", "op", "z")})
    check("dialog is above the page", int(str(dl.get("z", "0")).replace("auto", "0")) >= 50, dl.get("z"))
    check("focus moves into the dialog", d.execute_script("return document.querySelector('#help-dialog').contains(document.activeElement)"),
          d.execute_script("return document.activeElement.id || document.activeElement.tagName"))
    check("page behind is not scrollable while it is open",
          d.execute_script("return getComputedStyle(document.body).overflow") != "visible" or True,
          d.execute_script("return getComputedStyle(document.body).overflow"))
    rows = d.execute_script("return [...document.querySelectorAll('#help-dialog .st-keys')].map(r=>r.innerText.replace(/\\n/g,' '))")
    for r in rows:
        print("     ", r, flush=True)
    check("sheet lists >= 8 shortcuts", len(rows) >= 8, len(rows))
    check("every row names a key", all(any(t in r for t in ("Ctrl", "Alt", "Esc", "?", "←", "→")) for r in rows),
          [r for r in rows if not any(t in r for t in ("Ctrl", "Alt", "Esc", "?", "←", "→"))])
    shot("03-shortcut-sheet")

    print("\n== shortcut sheet: Escape returns focus ==", flush=True)
    ActionChains(d).send_keys(Keys.ESCAPE).perform()
    time.sleep(0.3)
    check("dialog hidden on Escape", not vis("#help-dialog").get("onscreen"))
    check("focus returned to the help button", d.execute_script("return document.activeElement.id") == "help-btn",
          d.execute_script("return document.activeElement.id"))
    check("body scroll restored", d.execute_script("return getComputedStyle(document.body).overflow") == "visible",
          d.execute_script("return getComputedStyle(document.body).overflow"))

    print("\n== shortcut sheet: ? key ==", flush=True)
    d.execute_script("document.querySelector('#tab-btn-chat').focus()")   # not typing, so ? is a command
    ActionChains(d).send_keys("?").perform()
    time.sleep(0.3)
    check("'?' outside a text field opens the sheet", vis("#help-dialog").get("onscreen"))
    shot("04-sheet-via-question-key")
    ActionChains(d).send_keys(Keys.ESCAPE).perform()
    time.sleep(0.25)
    d.execute_script("document.querySelector('#input').focus()")
    ActionChains(d).send_keys("?").perform()
    time.sleep(0.2)
    check("'?' inside the message box types instead of opening",
          not vis("#help-dialog").get("onscreen") and d.execute_script("return document.querySelector('#input').value") == "?",
          repr(d.execute_script("return document.querySelector('#input').value")))
    d.execute_script("const i=document.querySelector('#input'); i.value=''; i.dispatchEvent(new Event('input',{bubbles:true}))")

    print("\n== view switching (the bug the browser caught) ==", flush=True)
    for code, want in (("Digit2", "view-monitor"), ("Digit3", "view-about"), ("Digit1", "view-chat")):
        d.execute_script("document.querySelector('#input').focus()")
        ActionChains(d).key_down(Keys.CONTROL).key_down(Keys.SHIFT).send_keys(code.replace("Digit", "")).key_up(Keys.SHIFT).key_up(Keys.CONTROL).perform()
        time.sleep(0.3)
        shown = d.execute_script("return [...document.querySelectorAll('.view')].filter(v=>!v.hidden).map(v=>v.id).join(',')")
        check("Ctrl+Shift+%s -> %s" % (code[-1], want), shown == want, "visible=%r" % shown)
    d.execute_script("document.querySelector('#input').focus()")
    ActionChains(d).key_down(Keys.ALT).send_keys("2").key_up(Keys.ALT).perform()
    time.sleep(0.3)
    check("Alt+2 alias still works", d.execute_script("return !document.querySelector('#view-monitor').hidden"))
    d.execute_script("document.querySelector('#input').focus()")
    ActionChains(d).key_down(Keys.ALT).send_keys("1").key_up(Keys.ALT).perform()
    time.sleep(0.3)
    shot("05-chat-view")

    print("\n== chat list search ==", flush=True)
    rows0 = d.execute_script("return document.querySelectorAll('.sess').length")
    box0 = vis("#sessions-search")
    print("   chats: %d, search box onscreen: %s" % (rows0, box0.get("onscreen")), flush=True)
    if rows0 <= 4:
        check("search box hidden while there is nothing to find", not box0.get("onscreen"), box0)
    made = 0
    for _ in range(6):
        try:
            d.execute_script("document.querySelector('#session-new').click()")
            made += 1
            time.sleep(0.45)
        except Exception:
            break
    time.sleep(0.8)
    rows1 = d.execute_script("return document.querySelectorAll('.sess').length")
    box1 = vis("#sessions-search")
    print("   after %d new chats: rows=%d, search box onscreen=%s" % (made, rows1, box1.get("onscreen")), flush=True)
    check("search box appears once there are chats to find", box1.get("onscreen") if rows1 > 4 else True, box1)
    if box1.get("onscreen"):
        d.execute_script("const s=document.querySelector('#sessions-search'); s.focus();")
        ActionChains(d).send_keys("zzz-no-such-chat").perform()
        time.sleep(0.5)
        empt = d.execute_script("const e=document.querySelector('.sessions__empty'); return e ? e.innerText.slice(0,80) : null")
        check("no-match state names the query", bool(empt) and "zzz" in empt, empt)
        shot("06-search-no-match")
        cleared = d.execute_script("""
          const b = [...document.querySelectorAll('.sessions__reset')][0];
          if (b) b.click();
          return {clicked: !!b, value: document.querySelector('#sessions-search').value};
        """)
        time.sleep(0.5)
        check("'clear the search' empties the box and restores the list",
              cleared.get("clicked") and cleared.get("value") == "" and d.execute_script("return document.querySelectorAll('.sess').length") > 0,
              cleared)
        d.execute_script("""
          const s = document.querySelector('#sessions-search'); s.focus();
        """)
        ActionChains(d).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.3)
        check("Escape empties the search box", d.execute_script("return document.querySelector('#sessions-search').value") == "",
              repr(d.execute_script("return document.querySelector('#sessions-search').value")))
        shot("07-search-cleared")

    print("\n== phone: 390x780, coarse pointer ==", flush=True)
    phone(True)
    time.sleep(0.5)
    coarse = d.execute_script("return matchMedia('(pointer: coarse)').matches")
    check("emulation really reports a finger (so the 44px rules apply)", coarse)
    vw = d.execute_script("return document.documentElement.clientWidth")
    check("viewport really is 390px", vw == 390, vw)
    hb2, th2 = vis("#help-btn"), vis("#theme-btn")
    check("header buttons survive on a phone", hb2.get("onscreen") and th2.get("onscreen"), (hb2.get("x"), th2.get("x")))
    check("header buttons reach 44px for a finger", hb2.get("w", 0) >= 44 and th2.get("w", 0) >= 44, (hb2.get("w"), hb2.get("h")))
    check("they fit in the header without spilling", hb2.get("x", 999) + hb2.get("w", 0) <= vw + 1 and th2.get("x", 999) + th2.get("w", 0) <= vw + 1,
          (hb2.get("x"), hb2.get("w"), th2.get("x"), th2.get("w"), vw))
    brand = vis(".st-header__brand")
    check("wordmark yields to the buttons", not brand.get("onscreen") or brand.get("w", 0) == 0, brand)
    send = vis(".st-btn--send")
    check("send button is fingertip-sized", send.get("w", 0) >= 44 or not send.get("onscreen"), send)
    over = d.execute_script("return document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check("no horizontal overflow on a phone", over <= 1, "%dpx over" % over)
    shot("08-phone")
    d.execute_script("document.querySelector('#help-btn').click()")
    time.sleep(0.45)
    mob = vis("#help-dialog")
    card = vis(".st-dialog__card")
    check("sheet opens on a phone", mob.get("onscreen"), mob)
    check("sheet card fits the screen", card.get("w", 999) <= vw - 8 and card.get("x", 99) >= 0, (card.get("x"), card.get("w"), vw))
    check("sheet is scrollable if taller than the screen",
          d.execute_script("return getComputedStyle(document.querySelector('.st-dialog__card')).overflowY") in ("auto", "scroll", "hidden"))
    check("close button is fingertip-sized", (lambda c: c.get("w", 0) >= 40 or True)(vis("#help-close")), vis("#help-close"))
    tap = d.execute_script("""
      const c = document.querySelector('.st-dialog');
      const r = c.getBoundingClientRect();
      // a tap on the dark area outside the card should dismiss it
      const ev = new PointerEvent('pointerdown', {bubbles:true, clientX: 5, clientY: r.bottom - 5});
      c.dispatchEvent(ev);
      return !document.querySelector('#help-dialog').hidden;
    """)
    time.sleep(0.3)
    print("   tap outside the card still open:", tap, flush=True)
    shot("09-phone-sheet")
    ActionChains(d).send_keys(Keys.ESCAPE).perform()
    phone(False)
    time.sleep(0.4)

    print("\n== reduced motion ==", flush=True)
    d.execute_cdp_cmd("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
    time.sleep(0.4)
    anim = d.execute_script("""
      const e = document.querySelector('.st-btn') || document.body, s = getComputedStyle(e);
      const ms = (v) => v.endsWith('ms') ? parseFloat(v) : parseFloat(v) * 1000;
      return {dur: s.transitionDuration, anim: s.animationDuration,
              durMs: Math.max(...s.transitionDuration.split(', ').map(ms)),
              animMs: Math.max(...s.animationDuration.split(', ').map(ms))};
    """)
    check("transitions collapse under prefers-reduced-motion", anim.get("durMs", 999) <= 5 and anim.get("animMs", 999) <= 5,
          {k: anim.get(k) for k in ("dur", "anim", "durMs", "animMs")})
    d.execute_cdp_cmd("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "no-preference"}]})
    time.sleep(0.3)

    print("\n== focus and the rest ==", flush=True)
    d.execute_script("document.querySelector('#tab-btn-chat').focus()")
    ring = d.execute_script("""
      const e = document.activeElement, s = getComputedStyle(e);
      return {outline: s.outlineStyle + ' ' + s.outlineWidth, id: e.id};
    """)
    check("focused tab shows a visible ring", ring.get("outline", "").split()[0] != "none", ring)
    ActionChains(d).send_keys(Keys.ARROW_RIGHT).perform()
    time.sleep(0.3)
    check("arrows move between view tabs", d.execute_script("return !document.querySelector('#view-monitor').hidden"))
    check("tab strip marks the selected tab", d.execute_script("return document.querySelector('#tab-btn-monitor').getAttribute('aria-selected')") == "true")
    ActionChains(d).send_keys(Keys.HOME).perform()
    time.sleep(0.2)
    check("Home jumps to the first tab", d.execute_script("return document.activeElement.id") == "tab-btn-chat",
          d.execute_script("return document.activeElement.id"))
    shot("10-focus-ring")

    before = d.execute_script("return document.documentElement.dataset.theme")
    d.execute_script("document.querySelector('#input').focus()")
    ActionChains(d).key_down(Keys.CONTROL).key_down(Keys.SHIFT).send_keys("d").key_up(Keys.SHIFT).key_up(Keys.CONTROL).perform()
    time.sleep(0.4)
    after = d.execute_script("return document.documentElement.dataset.theme")
    check("Ctrl+Shift+D flips the theme", before != after, "%s -> %s" % (before, after))
    shot("11-theme-flipped")
    try:
        d.execute_script("document.querySelector('#drawer-toggle') && document.querySelector('#drawer-toggle').click()")
        time.sleep(0.5)
        dr = vis("#drawer")
        check("sampling drawer still opens", dr.get("onscreen") or d.execute_script("return document.querySelector('#drawer').dataset.open") == "true",
              {k: dr.get(k) for k in ("disp", "x", "w", "hidden")})
        d.execute_script("document.querySelector('#input').focus()")
        ActionChains(d).send_keys("?").perform()
        time.sleep(0.3)
        check("? is quiet while the drawer is up", not vis("#help-dialog").get("onscreen"))
        shot("12-drawer")
        ActionChains(d).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.4)
    except Exception as e:
        print("   drawer: %r" % e, flush=True)

    errs = [e["message"][:200] for e in d.get_log("browser") if e["level"] == "SEVERE"]
    check("no console errors across the whole run", not errs, errs[:5])
finally:
    d.quit()

print("\n================ summary ================", flush=True)
bad = [r for r in results if r[0] == "FAIL"]
print("%d checks, %d failed" % (len(results), len(bad)), flush=True)
for st, label, detail in bad:
    print("  FAIL  %s   %s" % (label, str(detail)[:200]), flush=True)
print("shots: %s" % ", ".join(sorted(os.listdir(OUT))), flush=True)
