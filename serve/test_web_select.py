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

    def test_the_popup_and_both_actions_are_wired(self):
        src = APP.read_text(encoding="utf-8")
        for needle in ('data-sel="side"', 'data-sel="reply"', '.st-msg--assistant .st-bubble', "await newSession()"):
            self.assertIn(needle, src)


if __name__ == "__main__":
    unittest.main()
