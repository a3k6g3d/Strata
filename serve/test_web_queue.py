import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent / "web" / "app.js"

# The queue functions are taken out of app.js by name and run in node against stubs of the page (no browser needed).
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
const names = ['renderQueued', 'queueMessage', 'sendQueued', 'maybeSendQueued'];
const els = {};
const el = (id) => els[id] || (els[id] = {id, hidden: true, textContent: '', title: '', value: '',
  querySelector: () => (els[id + ':label'] = els[id + ':label'] || {textContent: ''})});
const out = {};
const ctx = {
  $: el, attachments: [], current: {id: 'B'}, busy: null, sent: [], renderAttachments() {}, autosize() {},
  writingHere: () => !!ctx.busy && ctx.busy.id === ctx.current.id,
  writingTitle: () => 'Strata', els, setTimeout: (f) => f(), send(q) { ctx.sent.push(q); }, out,
};
vm.createContext(ctx);
vm.runInContext(src.slice(src.indexOf('const queuedBy'), src.indexOf('function renderQueued(')) + names.map(grab).join('\n'), ctx);
vm.runInContext(process.argv[2], ctx);
console.log(JSON.stringify(ctx.out));
"""


def run_js(body):
    if shutil.which("node") is None:
        raise unittest.SkipTest("node is not installed")
    r = subprocess.run(["node", "-e", HARNESS, str(APP), body], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


class QueueAcrossChats(unittest.TestCase):
    def test_a_message_typed_while_another_chat_writes_is_queued_here(self):
        out = run_js("""
            busy = {id: 'A'}; current = {id: 'B'}; $('input').value = 'hello';
            queueMessage('hello');
            out.text = queuedBy.get('B').text; out.box = $('input').value; out.bar = !$('queued').hidden;
            out.label = $('queued:label') && $('queued:label').textContent;
        """)
        self.assertEqual((out["text"], out["box"], out["bar"]), ("hello", "", True))

    def test_each_chat_keeps_its_own_message(self):
        out = run_js("""
            busy = {id: 'A'};
            current = {id: 'B'}; queueMessage('for b'); queueMessage('and more');
            current = {id: 'C'}; queueMessage('for c');
            out.b = queuedBy.get('B').text; out.c = queuedBy.get('C').text; out.n = queuedBy.size;
        """)
        self.assertEqual((out["b"], out["c"], out["n"]), ("for b and more", "for c", 2))

    def test_the_bar_names_the_chat_it_waits_for(self):
        out = run_js("""
            busy = {id: 'A'}; current = {id: 'B'}; queueMessage('x'); renderQueued();
            out.waiting = els['queued'].querySelector().textContent;
            busy = {id: 'B'}; renderQueued(); out.same = els['queued'].querySelector().textContent;
        """)
        self.assertEqual(out["waiting"], "Waits for “Strata”")
        self.assertEqual(out["same"], "Queued")

    def test_it_goes_out_only_in_the_chat_on_screen_and_only_when_nothing_writes(self):
        out = run_js("""
            busy = {id: 'A'}; current = {id: 'B'}; queueMessage('for b');
            sendQueued(); out.while_busy = sent.length;
            busy = null; current = {id: 'C'}; maybeSendQueued(); out.other_chat = sent.length;
            current = {id: 'B'}; maybeSendQueued();
            out.sent = sent.map((q) => q.text); out.left = queuedBy.has('B');
        """)
        self.assertEqual((out["while_busy"], out["other_chat"], out["sent"], out["left"]), (0, 0, ["for b"], False))


if __name__ == "__main__":
    unittest.main()
