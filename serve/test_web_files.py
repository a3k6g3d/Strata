import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent / "web"
APP = WEB / "app.js"

# Piece A of docs/WEB_UI_REDESIGN.md: file cards, "+a -d" counts and the File tab.  The pure logic (between the
# <files:start> and <files:end> markers, plus groupLabel) is taken out of app.js and run in node; the sandbox of the
# preview frame is checked in the markup, because it is the thing that keeps model-written code away from the page.
HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const block = src.slice(src.indexOf('// <files:start>'), src.indexOf('// <files:end>'));
function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  let d = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}' && --d === 0) return src.slice(i, k + 1);
  }
}
const search = src.match(/const SEARCH_TOOL = .*;/)[0];
const ctx = {};
vm.createContext(ctx);
vm.runInContext(block + '\n' + search + '\n' + grab('groupLabel'), ctx);
const cases = JSON.parse(process.argv[2]);
const out = cases.map((c) => {
  const r = vm.runInContext(c.expr, Object.assign(ctx, {input: c.input}));
  return r === undefined ? null : JSON.parse(JSON.stringify(r, (k, v) => (Object.prototype.toString.call(v) === '[object Map]' ? [...v.values()] : v)));
});
console.log(JSON.stringify(out));
"""


def write(path, content):
    return {"tool": "write_file", "state": "done", "kind": "write", "arguments": {"path": path, "content": content}}


def edit(path, old, new, **kw):
    return {"tool": "edit_file", "state": "done", "kind": "write", "arguments": {"path": path, "oldText": old, "newText": new, **kw}}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class FileLogic(unittest.TestCase):
    def run_js(self, expr, value):
        r = subprocess.run(["node", "-e", HARNESS, str(APP), json.dumps([{"expr": expr, "input": value}])], capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)[0]

    def changes(self, tools):
        return self.run_js("fileChanges(input)", tools)

    def label(self, tools):
        return self.run_js("groupLabel(input.map((t) => [t]))", tools)

    def test_a_written_file_has_its_text_and_its_lines_counted_as_added(self):
        (f,) = self.changes([write("C:/w/index.html", "<p>a</p>\n<p>b</p>\n")])
        self.assertEqual((f["name"], f["add"], f["del"], f["content"]), ("index.html", 2, 0, "<p>a</p>\n<p>b</p>\n"))

    def test_edits_after_a_write_are_applied_to_the_text_and_counted(self):
        (f,) = self.changes([write("a.txt", "one\ntwo\nthree\n"), edit("a.txt", "two", "TWO\nand more")])
        self.assertEqual(f["content"], "one\nTWO\nand more\nthree\n")
        self.assertEqual((f["add"], f["del"], f["wrote"], f["edited"]), (3 + 2, 1, 1, 1))

    def test_replace_all_changes_every_place(self):
        (f,) = self.changes([write("a.txt", "x y x"), edit("a.txt", "x", "z", replace_all=True)])
        self.assertEqual(f["content"], "z y z")

    def test_an_edit_that_came_first_leaves_the_text_unknown(self):
        # the file already existed: the page never saw it, so there is nothing honest to preview
        (f,) = self.changes([edit("a.txt", "old", "new")])
        self.assertIsNone(f["content"])
        self.assertEqual((f["add"], f["del"]), (1, 1))

    def test_an_edit_whose_text_is_not_found_leaves_the_text_unknown(self):
        (f,) = self.changes([write("a.txt", "hello"), edit("a.txt", "absent", "x")])
        self.assertIsNone(f["content"])

    def test_a_failed_or_other_call_is_not_a_change(self):
        failed = write("a.txt", "x")
        failed["state"] = "error"
        other = {"tool": "run_command", "state": "done", "kind": "exec", "arguments": {"command": "dir"}}
        self.assertEqual(self.changes([failed, other]), [])

    def test_two_files_stay_two(self):
        got = self.changes([write("a.txt", "1"), write("sub/b.py", "2\n3")])
        self.assertEqual([(f["name"], f["add"]) for f in got], [("a.txt", 1), ("b.py", 2)])

    def test_windows_paths_give_the_file_name(self):
        (f,) = self.changes([write("C:\\work\\pagoda\\index.html", "x")])
        self.assertEqual(f["name"], "index.html")

    def test_the_group_label_counts_files_and_lines(self):
        t = [write("a.html", "1\n2\n3\n"), edit("a.html", "2", "two"), write("b.js", "x\ny\n")]
        self.assertEqual(self.label(t), "Edited 2 files +6 \u22121")   # 3 written + 1 by the edit + 2 in the other file; 1 removed

    def test_a_group_of_only_writes_says_wrote(self):
        self.assertEqual(self.label([write("a.html", "1\n2\n")]), "Wrote 1 file +2 \u22120")

    def test_the_label_keeps_the_other_counts_beside_the_files(self):
        run = {"tool": "run_command", "state": "done", "kind": "exec", "arguments": {}}
        self.assertEqual(self.label([run, run, write("a.html", "x")]), "Ran 2 commands, wrote 1 file +1 \u22120")

    def test_file_kind_and_size(self):
        self.assertEqual(self.run_js("[fileKindOf('index.html'), fileKindOf('Makefile'), fileKindOf('a.tar'), fileKindOf('x.PY')]", None),
                         ["HTML", "FILE", "TAR", "PY"])
        self.assertEqual(self.run_js("[fileSize(900), fileSize(6656), fileSize(3 * 1048576)]", None), ["900 B", "6.5 KB", "3.0 MB"])


class PreviewSandbox(unittest.TestCase):
    def test_the_preview_frame_can_only_run_scripts(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        tag = re.search(r"<iframe[^>]*id=\"file-frame\"[^>]*>", html)
        self.assertIsNotNone(tag, "the File tab's frame is missing")
        sandbox = re.search(r'sandbox="([^"]*)"', tag.group(0))
        self.assertIsNotNone(sandbox, "the preview frame has no sandbox attribute: model-written code would run as the page")
        self.assertEqual(sandbox.group(1).split(), ["allow-scripts"],
                         "the preview may allow scripts and nothing else - above all not allow-same-origin")

    def test_the_page_never_loosens_it_from_script(self):
        js = APP.read_text(encoding="utf-8")
        self.assertNotIn("file-frame\").sandbox", js)
        self.assertNotIn("file-frame').sandbox", js)
        self.assertNotIn("removeAttribute(\"sandbox\")", js)

    def test_open_only_ever_runs_on_a_click(self):
        # the owner's decision (docs/WEB_UI_REDESIGN.md): the preview opens on a click, never by itself
        js = APP.read_text(encoding="utf-8")
        self.assertEqual(js.count('setPanel(true, "file")'), 1, "something other than the Open click opens the File tab")
        self.assertIn('e.target.closest("[data-file-open]")', js)


if __name__ == "__main__":
    unittest.main()
