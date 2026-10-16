"""serve/test_files_mcp.py - tools/strata_files_mcp.py: what it may touch, what it refuses to change, and what it
keeps about a file it writes.  Everything happens in a temp folder; nothing outside it is touched.

    python -m unittest serve.test_files_mcp -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import strata_files_mcp as x  # noqa: E402


class Files(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="strata-files-")
        self.inside = os.path.join(self.base, "notes")
        self.outside = os.path.join(self.base, "elsewhere")
        os.makedirs(self.inside)
        os.makedirs(self.outside)
        self.f = os.path.join(self.inside, "todo.md")
        with open(self.f, "w", encoding="utf-8") as h:
            h.write("buy milk\nfix the sink\n")
        x.configure([], [], [self.inside])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    # ---- what it may touch

    def test_paths_outside_the_named_folders_are_refused(self):
        other = os.path.join(self.outside, "secret.txt")
        with open(other, "w", encoding="utf-8") as h:
            h.write("hidden\n")
        for tool in (lambda: x.read_file(other), lambda: x.write_file(other, "x\n"),
                     lambda: x.edit_file(other, "hidden", "shown")):
            with self.assertRaises(x.FilesError) as cm:
                tool()
            self.assertIn("outside the folders", str(cm.exception))

    def test_a_whole_drive_as_a_root_reaches_the_files_under_it(self):
        # A drive root ends with a separator, which is exactly where a "root + separator" check goes wrong: the
        # path under it has one backslash after the colon, so a root kept as "C:\\" matches nothing.
        drive = os.path.splitdrive(self.base)[0] + os.sep
        x.configure([], [], [drive])
        self.assertIn("buy milk", x.read_file(self.f))
        # another drive is outside it (a letter that does not exist here: no filesystem is touched)
        other = os.path.join("Z:" + os.sep, "__strata_not_here__", "x.txt")
        with self.assertRaises(x.FilesError) as cm:
            x.read_file(other)
        self.assertIn("outside the folders", str(cm.exception))

    def test_dot_dot_cannot_step_outside(self):
        self.assertRaises(x.FilesError, x.read_file, os.path.join(self.inside, "..", "elsewhere", "secret.txt"))

    def test_the_no_read_list_refuses_reads_and_changes_alike(self):
        secret = os.path.join(self.inside, "pass.txt")
        with open(secret, "w", encoding="utf-8") as h:
            h.write("hunter2\n")
        x.configure([os.path.normcase(os.path.realpath(self.inside + os.sep + "pass.txt"))], [], [self.inside])
        for bad in (lambda: x.read_file(secret), lambda: x.edit_file(secret, "hunter2", "hunter3")):
            with self.assertRaises(x.FilesError) as cm:
                bad()
            self.assertIn("no-read list", str(cm.exception))

    def test_a_folder_is_not_a_file_and_a_missing_one_says_where(self):
        with self.assertRaises(x.FilesError) as cm:
            x.read_file(self.inside)
        self.assertIn("that is a folder", str(cm.exception))
        gone = os.path.join(self.inside, "nope", "deep.md")
        with self.assertRaises(x.FilesError) as cm:
            x.read_file(gone)
        self.assertIn("its folder does not exist", str(cm.exception))

    def test_no_folders_named_at_all(self):
        x.configure([], [], [])
        with self.assertRaises(x.FilesError) as cm:
            x.read_file(self.f)
        self.assertIn("without any folders", str(cm.exception))

    # ---- reading

    def test_read_the_whole_file(self):
        out = x.read_file(self.f)
        self.assertIn("buy milk\nfix the sink", out)
        self.assertIn("3 lines", out)                      # the last newline makes an empty final line

    def test_head_and_tail(self):
        big = os.path.join(self.inside, "big.txt")
        with open(big, "w", encoding="utf-8") as h:
            h.write("\n".join(f"line {i}" for i in range(500)))
        self.assertIn("line 0\nline 1", x.read_file(big, head=2))
        out = x.read_file(big, head=1, tail=1)
        self.assertIn("line 0", out)
        self.assertIn("line 499", out)
        self.assertNotIn("line 250", out)
        self.assertIn("[...]", out)

    def test_binary_and_undecodable_files_are_not_shown_as_text(self):
        blob = os.path.join(self.inside, "blob.bin")
        with open(blob, "wb") as h:
            h.write(b"\x00\x01\x02binary")
        with self.assertRaises(x.FilesError) as cm:
            x.read_file(blob)
        self.assertIn("binary", str(cm.exception))
        latin = os.path.join(self.inside, "latin.txt")
        with open(latin, "wb") as h:
            h.write(b"caf\xe9\n")
        with self.assertRaises(x.FilesError) as cm:
            x.read_file(latin)
        self.assertIn("not UTF-8", str(cm.exception))

    # ---- writing: what is kept

    def test_create_a_new_file_needs_an_existing_folder(self):
        new = os.path.join(self.inside, "fresh.md")
        self.assertIn("created", x.write_file(new, "hello\n"))
        self.assertEqual(Path(new).read_text(encoding="utf-8"), "hello\n")
        with self.assertRaises(x.FilesError) as cm:
            x.write_file(os.path.join(self.inside, "sub", "a.txt"), "x")
        self.assertIn("its folder does not exist", str(cm.exception))

    def test_an_existing_file_must_be_read_before_it_is_replaced(self):
        with self.assertRaises(x.FilesError) as cm:
            x.write_file(self.f, "guessed content\n")
        self.assertIn("has not been read", str(cm.exception))
        self.assertEqual(Path(self.f).read_text(encoding="utf-8"), "buy milk\nfix the sink\n")   # nothing was written
        x.read_file(self.f)
        self.assertIn("replaced", x.write_file(self.f, "guessed content\n"))
        self.assertEqual(Path(self.f).read_text(encoding="utf-8"), "guessed content\n")

    def test_line_endings_bom_and_final_newline_survive_a_change(self):
        crlf = os.path.join(self.inside, "win.txt")
        with open(crlf, "wb") as h:
            h.write("\ufefffirst\r\nsecond\r\n".encode("utf-8"))   # a BOM, written the way an editor writes one
        x.read_file(crlf)
        x.edit_file(crlf, "second", "SECOND")
        raw = Path(crlf).read_bytes()
        self.assertEqual(raw, "\ufefffirst\r\nSECOND\r\n".encode("utf-8"))
        self.assertIn("CR LF", x.read_file(crlf))            # the model is told, so it writes them back

    def test_an_edit_written_with_lf_still_matches_a_crlf_file(self):
        crlf = os.path.join(self.inside, "win2.txt")
        with open(crlf, "wb") as h:
            h.write(b"one\r\ntwo\r\n")
        x.edit_file(crlf, "one\ntwo", "ONE\nTWO")           # the endings the model saw, not the ones on disk
        self.assertEqual(Path(crlf).read_bytes(), b"ONE\r\nTWO\r\n")

    def test_a_file_that_ended_with_a_newline_still_does(self):
        x.read_file(self.f)
        x.write_file(self.f, "no trailing newline")
        self.assertEqual(Path(self.f).read_text(encoding="utf-8"), "no trailing newline\n")

    def test_a_failed_write_leaves_the_file_alone_and_no_temporary_file(self):
        x.read_file(self.f)
        with self.assertRaises(x.FilesError):
            x.write_file(self.f, "x" * (x.MAX_WRITE + 1))
        self.assertEqual(Path(self.f).read_text(encoding="utf-8"), "buy milk\nfix the sink\n")
        self.assertEqual([p for p in os.listdir(self.inside) if p.endswith(".strata-tmp")], [])

    # ---- editing

    def test_an_exact_match_is_changed_and_the_change_is_shown(self):
        out = x.edit_file(self.f, "fix the sink", "fix the roof")
        self.assertIn("changed", out)
        self.assertIn("-fix the sink", out)
        self.assertIn("+fix the roof", out)
        self.assertEqual(Path(self.f).read_text(encoding="utf-8"), "buy milk\nfix the roof\n")

    def test_a_guess_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(x.FilesError) as cm:
            x.edit_file(self.f, "fix the sink badly", "whatever")
        self.assertIn("not in the file", str(cm.exception))
        self.assertEqual(Path(self.f).read_text(encoding="utf-8"), "buy milk\nfix the sink\n")

    def test_ambiguous_text_asks_for_more_or_for_replace_all(self):
        twice = os.path.join(self.inside, "twice.txt")
        with open(twice, "w", encoding="utf-8") as h:
            h.write("same\nsame\n")
        with self.assertRaises(x.FilesError) as cm:
            x.edit_file(twice, "same", "other")
        self.assertIn("appears 2 times", str(cm.exception))
        self.assertEqual(Path(twice).read_text(encoding="utf-8"), "same\nsame\n")     # nothing was changed
        self.assertIn(": 2 places (", x.edit_file(twice, "same", "other", replace_all=True))
        self.assertEqual(Path(twice).read_text(encoding="utf-8"), "other\nother\n")
        # more of the surrounding lines says which one: one place, changed
        self.assertIn(": 1 place (", x.edit_file(twice, "other\nother", "one\nother"))
        self.assertEqual(Path(twice).read_text(encoding="utf-8"), "one\nother\n")

    def test_an_empty_match_would_change_everything(self):
        with self.assertRaises(x.FilesError) as cm:
            x.edit_file(self.f, "", "x")
        self.assertIn("empty", str(cm.exception))

    def test_the_content_that_is_refused_for_being_too_long(self):
        with self.assertRaises(x.FilesError) as cm:
            x.write_file(os.path.join(self.inside, "big.md"), "x" * (x.MAX_WRITE + 1))
        self.assertIn("longer than", str(cm.exception))


class LookingAround(unittest.TestCase):
    """The folder tools: what is where, and the two that change the tree."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="strata-files-tree-")
        self.inside = os.path.join(self.base, "notes")
        os.makedirs(os.path.join(self.inside, "sub"))
        os.makedirs(os.path.join(self.base, "elsewhere"))
        self.f = os.path.join(self.inside, "todo.md")
        Path(self.f).write_text("buy milk\n", encoding="utf-8")
        Path(os.path.join(self.inside, "sub", "deep.txt")).write_text("deep\n", encoding="utf-8")
        Path(os.path.join(self.base, "elsewhere", "out.md")).write_text("out\n", encoding="utf-8")
        x.configure([], [], [self.inside])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def test_it_lists_a_folder_with_folders_first(self):
        out = x.list_directory(self.inside)
        self.assertIn("[DIR]  sub", out)
        self.assertIn("[FILE] todo.md", out)
        self.assertLess(out.index("[DIR]"), out.index("[FILE]"))

    def test_it_can_show_sizes(self):
        self.assertIn("B", x.list_directory(self.inside, True))

    def test_a_file_is_not_a_folder(self):
        with self.assertRaises(x.FilesError):
            x.list_directory(self.f)

    def test_the_tree_goes_down_to_the_depth_asked_for(self):
        self.assertIn("sub/", x.directory_tree(self.inside))
        self.assertIn("deep.txt", x.directory_tree(self.inside, 2))
        self.assertNotIn("deep.txt", x.directory_tree(self.inside, 1))

    def test_search_finds_by_name_under_the_folder(self):
        self.assertIn("deep.txt", x.search_files(self.inside, "*.txt"))
        self.assertNotIn("out.md", x.search_files(self.inside, "*.md"))     # elsewhere is outside the allow-list
        self.assertIn("nothing", x.search_files(self.inside, "*.nope"))

    def test_it_needs_a_pattern_to_search_for(self):
        with self.assertRaises(x.FilesError):
            x.search_files(self.inside, "")

    def test_the_info_of_a_file_and_of_a_folder(self):
        info = x.get_file_info(self.f)
        self.assertIn("type: file", info)
        self.assertIn("modified:", info)
        self.assertIn("type: folder", x.get_file_info(self.inside))

    def test_it_names_the_folders_it_may_use(self):
        self.assertIn(self.inside, x.list_allowed_directories())

    def test_it_reads_several_files_and_reports_the_ones_that_failed(self):
        out = x.read_multiple_files([self.f, os.path.join(self.inside, "nope.md")])
        self.assertIn("buy milk", out)
        self.assertIn("could not read it", out)
        self.assertIn("1 of them could not be read", out)

    # ---- the two that change the tree

    def test_it_makes_a_folder_and_the_ones_above_it(self):
        new = os.path.join(self.inside, "a", "b")
        self.assertIn("created folder", x.create_directory(new))
        self.assertTrue(os.path.isdir(new))
        self.assertIn("already there", x.create_directory(new))

    def test_a_new_folder_lets_a_new_file_be_written_at_once(self):
        new = os.path.join(self.inside, "inbox")
        x.create_directory(new)
        self.assertIn("created", x.write_file(os.path.join(new, "note.md"), "first\n"))

    def test_a_move_leaves_nothing_behind_and_names_the_new_place(self):
        dst = os.path.join(self.inside, "sub", "renamed.md")
        self.assertIn("moved", x.move_file(self.f, dst))
        self.assertFalse(os.path.exists(self.f))
        self.assertEqual(Path(dst).read_text(encoding="utf-8"), "buy milk\n")

    def test_a_move_across_drives_falls_back_to_copying(self):
        # Windows answers "Access is denied" to a rename between volumes; the tool should copy instead of giving up.
        real_rename = x.os.rename

        def cross_drive(a, b):
            raise PermissionError(5, "Access is denied")

        x.os.rename = cross_drive
        try:
            dst = os.path.join(self.inside, "sub", "renamed.md")
            self.assertIn("copied", x.move_file(self.f, dst))
            self.assertIn("another drive", x.move_file(dst, os.path.join(self.inside, "back.md")))
        finally:
            x.os.rename = real_rename
        self.assertFalse(os.path.exists(self.f))
        self.assertEqual(Path(os.path.join(self.inside, "back.md")).read_text(encoding="utf-8"), "buy milk\n")

    def test_a_move_that_cannot_copy_either_says_what_might_be_wrong(self):
        real_rename, real_move = x.os.rename, x.shutil.move
        x.os.rename = lambda a, b: (_ for _ in ()).throw(PermissionError(5, "Access is denied"))
        x.shutil.move = lambda a, b: (_ for _ in ()).throw(PermissionError(5, "Access is denied"))
        try:
            with self.assertRaises(x.FilesError) as cm:
                x.move_file(self.f, os.path.join(self.inside, "sub", "nope.md"))
            self.assertIn("open somewhere else", str(cm.exception))
        finally:
            x.os.rename, x.shutil.move = real_rename, real_move
        self.assertTrue(os.path.exists(self.f))

    def test_a_move_onto_something_that_exists_is_refused(self):
        other = os.path.join(self.inside, "other.md")
        Path(other).write_text("other\n", encoding="utf-8")
        with self.assertRaises(x.FilesError) as cm:
            x.move_file(self.f, other)
        self.assertIn("already", str(cm.exception))
        self.assertTrue(os.path.exists(self.f))            # nothing moved

    def test_a_move_into_itself_is_refused(self):
        with self.assertRaises(x.FilesError) as cm:
            x.move_file(self.inside, os.path.join(self.inside, "sub", "inner"))
        self.assertIn("inside itself", str(cm.exception))

    def test_a_move_needs_the_destination_folder_to_exist(self):
        with self.assertRaises(x.FilesError) as cm:
            x.move_file(self.f, os.path.join(self.inside, "gone", "x.md"))
        self.assertIn("create_directory", str(cm.exception))

    def test_a_move_outside_the_named_folders_is_refused(self):
        with self.assertRaises(x.FilesError) as cm:
            x.move_file(self.f, os.path.join(self.base, "elsewhere", "x.md"))
        self.assertIn("outside the folders", str(cm.exception))
        self.assertTrue(os.path.exists(self.f))

    def test_a_moved_file_must_be_read_again_before_it_is_replaced(self):
        dst = os.path.join(self.inside, "sub", "renamed.md")
        x.read_file(self.f)
        x.move_file(self.f, dst)
        with self.assertRaises(x.FilesError) as cm:
            x.write_file(dst, "guessed\n")
        self.assertIn("has not been read", str(cm.exception))


class Protocol(unittest.TestCase):
    def test_mcp_session_with_a_named_folder(self):
        base = tempfile.mkdtemp(prefix="strata-files-probe-")
        try:
            p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "strata_files_mcp.py"), base],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
            try:
                def ask(i, method, params=None):
                    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
                    p.stdin.flush()
                    return json.loads(p.stdout.readline())
                self.assertEqual(ask(1, "initialize")["result"]["serverInfo"]["name"], "strata-files")
                tools = ask(2, "tools/list")["result"]["tools"]
                self.assertEqual([t["name"] for t in tools],
                                 ["read_file", "write_file", "edit_file", "read_multiple_files", "list_directory",
                                  "directory_tree", "search_files", "get_file_info", "list_allowed_directories",
                                  "create_directory", "move_file"])
                self.assertTrue(tools[0]["annotations"]["readOnlyHint"])
                self.assertTrue(tools[1]["annotations"]["destructiveHint"])
                self.assertTrue([t for t in tools if t["name"] == "move_file"][0]["annotations"]["destructiveHint"])
                self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools
                                    if t["name"].startswith(("read_", "list_di", "list_al", "search_", "get_", "directory_"))))
                path = os.path.join(base, "a.txt")
                r = ask(3, "tools/call", {"name": "write_file", "arguments": {"path": path, "content": "one\n"}})["result"]
                self.assertFalse(r["isError"], r["content"][0]["text"])
                r = ask(4, "tools/call", {"name": "edit_file", "arguments": {"path": path, "oldText": "one",
                                                                             "newText": "two"}})["result"]
                self.assertFalse(r["isError"], r["content"][0]["text"])
                self.assertEqual(Path(path).read_text(encoding="utf-8"), "two\n")
                # a path outside the folder named on the command line: an error the model can read
                r = ask(5, "tools/call", {"name": "read_file",
                                          "arguments": {"path": os.path.join(os.path.dirname(base), "other.txt")}})["result"]
                self.assertTrue(r["isError"])
                self.assertIn("outside the folders", r["content"][0]["text"])
                self.assertTrue(ask(6, "tools/call", {"name": "nope", "arguments": {}})["result"]["isError"])
            finally:
                p.stdin.close()
                p.wait(timeout=10)
                p.stdout.close()
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)

    def test_it_refuses_to_start_without_any_folder(self):
        p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "strata_files_mcp.py")], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        out, err = p.communicate("", timeout=20)
        self.assertEqual(p.returncode, 2)
        self.assertIn("name the folders", err)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
