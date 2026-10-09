"""The chat page's sessions (separate conversations), kept in files next to the run config.

One session = `<id>.json` (title, times, the messages as the page keeps them) and `<id>.meta.json` (the list entry), so
listing the sessions never reads a conversation.  Writes go to a temporary file first and replace the old one in a
single step: a crash leaves the old session whole.  The ids are made by the page (`s-<time>-<random>`); only the
characters a-z 0-9 and - are accepted, so an id can never name a path.
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


class SessionError(ValueError):
    """A bad id, title or body (the server answers 400)."""


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _path(self, sid: str, meta: bool = False) -> Path:
        if not isinstance(sid, str) or not ID.match(sid):
            raise SessionError("a session id is 1-64 characters of a-z, 0-9 and -")
        return self.root / (sid + (".meta.json" if meta else ".json"))

    @staticmethod
    def _write(path: Path, obj) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def list(self) -> list[dict]:
        out = []
        with self.lock:
            for p in self.root.glob("*.meta.json"):
                try:
                    meta = json.loads(p.read_text(encoding="utf-8"))
                    if ID.match(str(meta.get("id", ""))):
                        out.append(meta)
                except (OSError, ValueError):
                    continue                                   # a half-written or foreign file is not a session
        return sorted(out, key=lambda m: -float(m.get("updated") or 0))

    def get(self, sid: str):
        path = self._path(sid)
        with self.lock:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, ValueError) as e:
                raise SessionError(f"session {sid} cannot be read: {e}") from None

    def save(self, sid: str, title, messages, custom_title: bool = False) -> dict:
        path = self._path(sid)
        if not isinstance(messages, list):
            raise SessionError("messages must be a list")
        title = " ".join(str(title or "").split())[:TITLE_MAX] or "New chat"
        now = time.time()
        with self.lock:
            created = now
            try:
                created = float(json.loads(self._path(sid, True).read_text(encoding="utf-8")).get("created") or now)
            except (OSError, ValueError):
                pass
            meta = {"id": sid, "title": title, "custom_title": bool(custom_title), "created": created, "updated": now,
                    "messages": len(messages)}
            self._write(path, {**meta, "messages": messages})
            self._write(self._path(sid, True), meta)
        return meta

    def delete(self, sid: str) -> bool:
        gone = False
        with self.lock:
            for p in (self._path(sid), self._path(sid, True)):
                try:
                    p.unlink()
                    gone = True
                except FileNotFoundError:
                    pass
        return gone
