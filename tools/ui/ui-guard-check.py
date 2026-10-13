"""The "Still writing" guard: does the toast offer one button that stops the answer and then does what you asked?

The page refuses to switch or clear a chat while an answer is being written.  It used to say "stop first" and
leave the clicking to you, so this checks the toast comes with a button, and that the button really stops the
answer and then does what you originally asked.  It asks the model for something long, because the app is ES
modules now and nothing outside it can pretend an answer is in progress the way a console could - and the
page's own signal that an answer is being written is the Stop button appearing by the send button.

    python -I ui-guard-check.py [url]        # default http://127.0.0.1:8081/
"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

o = Options()
o.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
for a in ("--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1440,900", "--hide-scrollbars"):
    o.add_argument(a)

PROMPT = "Count from 1 to 400, one number per line, nothing else."
WRITING = "return !document.getElementById('stop-btn').hidden"      # the page's own "an answer is being written"

d = webdriver.Chrome(options=o)
try:
    d.set_window_rect(0, 0, 1440, 900)
    d.get(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8081/")
    time.sleep(1.8)
    box = d.find_element(By.ID, "input")
    box.send_keys(PROMPT)
    box.send_keys(Keys.ENTER)
    t0 = time.time()
    while time.time() - t0 < 60 and not d.execute_script(WRITING):
        time.sleep(0.4)
    if not d.execute_script(WRITING):
        print("FAIL: the answer never started; nothing to guard")
    else:
        title = d.find_element(By.ID, "chat-title").text
        d.find_element(By.ID, "new-btn").click()
        time.sleep(0.4)
        toasts = d.find_elements(By.CSS_SELECTOR, ".st-toast--warn")
        if not toasts:
            print("FAIL: no toast - the click was swallowed quietly")
        else:
            t = toasts[-1]
            print("toast:", t.find_element(By.CLASS_NAME, "st-toast__title").text, "|",
                  t.find_element(By.CLASS_NAME, "t-text").text,
                  "| buttons:", [b.text for b in t.find_elements(By.TAG_NAME, "button")])
            print("the chat is left alone until you act:", d.find_element(By.ID, "chat-title").text == title)
            act = [b for b in t.find_elements(By.TAG_NAME, "button") if b.text.startswith("Stop")]
            if not act:
                print("FAIL: no way to act on it; you still have to find the Stop button yourself")
            else:
                act[0].click()
                done = time.time()
                while time.time() - done < 10 and d.execute_script(WRITING):
                    time.sleep(0.3)
                time.sleep(0.6)
                print("the answer stopped:", not d.execute_script(WRITING), f"({time.time() - done:.1f}s)")
                print("and then it did what was asked:", d.find_element(By.ID, "chat-title").text != title)
                print("the toast went away:", len(d.find_elements(By.CSS_SELECTOR, ".st-toast--warn")) == 0)
finally:
    d.quit()
