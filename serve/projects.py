"""Projects: a name, a working folder, and the chats under it (docs/WEB_UI_REDESIGN.md, piece C).

Kept in one file beside the chats (<sessions folder>/_projects.json) and not in the chats themselves: the Discord bot and
older pages save chats too, and would drop a field they do not know.  A chat belongs to at most one project.

For a chat in a project the server (run_with_mcp) gives the tools the project's folder: a command runs there unless the
model names another `cwd`, and a relative path to the files tools is taken from it (with_folder below).  The tools' own
rules are unchanged - the files server still refuses a folder outside its allowed ones, with its own message, and the
no-read list and protected paths are checked on the arguments as filled in.
"""
import os
import re
import secrets
import threading
from pathlib import Path

from serve.sessions import ID, atomic_write_json, _read_json

FILE = "_projects.json"     # not a chat: the "_" keeps it out of the chat store's ids (it would quarantine it as damaged)
NAME_MAX = 80
PID = re.compile(r"^p-[0-9a-f]{8}$")
EXEC_TOOLS = {"run_command", "run_python"}                 # tools/strata_exec_mcp.py: each takes a `cwd`
PATH_TOOLS = {"read_file", "write_file", "edit_file", "list_directory", "directory_tree", "search_files", "get_file_info",
              "create_directory"}                          # tools/strata_files_mcp.py: each takes a `path`


class ProjectError(ValueError):
    pass


def _absolute(p: str) -> bool:
    # on Windows "\work" and "C:work" are not absolute: both depend on the server's current drive or folder
    return os.path.isabs(p) and (os.name != "nt" or bool(os.path.splitdrive(p)[0]))


def _join(folder: str, p):
    if not isinstance(p, str):
        return p
    if p.strip() in ("", "."):
        return folder
    if _absolute(p) or p.startswith(("\\\\", "//")):
        return p
    # "\x.txt" and "C:x.txt" would join to the drive's root or its current folder: under the project's folder they go
    return os.path.normpath(os.path.join(folder, os.path.splitdrive(p)[1].lstrip("\\/")))


def with_folder(tool: str, arguments, folder: str):
    """The call's arguments with the project's folder filled in: the `cwd` of a command when it names none (or a relative
    one), and a relative `path` (or `paths`, `source`, `destination`) of a files tool.  Anything else is returned as is."""
    if not folder or not isinstance(arguments, dict):
        return arguments
    a = dict(arguments)
    if tool in EXEC_TOOLS:
        a["cwd"] = _join(folder, a.get("cwd") or "")
    elif tool in PATH_TOOLS and "path" in a:
        a["path"] = _join(folder, a["path"])
    elif tool == "read_multiple_files" and isinstance(a.get("paths"), list):
        a["paths"] = [_join(folder, p) for p in a["paths"]]
    elif tool == "move_file":
        for k in ("source", "destination"):
            if k in a:
                a[k] = _join(folder, a[k])
    else:
        return arguments
    return a


def folder_note(name: str, folder: str) -> str:
    """What the model is told for a chat in a project (added to the system message, so it stays the same every turn and
    the conversation cache keeps reusing the prompt)."""
    return (f"This chat belongs to the project \"{name}\", whose working folder is {folder}. Commands run in that folder "
            f"unless you give another cwd, and a relative path to the file tools is taken from it. Keep the project's "
            f"files in it.")


class ProjectStore:
    def __init__(self, root):
        self.file = Path(root) / FILE
        self.lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = _read_json(self.file)
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            data = {}
        projects = [p for p in data.get("projects") or [] if isinstance(p, dict) and PID.match(str(p.get("id")))]
        known = {p["id"] for p in projects}
        chats = {s: p for s, p in (data.get("chats") or {}).items() if isinstance(s, str) and ID.match(s) and p in known}
        return {"projects": projects, "chats": chats}

    def list(self) -> dict:
        with self.lock:
            return self._load()

    def get(self, pid):
        if not isinstance(pid, str) or not PID.match(pid):
            return None
        return next((p for p in self.list()["projects"] if p["id"] == pid), None)

    def project_of(self, sid):
        with self.lock:
            data = self._load()
        pid = data["chats"].get(sid) if isinstance(sid, str) else None
        return next((p for p in data["projects"] if p["id"] == pid), None)

    @staticmethod
    def check_folder(folder) -> str:
        if not isinstance(folder, str) or not folder.strip() or len(folder) > 400:
            raise ProjectError("a project needs a folder")
        folder = folder.strip().strip('"')
        if not _absolute(folder):
            raise ProjectError("the folder must be a full path, with its drive (for example D:\\work\\my-site)")
        folder = os.path.normpath(folder)
        if not os.path.isdir(folder):
            raise ProjectError(f"there is no folder {folder}")
        return folder

    def save(self, pid, name, folder) -> dict:
        """Create a project (pid None) or rename it / change its folder."""
        name = " ".join(str(name or "").split())[:NAME_MAX]
        if not name:
            raise ProjectError("a project needs a name")
        folder = self.check_folder(folder)
        with self.lock:
            data = self._load()
            if pid is None:
                project = {"id": "p-" + secrets.token_hex(4), "name": name, "folder": folder}
                data["projects"].append(project)
            else:
                project = next((p for p in data["projects"] if p["id"] == pid), None)
                if project is None:
                    raise ProjectError("no such project")
                project.update(name=name, folder=folder)
            atomic_write_json(self.file, data)
        return project

    def delete(self, pid) -> bool:
        """The project goes; its chats stay, outside any project.  Nothing in its folder is touched."""
        with self.lock:
            data = self._load()
            before = len(data["projects"])
            data["projects"] = [p for p in data["projects"] if p["id"] != pid]
            data["chats"] = {s: p for s, p in data["chats"].items() if p != pid}
            atomic_write_json(self.file, data)
        return len(data["projects"]) < before

    def assign(self, sid, pid) -> None:
        """Put a chat into a project, or (pid None) take it out."""
        if not isinstance(sid, str) or not ID.match(sid):
            raise ProjectError("a session id is 1-64 characters of a-z, 0-9 and -")
        with self.lock:
            data = self._load()
            if pid is None:
                data["chats"].pop(sid, None)
            elif any(p["id"] == pid for p in data["projects"]):
                data["chats"][sid] = pid
            else:
                raise ProjectError("no such project")
            atomic_write_json(self.file, data)
