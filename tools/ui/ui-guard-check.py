"""The "Still writing" guard: does the toast offer one button that stops the answer and then does what you asked?

The page refuses to switch or clear a chat while an answer is being written.  It used to say "stop first" and
leave the clicking to you, so this checks the toast comes with a button, and that the button really aborts the
stream.  A live generation is slow and costs tokens, so the page's own request is stood in for by an
AbortController made here: the guard only ever touches busy.controller.
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

d = webdriver.Chrome(options=o)
try:
    d.set_window_rect(0, 0, 1440, 900)
    d.get(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080/")
    time.sleep(1.6)
    # pretend an answer is being written, then click "New chat"
    d.execute_script("""busy = {controller: new AbortController(), started: Date.now()};
                        window.__sig = busy.controller.signal;""")
    d.find_element(By.ID, "new-btn").click()
    time.sleep(0.4)
    toast = d.find_elements(By.CSS_SELECTOR, ".st-toast--warn")
    title = toast[-1].find_element(By.CLASS_NAME, "st-toast__title").text if toast else "(no toast)"
    body = toast[-1].find_element(By.CLASS_NAME, "t-text").text if toast else ""
    btns = [b.text for b in toast[-1].find_elements(By.TAG_NAME, "button")] if toast else []
    print("toast:", title, "|", body, "| buttons:", btns)
    print("chat untouched:", d.find_element(By.ID, "chat-title").text)
    act = [b for b in (toast[-1].find_elements(By.TAG_NAME, "button") if toast else []) if b.text.startswith("Stop")]
    if not act:
        print("FAIL: no way to act on it; you still have to find the stop button yourself")
    else:
        act[0].click()
        time.sleep(0.5)
        print("stream aborted:", d.execute_script("return window.__sig.aborted"))
        print("toast gone:", len(d.find_elements(By.CSS_SELECTOR, ".st-toast--warn")) == 0)
finally:
    d.quit()
