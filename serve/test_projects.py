import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from serve import server
from serve.frontend import Event, ToolCall
from serve.projects import ProjectError, ProjectStore, folder_note, with_folder

# Piece C of docs/WEB_UI_REDESIGN.md: projects (a name, a working folder, chats).  The store, the filling-in of the folder
# into tool arguments, and that the tool loop fills it in before the permission checks look at a call.

ROOT = "C:\\work\\site" if os.name == "nt" else "/work/site"


class WithFolder(unittest.TestCase):
    def test_a_command_without_a_cwd_runs_in_the_folder(self):
        self.assertEqual(with_folder("run_command", {"command": "dir"}, ROOT), {"command": "dir", "cwd": ROOT})
        self.assertEqual(with_folder("run_python", {"code": "1", "cwd": ""}, ROOT)["cwd"], ROOT)

    def test_a_relative_cwd_or_path_is_taken_from_the_folder(self):
        self.assertEqual(with_folder("run_command", {"command": "x", "cwd": "src"}, ROOT)["cwd"], os.path.join(ROOT, "src"))
        self.assertEqual(with_folder("write_file", {"path": "index.html", "content": ""}, ROOT)["path"],
                         os.path.join(ROOT, "index.html"))
        self.assertEqual(with_folder("list_directory", {"path": "."}, ROOT)["path"], ROOT)

    def test_an_absolute_path_is_left_as_the_model_wrote_it(self):
        other = "D:\\elsewhere\\a.txt" if os.name == "nt" else "/elsewhere/a.txt"
        self.assertEqual(with_folder("read_file", {"path": other}, ROOT)["path"], other)
        self.assertEqual(with_folder("run_command", {"command": "x", "cwd": other}, ROOT)["cwd"], other)

    def test_several_paths_and_a_move(self):
        a = with_folder("read_multiple_files", {"paths": ["a.txt", "b.txt"]}, ROOT)
        self.assertEqual(a["paths"], [os.path.join(ROOT, "a.txt"), os.path.join(ROOT, "b.txt")])
        m = with_folder("move_file", {"source": "a", "destination": "b"}, ROOT)
        self.assertEqual((m["source"], m["destination"]), (os.path.join(ROOT, "a"), os.path.join(ROOT, "b")))

    def test_other_tools_and_no_folder_are_untouched(self):
        args = {"query": "weather", "path": "x"}
        self.assertIs(with_folder("web_search", args, ROOT), args)
        self.assertIs(with_folder("write_file", args, None), args)

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_a_path_without_its_drive_is_relative_on_windows(self):
        # "\work" and "C:work" depend on the server's own drive and folder: neither is a place the model can mean
        self.assertEqual(with_folder("read_file", {"path": "\\x.txt"}, ROOT)["path"], os.path.normpath(ROOT + "\\x.txt"))


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.folder = self.dir / "site"
        self.folder.mkdir()
        self.store = ProjectStore(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_make_assign_and_find(self):
        p = self.store.save(None, "  Pagoda   garden ", str(self.folder))
        self.assertEqual((p["name"], p["folder"]), ("Pagoda garden", str(self.folder)))
        self.store.assign("s-1", p["id"])
        self.assertEqual(self.store.project_of("s-1"), p)
        self.assertIsNone(self.store.project_of("s-2"))
        self.assertEqual(ProjectStore(self.dir).list()["chats"], {"s-1": p["id"]})       # kept on disk

    def test_rename_and_take_a_chat_out(self):
        p = self.store.save(None, "a", str(self.folder))
        self.store.save(p["id"], "b", str(self.folder))
        self.assertEqual(self.store.get(p["id"])["name"], "b")
        self.store.assign("s-1", p["id"])
        self.store.assign("s-1", None)
        self.assertEqual(self.store.list()["chats"], {})

    def test_removing_a_project_leaves_its_chats_outside_any(self):
        p = self.store.save(None, "a", str(self.folder))
        self.store.assign("s-1", p["id"])
        self.assertTrue(self.store.delete(p["id"]))
        self.assertEqual(self.store.list(), {"projects": [], "chats": {}})
        self.assertTrue(self.folder.is_dir())                                         # nothing on disk is touched

    def test_refusals(self):
        with self.assertRaises(ProjectError):
            self.store.save(None, "", str(self.folder))
        with self.assertRaises(ProjectError):
            self.store.save(None, "a", str(self.dir / "missing"))
        with self.assertRaises(ProjectError):
            self.store.save(None, "a", "relative\\folder")
        with self.assertRaises(ProjectError):
            self.store.assign("s-1", "p-00000000")
        with self.assertRaises(ProjectError):
            self.store.assign("../x", None)

    def test_a_damaged_file_reads_as_no_projects(self):
        (self.dir / "_projects.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.list(), {"projects": [], "chats": {}})

    def test_the_chat_store_beside_it_neither_lists_nor_quarantines_it(self):
        # found in the browser: a file named like a chat id was read as a damaged chat and renamed .damaged
        from serve.sessions import SessionStore
        p = self.store.save(None, "a", str(self.folder))
        self.assertEqual(SessionStore(self.dir).list(), [])
        self.assertEqual(self.store.get(p["id"])["name"], "a")

    def test_the_note_names_the_project_and_its_folder(self):
        n = folder_note("Pagoda", ROOT)
        self.assertIn('"Pagoda"', n)
        self.assertIn(ROOT, n)


class Hub:
    settings = {"max_rounds": 10, "approval_timeout_s": 5}
    gate = SimpleNamespace(always=set())

    def __init__(self):
        self.seen, self.checked = [], []

    def routes(self):
        return {"exec__run_command": (SimpleNamespace(name="exec"), "run_command")}

    def kind_of(self, name):
        return "read"

    def blocked(self, arguments):
        self.checked.append(dict(arguments))
        return False

    blocked_text = protected = protected_text = staticmethod(lambda *a, **k: False)

    def auto_risk(self, *a):
        return None

    def call(self, name, arguments, cancel):
        self.seen.append(dict(arguments))
        return {"ok": True, "text": "done", "chars": 4, "truncated": False, "ms": 1}


class Svc:
    def __init__(self):
        self.rounds = [([Event("tool_call", call=ToolCall("exec__run_command", {"command": "dir"}))], "stop"),
                       ([Event("content", text="ok")], "stop")]

    def run(self, ids, thinking, tools, max_new, sampling, cancel):
        events, finish = self.rounds.pop(0)
        for e in events:
            yield "event", e
        yield "done", {"finish": finish, "completion_tokens": 1, "prompt_tokens": 1}

    def prepare(self, messages, tools, kw, max_req, cancel=None):
        return [1], False, 10


def fit_nothing(svc, messages, tools, kw, max_req, cancel):
    return 0, 0
    yield


class ToolLoop(unittest.TestCase):
    def run_turn(self, folder):
        hub, events = Hub(), []
        with mock.patch.object(server, "fit_context_live", fit_nothing):
            for kind, x in server.run_with_mcp(Svc(), hub, [{"role": "user", "content": "go"}], [], {}, [1], False, 10, 10,
                                               {}, threading.Event(), {"exec__run_command"}, "full", folder):
                if kind == "mcp" and x.get("event") == "call":
                    events.append(x)
        return hub, events

    def test_the_folder_is_filled_in_before_the_checks_and_shown_in_the_call(self):
        hub, events = self.run_turn(ROOT)
        self.assertEqual(hub.checked, [{"command": "dir", "cwd": ROOT}])      # the no-read list saw the real folder
        self.assertEqual(hub.seen, [{"command": "dir", "cwd": ROOT}])
        self.assertEqual(events[0]["arguments"], {"command": "dir", "cwd": ROOT})   # and so does the page's call card

    def test_without_a_project_nothing_is_added(self):
        hub, _ = self.run_turn(None)
        self.assertEqual(hub.seen, [{"command": "dir"}])


if __name__ == "__main__":
    unittest.main()
