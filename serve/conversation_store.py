"""Opt-in: keep the conversations the engine has read on the disk, so a chat switch or a restart does not mean reading them again.

Measured on one 12 GB PC (a conversation of 25K tokens): reading it takes 34 s; saving the engine's state of it took 0.7 s and a 585 MB file
(about 270 MB plus 14 KB per token); restoring it 0.6 s, after which the next request read only its 26 new tokens (0.8 s).  The engine already
has SAVE / RESTORE (the `--slot-save-path` slot calls); this puts them to work by itself:

  * the live conversation is saved when a request for a different conversation arrives (a chat switch), and again once it has grown by
    `grow_tokens` since the last save (so a restart or a crash costs at most the newest few thousand tokens);
  * a request whose prompt starts with a saved conversation's prompt gets that state restored before it is read.

    "conversation_store": {"enabled": true, "folder": "conversations", "max_gib": 20, "min_tokens": 4000, "grow_tokens": 6000}

A conversation is found by the prompt tokens it was saved with (stored next to the state), not by a name, so any client's chats work.  Only one
file is kept per conversation (a newer save replaces the older state it extends); when the folder passes `max_gib` the oldest go first; nothing is
saved when the disk has under 5 GiB free, for a request with pictures, or for one that was cancelled.  Every call happens with the request's turn
already held, and a failure of any kind is logged once and the request carries on as if the store were off.  Off unless "enabled" is true.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from array import array
from pathlib import Path

MIN_FREE_BYTES = 5 * 2 ** 30
CONTINUES = 0.5           # a prompt that shares this much of the live conversation's prompt is the same conversation
RESTORE_IF = 0.9          # a saved conversation is restored when this much of its prompt is at the start of the new prompt


def common_prefix(a, b) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def settings(cfg, config_dir: Path | None = None) -> dict | None:
    """The validated block, or None when it is off or malformed."""
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return None

    def num(key, default, lo, hi):
        v = cfg.get(key)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi else default
    folder = Path(str(cfg.get("folder") or "conversations"))
    if not folder.is_absolute():
        folder = (config_dir or Path(".")) / folder
    return {"folder": folder, "max_bytes": int(num("max_gib", 20, 1, 2000) * 2 ** 30), "min_tokens": int(num("min_tokens", 4000, 256, 10 ** 6)),
            "grow_tokens": int(num("grow_tokens", 6000, 500, 10 ** 6))}


class ConversationStore:
    def __init__(self, folder: Path, max_bytes: int, min_tokens: int, grow_tokens: int, log=print):
        self.folder, self.max_bytes, self.min_tokens, self.grow_tokens, self.log = Path(folder), max_bytes, min_tokens, grow_tokens, log
        self.live: array | None = None          # the prompt of the conversation the engine holds
        self.saved_n = 0                        # its length when it was last saved (0: never)
        self.failed = False                     # one log line per kind of failure is enough
        self.folder.mkdir(parents=True, exist_ok=True)
        self.index = self._load_index()

    # ---- the index: one entry per saved conversation, kept in a small JSON file beside the states
    def _index_path(self):
        return self.folder / "index.json"

    def _load_index(self) -> list:
        try:
            rows = json.loads(self._index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [r for r in rows if isinstance(r, dict) and (self.folder / str(r.get("name"))).is_file() and (self.folder / (str(r["name"]) + ".ids")).is_file()]

    def _save_index(self):
        tmp = self._index_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index), encoding="utf-8")
        os.replace(tmp, self._index_path())

    def _ids_of(self, row) -> array | None:
        try:
            a = array("i")
            a.frombytes((self.folder / (row["name"] + ".ids")).read_bytes())
            return a
        except (OSError, ValueError):
            return None

    def _drop(self, row):
        for suffix in ("", ".ids"):
            try:
                (self.folder / (row["name"] + suffix)).unlink()
            except OSError:
                pass
        if row in self.index:
            self.index.remove(row)

    def _warn(self, what: str, e: Exception):
        if not self.failed:
            self.failed = True
            self.log(f"[strata] conversation store: {what}: {type(e).__name__}: {str(e)[:160]} (the request carries on without it)")

    # ---- the two moments
    def before_request(self, svc, ids) -> None:
        """A request is about to be read (the turn is held, the engine is loaded): bring back the saved conversation this prompt continues, if
        that matches better than what the engine already holds, and keep the live conversation when it is being left."""
        try:
            ids = array("i", ids)
            n_live = common_prefix(self.live, ids) if self.live is not None else 0
            live_ok = self.live is not None and n_live >= CONTINUES * len(self.live)      # the engine holds this conversation
            best, best_len, closest = None, 0, (0, 0)
            for row in self.index:
                saved = self._ids_of(row)
                if saved is None:
                    continue
                n = common_prefix(saved, ids)
                closest = max(closest, (n, len(saved)))
                if n >= RESTORE_IF * len(saved) and n >= self.min_tokens and n > best_len:
                    best, best_len = row, n
            if best is not None and not (live_ok and best_len <= n_live):
                if self.live is not None:
                    self._save_live(svc)                                  # leaving this one: keep what the engine holds
                t = time.perf_counter()
                svc.store_session_file("restore", str(self.folder / best["name"]))
                self.live, self.saved_n = self._ids_of(best), best["n"]
                self.log(f"[strata] conversation store: restored a conversation of {best['n']:,} tokens in {time.perf_counter() - t:.1f} s")
                return
            if live_ok:
                return                                                   # the same conversation: the engine reuses its own state
            if self.live is not None:
                self._save_live(svc)
            self.live, self.saved_n = None, 0
            if self.index and len(ids) >= self.min_tokens:
                self.log(f"[strata] conversation store: no saved conversation matches this {len(ids):,}-token prompt "
                         f"(closest: {closest[0]:,} of {closest[1]:,} tokens match at the start)")
        except Exception as e:  # noqa: BLE001
            self.live, self.saved_n = None, 0
            self._warn("restore", e)

    def after_request(self, svc, ids, finish: str, pictures: bool = False) -> None:
        """A request has finished and the turn is still held: remember what the engine holds, and save it if it has grown."""
        try:
            if finish in ("cancel", "disconnect") or pictures:
                self.live = None if pictures else self.live
                return
            self.live = array("i", ids)
            if len(self.live) >= self.min_tokens and (self.saved_n == 0 or len(self.live) - self.saved_n >= self.grow_tokens):
                self._save_live(svc)
        except Exception as e:  # noqa: BLE001
            self._warn("save", e)

    # ---- saving
    def _save_live(self, svc) -> None:
        live = self.live
        if live is None or len(live) < self.min_tokens or len(live) <= self.saved_n:
            return
        if shutil.disk_usage(self.folder).free < MIN_FREE_BYTES:
            return
        name = f"conv-{int(time.time() * 1000):x}.bin"
        t = time.perf_counter()
        try:
            svc.store_session_file("save", str(self.folder / name))
            (self.folder / (name + ".ids")).write_bytes(live.tobytes())
        except Exception as e:  # noqa: BLE001
            for suffix in ("", ".ids"):
                try:
                    (self.folder / (name + suffix)).unlink()
                except OSError:
                    pass
            self._warn("save", e)
            return
        # the newer state replaces the older ones it extends (the same conversation, shorter)
        for row in list(self.index):
            old = self._ids_of(row)
            if old is None or common_prefix(old, live) >= RESTORE_IF * len(old):
                self._drop(row)
        row = {"name": name, "n": len(live), "saved_at": time.time(), "bytes": (self.folder / name).stat().st_size}
        self.index.append(row)
        self.saved_n = len(live)
        self._evict()
        self._save_index()
        self.log(f"[strata] conversation store: saved a conversation of {len(live):,} tokens ({row['bytes'] / 2 ** 20:.0f} MB) in "
                 f"{time.perf_counter() - t:.1f} s")

    def _evict(self):
        total = sum(r.get("bytes", 0) for r in self.index)
        for row in sorted(self.index, key=lambda r: r.get("saved_at", 0)):
            if total <= self.max_bytes or len(self.index) <= 1:
                break
            total -= row.get("bytes", 0)
            self._drop(row)


def create(cfg, config_dir: Path | None, log=print) -> ConversationStore | None:
    s = settings(cfg, config_dir)
    if s is None:
        return None
    try:
        store = ConversationStore(s["folder"], s["max_bytes"], s["min_tokens"], s["grow_tokens"], log)
    except OSError as e:
        log(f"[strata] conversation store: not started - {e}")
        return None
    log(f"[strata] conversation store: on ({s['folder']}, up to {s['max_bytes'] / 2 ** 30:.0f} GiB, conversations of {s['min_tokens']:,}+ tokens; "
        f"{len(store.index)} saved)")
    return store
