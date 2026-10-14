"""Does a long-running command say so, while it runs?

The stream sends nothing at all while the server waits for a command, so a call that is running used to be a
blinking badge and a blank pause: a six-second sleep and a hung process looked the same.  The call now carries a
clock - the seconds it has taken, against the time it was given - on the call itself and on the group line above
it.  It has to appear while the command is out, count up, and be gone once the result lands.

The answer is fed in by a stubbed stream rather than by a real model: the clock is the page's own doing, and
waiting minutes on a small local model to happen to call a tool, and to still be calling it when the look happens,
is how a check like this gets ignored.  The stub announces a command with a 60-second limit, holds it open for
four seconds, and closes it - which is exactly the shape of a long command.

    python -I ui-run-check.py [url]        # default http://127.0.0.1:8081/
"""
import json, re, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

o = Options()
o.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
for a in ("--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1440,900", "--hide-scrollbars"):
    o.add_argument(a)
o.set_capability("goog:loggingPrefs", {"browser": "ALL"})

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8081/"
SECS = re.compile(r"^(\d+) s(?: of (\d+) s)?$")

# One answer, invented here.  Everything the page does with a real stream it does with this one: the only difference
# is that the command stays open for four seconds, on purpose, so the clock can be watched doing its job.
STUB = r"""
window.__stubbed = false;
const orig = window.fetch;
window.fetch = function (url, opts) {
  if (!String(url).includes("chat/completions")) { return orig.apply(this, arguments); }
  window.__stubbed = true;
  const enc = new TextEncoder();
  const line = (obj) => enc.encode("data: " + JSON.stringify(obj) + "\n\n");
  const call = {event: "call", id: "c1", name: "run_command", tool: "run_command", server: "exec", round: 1,
                kind: "exec", arguments: {command: "Start-Sleep -Seconds 6; echo SLEPT", timeout_s: 60}};
  const back = {event: "result", id: "c1", ok: true, result: "SLEPT", ms: 6500};
  return new Promise((resolve) => {
    const stream = new ReadableStream({start(c) {
      const at = (ms, chunk) => setTimeout(() => c.enqueue(chunk), ms);
      c.enqueue(enc.encode(": keep-alive\n\n"));                       // the engine reading the prompt
      at(400, line({choices: [{delta: {content: "Running it now. "}}]}));
      at(900, line({choices: [{delta: {}}], strata_mcp: call}));
      at(4600, line({choices: [{delta: {}}], strata_mcp: back}));
      at(4900, line({choices: [{delta: {content: "It printed SLEPT."}}], usage: {total_tokens: 42}}));
      at(5000, enc.encode("data: [DONE]\n\n"));
      setTimeout(() => c.close(), 5100);
    }});
    resolve({ok: true, status: 200, body: stream, headers: new Headers(), json: async () => ({})});
  });
};
"""

# one look at the page, as strings: the call sits inside a collapsible group, and a reference to something inside
# a closed one is not something to rely on
PROBE = ("return JSON.stringify({run: [...document.querySelectorAll('.tool-call[data-state=\"running\"]')]"
         ".map(e => e.dataset.state), grp: [...document.querySelectorAll('.tool-group')].map(e => e.dataset.state), "
         "clocks: [...document.querySelectorAll('.run-clock')].map(e => e.textContent), "
         "over: [...document.querySelectorAll('.run-clock.is-over')].map(e => e.textContent), "
         "busy: !document.getElementById('stop-btn').hidden})")

d = webdriver.Chrome(options=o)
try:
    d.set_window_rect(0, 0, 1440, 900)
    d.get(URL)
    d.execute_script("localStorage.setItem('strata.perm', 'full')")
    d.refresh()
    time.sleep(2.0)
    errs = [e["message"] for e in d.get_log("browser")
            if e["level"] == "SEVERE" and "favicon" not in e["message"] and "404" not in e["message"]]
    print("page loads clean:", not errs, errs[:2])
    d.execute_script(STUB)

    box = d.find_element(By.ID, "input")
    box.send_keys("Run that command for me.")
    box.send_keys(Keys.ENTER)

    seen, clocks, t0 = [], [], time.time()
    while time.time() - t0 < 30:
        p = json.loads(d.execute_script(PROBE) or "{}")
        key = (tuple(p.get("run", [])), tuple(p.get("grp", [])), tuple(p.get("clocks", [])))
        if key not in seen:
            seen.append(key)
            if p.get("clocks"):
                clocks.append((round(time.time() - t0, 1), p["clocks"], p.get("over", [])))
        if len(seen) > 3 and not p.get("busy"):
            break
        time.sleep(0.2)

    print("the answer was the stubbed one:", d.execute_script("return !!window.__stubbed"))
    if not clocks:
        print("FAIL: the command ran with no clock on show")
        for s in seen:
            print("   ", s)
        print("  on screen:", d.find_element(By.ID, "chat").text[-240:].replace("\n", " | "))
    else:
        print(f"the running command shows a clock: {len(clocks)} looks at it")
        for at, got, over in clocks:
            print(f"   {at:5.1f}s  {', '.join(got)}{'   (past its limit)' if over else ''}")
        nums = [int(SECS.match(c).group(1)) for _, got, _ in clocks for c in got if SECS.match(c)]
        print("  it counts up while the command is out:", len(nums) >= 2 and nums[-1] - nums[0] >= 2, nums)
        lim = [SECS.match(c).group(2) for _, got, _ in clocks for c in got if SECS.match(c)]
        print("  it names the limit it is measured against:", lim[-1] if lim and lim[-1] is not None else "(none)")
        print("  the group line above the call carries it too:", any(len(got) > 1 for _, got, _ in clocks))
        print("  it does not shout about a limit that was not passed:",
              not any(over for _, _, over in clocks))

    time.sleep(1.5)
    left = d.execute_script("return document.querySelectorAll('.run-clock').length")
    done = d.execute_script("return (document.querySelector('.tool-call[data-state=\"done\"]') || {})"
                            ".textContent || ''")
    print("the clock is gone once the result lands:", left == 0, f"({left} left)")
    print("  the time the server measured takes its place:", "6.5 s" in done)
    print("  the answer itself arrived:", "SLEPT" in d.find_element(By.ID, "chat").text)
finally:
    d.quit()
