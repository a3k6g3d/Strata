import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent / "web" / "app.js"

# Select text in an answer -> Reply / Start a side chat (app.js, selPop).  The quoting is taken out of app.js by name
# and run in node, as test_web_queue.py does; the popup itself was checked by hand in the web app (it needs a page).
HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('no function ' + name);
  let d = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}' && --d === 0) return src.slice(i, k + 1);
  }
}
const ctx = {};
vm.createContext(ctx);
vm.runInContext(grab('quoted'), ctx);
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map((c) => vm.runInContext('quoted(' + JSON.stringify(c) + ')', ctx))));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class QuotedSelection(unittest.TestCase):
    def quote(self, *texts):
        r = subprocess.run(["node", "-e", HARNESS, str(APP), json.dumps(list(texts))], capture_output=True, text=True,
                           encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_every_line_is_quoted_and_a_blank_line_follows(self):
        # the question about the quote is typed after it, on a line of its own
        self.assertEqual(self.quote("the golden files expect 6\nbut setup emits 8"),
                         ["> the golden files expect 6\n> but setup emits 8\n\n"])

    def test_the_selection_is_trimmed_first(self):
        # a drag usually catches a space or a newline at either end; it should not become an empty quoted line
        self.assertEqual(self.quote("  its golden file disagree.\n"), ["> its golden file disagree.\n\n"])

    def test_windows_line_ends_do_not_leave_a_carriage_return_in_the_quote(self):
        self.assertEqual(self.quote("one\r\ntwo"), ["> one\n> two\n\n"])

    def history(self, list_, at, writing_index=None):
        js = (r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const i = src.indexOf('function sideChatHistory(');
let d = 0, body = '';
for (let k = src.indexOf('{', i); k < src.length; k++) {
  if (src[k] === '{') d++; else if (src[k] === '}' && --d === 0) { body = src.slice(i, k + 1); break; }
}
const ctx = {}; vm.createContext(ctx); vm.runInContext(body, ctx);
const [list, at, w] = JSON.parse(process.argv[2]);
ctx.list = list; ctx.at = at; ctx.writing = w === null ? null : list[w];
const out = vm.runInContext('sideChatHistory(list, at, writing)', ctx);
out.length && (out[0].text = 'EDITED');          // an edit to the copy must not reach the original
console.log(JSON.stringify({out, original: list[0].text}));
""")
        r = subprocess.run(["node", "-e", js, str(APP), json.dumps([list_, at, writing_index])], capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_a_side_chat_carries_the_conversation_up_to_the_chosen_answer(self):
        chat = [{"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"},
                {"role": "user", "text": "q2"}, {"role": "assistant", "text": "a2"}]
        got = self.history(chat, 1)
        self.assertEqual([m["text"] for m in got["out"]], ["EDITED", "a1"], "up to and including answer 1, nothing after")
        self.assertEqual(got["original"], "q1", "the copy shares an object with the original chat")
        self.assertEqual(len(self.history(chat, 3)["out"]), 4)

    def test_an_answer_still_being_written_is_left_out(self):
        chat = [{"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"},
                {"role": "user", "text": "q2"}, {"role": "assistant", "text": "half an ans"}]
        self.assertEqual(len(self.history(chat, 3, writing_index=3)["out"]), 3)
        self.assertEqual(len(self.history(chat, 1, writing_index=3)["out"]), 2, "a stream after the answer changes nothing")

    def test_the_popup_and_both_actions_are_wired(self):
        src = APP.read_text(encoding="utf-8")
        for needle in ('data-sel="side"', 'data-sel="reply"', '.st-msg--assistant .st-bubble', "await newSession()"):
            self.assertIn(needle, src)


if __name__ == "__main__":
    unittest.main()
