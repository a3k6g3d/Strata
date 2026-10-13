"""The "Still writing" guard: does the toast offer one button that stops the answer and then does what you asked?

Two answers at once is the one thing that still cannot happen, so asking a question while an answer is being
written has to be held, not lost, and it has to say which chat is holding it.  It asks the model for something
long, because the app is ES modules now and nothing outside it can pretend an answer is in progress the way a
console could - and the page's own signal that an answer is being written is the Stop button appearing by the
send button.  The question is asked in a second chat, because leaving a chat is allowed now (the answer keeps
arriving there), so the guard is on the question, not on the browsing.

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
    if not d.execute_script("return !document.getElementById('sessions').hidden"):
        d.find_element(By.ID, "sessions-toggle").click()   # the check reads the marks on the rows in the list
        time.sleep(0.5)
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
        old = d.execute_script("return document.querySelector('.sess--current')?.dataset.id || null")
        d.find_element(By.ID, "new-btn").click()          # leaving is allowed now; the answer follows its own chat
        time.sleep(0.6)
        print("a new chat opens while the other answer writes:", d.find_element(By.ID, "chat-title").text != title)
        box = d.find_element(By.ID, "input")
        box.send_keys("What was my first question? Answer in one short sentence.")
        box.send_keys(Keys.ENTER)
        time.sleep(0.6)
        toasts = d.find_elements(By.CSS_SELECTOR, ".st-toast--warn")
        if not toasts:
            print("FAIL: no toast - the question was swallowed quietly")
        else:
            t = toasts[-1]
            head = t.find_element(By.CLASS_NAME, "st-toast__title").text
            print("toast:", head, "|", t.find_element(By.CLASS_NAME, "t-text").text,
                  "| buttons:", [b.text for b in t.find_elements(By.TAG_NAME, "button")])
            print("it names the chat that is writing:", title[:18] in head)
            act = [b for b in t.find_elements(By.TAG_NAME, "button") if b.text.startswith("Stop")]
            if not act:
                print("FAIL: no way to act on it; you still have to find the Stop button yourself")
            else:
                act[0].click()
                done = time.time()
                # the stop button is the wrong thing to watch here: the held question goes out the moment the old
                # answer ends, so a new answer is writing straight away and the button is back on purpose.  What
                # has to stop is the old chat's own answer - so watch that one row's mark, not the button.
                STOPPED = f"""return (() => {{
                  const r = document.querySelector('.sess[data-id="{old}"]');
                  return !r || !r.querySelector('.sess__writing:not([hidden])');
                }})()"""
                while time.time() - done < 12 and not d.execute_script(STOPPED):
                    time.sleep(0.3)
                print("the answer in the other chat stopped:", d.execute_script(STOPPED),
                      f"({time.time() - done:.1f}s)")
                time.sleep(1.0)
                sent = d.find_element(By.ID, "chat").text
                print("and the question here went out:", "What was my first question" in sent,
                      "- a new answer is writing here:", d.execute_script(WRITING))
                print("  while the stopped chat keeps its mark off:", d.execute_script(STOPPED))
                print("the toast went away:", len(d.find_elements(By.CSS_SELECTOR, ".st-toast--warn")) == 0)
finally:
    d.quit()
