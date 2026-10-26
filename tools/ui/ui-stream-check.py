"""Answers arriving in the background: does one chat's answer stay in its own chat while you read another?

The old rule was "stop the answer before you may look at anything else", which made people stop answers they
did not want stopped.  The answer now belongs to the chat that asked for it: you may browse, start a new chat
and keep working, the list says which chat is still writing, and opening that chat shows the answer mid-flight
rather than a stale copy from the server.  This walks that path and reports each step.

    python -I ui-stream-check.py [url]       # default http://127.0.0.1:8081/
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

PROMPT = "Count from 1 to 500, one number per line, nothing else."
WRITING = "return !document.getElementById('stop-btn').hidden"
ROWS = "return [...document.querySelectorAll('.sess')].map(r => r.dataset.id)"

CURSOR = "return !!document.querySelector('#chat .st-bubble.cursor, #chat .st-bubble .cursor')"  # a live stream
# paints a caret in the bubble; a copy fetched from the server never does

def click_writing_row(d):
    """The list re-draws while an answer arrives, so the row can go stale under the click; try again."""
    for _ in range(6):
        try:
            d.find_element(By.CSS_SELECTOR, ".sess--writing .sess__title").click()
            return True
        except Exception:
            time.sleep(0.4)
    return False

d = webdriver.Chrome(options=o)
try:
    d.set_window_rect(0, 0, 1440, 900)
    d.get(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8081/")
    time.sleep(2.0)
    if not d.execute_script("return !document.getElementById('sessions').hidden"):
        print("note: the chat list is closed; opening it (the check needs it)")
        d.find_element(By.ID, "sessions-toggle").click()
        time.sleep(0.5)
    box = d.find_element(By.ID, "input")
    box.send_keys(PROMPT)
    box.send_keys(Keys.ENTER)
    t0 = time.time()
    while time.time() - t0 < 60 and not d.execute_script(WRITING):
        time.sleep(0.4)
    if not d.execute_script(WRITING):
        print("FAIL: the answer never started")
    else:
        before = d.execute_script(ROWS) or []
        title = d.find_element(By.ID, "chat-title").text
        print(f"asking in one chat ({title[:28]!r}); {len(before)} chats in the list")

        d.find_element(By.ID, "session-new").click()
        time.sleep(0.8)
        print("browsing to a new chat was allowed:", d.find_element(By.ID, "chat-title").text != title)
        print("  no warning in the way:", not d.find_elements(By.CSS_SELECTOR, ".st-toast--warn"))
        print("  the composer still says an answer is in flight:",
              d.execute_script("return document.getElementById('composer-hint').textContent"))
        mark = d.find_elements(By.CSS_SELECTOR, ".sess__writing:not([hidden])")
        print("  the list marks the chat that is writing:", len(mark) == 1,
              (mark[0].text if mark else ""), "|",
              d.find_element(By.CSS_SELECTOR, ".sess--writing .sess__title").text[:28] if d.find_elements(By.CSS_SELECTOR, ".sess--writing") else "-")

        box = d.find_element(By.ID, "input")
        box.send_keys("Hello there")
        box.send_keys(Keys.ENTER)
        time.sleep(0.6)
        toasts = d.find_elements(By.CSS_SELECTOR, ".st-toast--warn")
        print("asking a second question is held, with the writing chat named:",
              bool(toasts) and title[:18] in toasts[-1].find_element(By.CLASS_NAME, "st-toast__title").text)
        # nothing was sent, so nothing was taken out of the box: the question is still there to be edited
        print("  and it is still in the box, not thrown away:",
              d.execute_script("return document.getElementById('input').value") == "Hello there")
        # the half-typed question belongs to this chat: leaving must not carry it to the next one
        d.find_element(By.ID, "session-new").click()
        time.sleep(0.6)
        print("  leaving takes it with the chat, it does not follow you:",
              d.execute_script("return document.getElementById('input').value") == "")

        click_writing_row(d)                                         # go to the chat that is writing
        time.sleep(1.0)
        back = d.find_element(By.ID, "chat-title").text
        body = d.find_element(By.ID, "chat").text
        print("opening it again shows the same chat:", back == title or title[:18] in back)
        print("  the live answer is on screen, not a stale copy:", "1" in body and "2" in body,
              f"({len(body)} chars so far)")
        print("  the composer knows this chat is the one writing:",
              d.execute_script("return document.getElementById('composer-hint').textContent"))
        # the caret in the bubble is what says "this is the answer on its way", not a copy fetched from the server
        print("  the answer on screen is the live one, it carries the caret:", d.execute_script(CURSOR))
        # and it keeps growing here - slowly, at the speed the model writes, so wait for it rather than assume it
        grew, deadline = len(body), time.time() + 15.0
        more = False
        while time.time() < deadline and not more:
            time.sleep(1.0)
            more = len(d.find_element(By.ID, "chat").text) > grew or not d.execute_script(WRITING)
        print("  and more of it lands while you watch:", more)
        print("  the row keeps its mark, whether or not it is on screen:",
              bool(d.find_elements(By.CSS_SELECTOR, ".sess--writing .sess__writing:not([hidden])")))
        # and a chat that has a row keeps its own half-typed question: type here, go away, come back
        d.find_element(By.ID, "stop-btn").click()
        time.sleep(1.5)
        print("stopping it leaves the partial answer in its own chat:", not d.execute_script(WRITING))
        box = d.find_element(By.ID, "input")
        box.send_keys("a draft left here")
        row = d.execute_script("return document.querySelector('.sess--current').dataset.id")
        d.find_element(By.ID, "session-new").click()
        time.sleep(0.6)
        print("  it does not follow you to the next chat:",
              d.execute_script("return document.getElementById('input').value") == "")
        d.find_element(By.CSS_SELECTOR, f".sess[data-id='{row}'] .sess__title").click()
        time.sleep(0.9)
        print("  and it is waiting when you come back:",
              d.execute_script("return document.getElementById('input').value") == "a draft left here")
        time.sleep(6.0)                                       # the list refreshes on its own beat after the stop
        print("the mark comes off once nothing is writing:", not d.find_elements(By.CSS_SELECTOR, ".sess--writing"))
finally:
    d.quit()
