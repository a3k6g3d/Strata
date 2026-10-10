"""The chat page's sessions (separate conversations), kept in files next to the run config.

One session = `<id>.json` (title, times, the messages as the page keeps them) and `<id>.meta.json` (the list entry), so
listing the sessions never reads a conversation.

Surviving a power loss or a hard reset (a PC that freezes): a write goes to a temporary file, the data is forced to the
disk (fsync) and only then does the temporary file replace the real one - without that, Windows can commit the rename
and lose the data, leaving a file of the right size that is all zeros.  The version before is kept as `<id>.json.bak`
(refreshed at most once a minute) and is read when the main file is damaged; a list entry that is damaged is rebuilt from
the conversation.  A file that cannot be read at all is renamed to `<name>.damaged`, not deleted.

The ids are made by the page (`s-<time>-<random>`); only the characters a-z 0-9 and - are accepted, so an id can never
name a path.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BYTES = 128 * 1024 * 1024         # one session; a tool-heavy chat is a few MB
TITLE_MAX = 120
BACKUP_EVERY_S = 60                   # the .bak is the version before, taken at most this often


class SessionError(ValueError):
    """A bad id, title or body (the server answers 400)."""


def atomic_write_json(path, obj, backup: bool = False) -> None:
    """Write `obj` as JSON so that a crash at any moment leaves the old file or the new one, never a mix or zeros."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())                                   # the data is on the disk before the name changes
        if backup and path.exists():
            bak = path.with_name(path.name + ".bak")
            try:
                if not bak.exists() or time.time() - bak.stat().st_mtime > BACKUP_EVERY_S:
                    try:
                        bak.unlink()
                    except FileNotFoundError:
                        pass
                    os.link(path, bak)                             # the old version keeps its bytes under its second name
            except OSError:
                pass                                               # no hard links here: no backup, the save still goes on
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path):
    """The parsed file; ValueError for anything that is not JSON (zeros, a cut-off file), OSError if it is not there."""
    text = path.read_text(encoding="utf-8")
    if not text.strip(" \t\r\n\x00"):
        raise ValueError("the file is empty or all zeros")
    return json.loads(text)


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _path(self, sid: str, meta: bool = False) -> Path:
        if not isinstance(sid, str) or not ID.match(sid):
            raise SessionError("a session id is 1-64 characters of a-z, 0-9 and -")
        return self.root / (sid + (".meta.json" if meta else ".json"))

    def _quarantine(self, path: Path) -> None:
        try:
            os.replace(path, path.with_name(path.name + ".damaged"))
        except OSError:
            pass

    def _load(self, sid: str):
        """The conversation: the main file, else its backup (and the backup then becomes the main file again).
        None if there is no such session; SessionError if it exists but nothing of it can be read."""
        main, bak = self._path(sid), self._path(sid).with_name(sid + ".json.bak")
        problem = None
        for path, is_backup in ((main, False), (bak, True)):
            try:
                data = _read_json(path)
                if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
                    raise ValueError("not a session")
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                problem = e
                if not is_backup:
                    self._quarantine(path)
                continue
            if is_backup:                                          # the main file was damaged: restore it from the backup
                try:
                    atomic_write_json(main, data)
                except OSError:
                    pass
                data["recovered_from_backup"] = True
            return data
        if problem is not None:
            raise SessionError(f"session {sid} is damaged and has no usable backup: {problem}")
        return None

    def _meta_of(self, sid: str):
        """The list entry, rebuilt from the conversation when it is damaged or missing; None if neither can be read."""
        meta_path = self._path(sid, True)
        try:
            meta = _read_json(meta_path)
            if isinstance(meta, dict) and meta.get("id") == sid:
                return meta
        except (OSError, ValueError):
            pass
        try:
            data = self._load(sid)
        except SessionError:
            data = None
        if data is None:
            return None
        meta = {"id": sid, "title": data.get("title") or "Chat", "custom_title": bool(data.get("custom_title")),
                "created": data.get("created") or time.time(),
                "updated": data.get("updated") or (self._path(sid).stat().st_mtime if self._path(sid).exists() else time.time()),
                "messages": len(data["messages"])}
        try:
            atomic_write_json(meta_path, meta)
        except OSError:
            pass
        return meta

    def list(self) -> list[dict]:
        ids = set()
        with self.lock:
            for p in self.root.glob("*.json"):
                stem = p.name[:-len(".meta.json")] if p.name.endswith(".meta.json") else p.name[:-len(".json")]
                if ID.match(stem):
                    ids.add(stem)
            out = []
            for sid in sorted(ids):
                meta = self._meta_of(sid)
                if meta is not None:
                    out.append(meta)
        return sorted(out, key=lambda m: -float(m.get("updated") or 0))

    def get(self, sid: str):
        self._path(sid)                                            # the id check
        with self.lock:
            return self._load(sid)

    def save(self, sid: str, title, messages, custom_title: bool = False) -> dict:
        path = self._path(sid)
        if not isinstance(messages, list):
            raise SessionError("messages must be a list")
        title = " ".join(str(title or "").split())[:TITLE_MAX] or "New chat"
        now = time.time()
        with self.lock:
            created = now
            try:
                created = float(_read_json(self._path(sid, True)).get("created") or now)
            except (OSError, ValueError, AttributeError):
                pass
            meta = {"id": sid, "title": title, "custom_title": bool(custom_title), "created": created, "updated": now,
                    "messages": len(messages)}
            atomic_write_json(path, {**meta, "messages": messages}, backup=True)
            atomic_write_json(self._path(sid, True), meta)
        return meta

    def delete(self, sid: str) -> bool:
        gone = False
        with self.lock:
            for p in (self._path(sid), self._path(sid, True), self._path(sid).with_name(sid + ".json.bak")):
                try:
                    p.unlink()
                    gone = True
                except FileNotFoundError:
                    pass
        return gone
