import os
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest import mock

from serve import conversation_store as C


class FakeService:
    """The two engine calls the store uses: a save writes a file of a chosen size, a restore only has to find the file."""
    def __init__(self, size=1000):
        self.size, self.calls, self.fail = size, [], None

    def store_session_file(self, action, path):
        self.calls.append((action, os.path.basename(path)))
        if self.fail == action:
            raise RuntimeError("the engine refused")
        if action == "save":
            Path(path).write_bytes(b"x" * self.size)
        elif not os.path.isfile(path):
            raise FileNotFoundError(path)


SYSTEM = [7] * 300                                          # the part every chat starts with (a system prompt)


def conv(tag, n=1000):
    return SYSTEM + [tag * 1000 + i for i in range(n)]


def store(d, **kw):
    log = kw.pop("log_list", [])
    return C.ConversationStore(Path(d), kw.get("max_bytes", 10 ** 9), kw.get("min_tokens", 500), kw.get("grow_tokens", 400), log=log.append), log


class Settings(unittest.TestCase):
    def test_off_unless_enabled_and_values_are_checked(self):
        for cfg in (None, {}, {"enabled": False}, {"enabled": "yes"}, "on"):
            self.assertIsNone(C.settings(cfg))
        s = C.settings({"enabled": True}, Path("cfgdir"))
        self.assertEqual((s["folder"], s["max_bytes"], s["min_tokens"], s["grow_tokens"]), (Path("cfgdir") / "conversations", 20 * 2 ** 30, 4000, 6000))
        s = C.settings({"enabled": True, "folder": "elsewhere", "max_gib": 0, "min_tokens": "x", "grow_tokens": 50}, Path("cfgdir"))
        self.assertEqual((s["folder"], s["max_bytes"], s["min_tokens"], s["grow_tokens"]), (Path("cfgdir") / "elsewhere", 20 * 2 ** 30, 4000, 6000))

    def test_common_prefix(self):
        self.assertEqual(C.common_prefix([1, 2, 3], [1, 2, 9]), 2)
        self.assertEqual(C.common_prefix([], [1]), 0)
        self.assertEqual(C.common_prefix(array("i", [5, 6]), [5, 6, 7]), 2)


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = FakeService()
        self.s, self.log = store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def finish(self, ids, store_=None):
        (store_ or self.s).before_request(self.svc, ids)
        (store_ or self.s).after_request(self.svc, ids, "stop")

    def saved(self):
        return [c for c in self.svc.calls if c[0] == "save"]

    def test_a_short_conversation_is_never_saved(self):
        self.finish(SYSTEM + list(range(100)))
        self.assertEqual(self.svc.calls, [])

    def test_a_long_one_is_saved_after_its_first_request_and_again_only_when_it_has_grown(self):
        a = conv(1, 800)
        self.finish(a)
        self.assertEqual(len(self.saved()), 1)
        self.finish(a + list(range(50000, 50100)))                              # +100 tokens: not enough
        self.assertEqual(len(self.saved()), 1)
        self.finish(a + list(range(50000, 50500)))                              # +500 since the save: saved again
        self.assertEqual(len(self.saved()), 2)
        self.assertEqual(len(self.s.index), 1)                                  # the newer state replaced the older one
        self.assertEqual(len([f for f in os.listdir(self.tmp.name) if f.endswith(".bin")]), 1)

    def test_going_back_to_a_conversation_restores_it_and_a_continuation_does_not(self):
        a, b = conv(1, 900), conv(2, 900)
        self.finish(a)
        self.finish(b)                                                          # a chat switch
        self.assertEqual(len(self.s.index), 2)
        self.svc.calls.clear()
        self.s.before_request(self.svc, a + list(range(60000, 60030)))          # back to the first one, with a new turn
        self.assertEqual([c[0] for c in self.svc.calls], ["restore"])
        self.assertEqual(self.svc.calls[0][1], self.s.index[0]["name"])
        self.s.after_request(self.svc, a + list(range(60000, 60030)), "stop")
        self.svc.calls.clear()
        self.s.before_request(self.svc, a + list(range(60000, 60060)))          # the same conversation again: the engine has it
        self.assertEqual(self.svc.calls, [])

    def test_a_chat_that_only_shares_the_system_prompt_restores_nothing(self):
        self.finish(conv(1, 900))
        self.svc.calls.clear()
        self.s.before_request(self.svc, conv(3, 900))
        self.assertEqual([c[0] for c in self.svc.calls], [])                    # the live conversation was already saved, none matches

    def test_the_live_conversation_is_saved_on_a_switch_if_it_grew_unsaved(self):
        a = conv(1, 900)
        self.finish(a)
        self.s.after_request(self.svc, a + list(range(70000, 70100)), "stop")   # grew by 100: not saved yet
        self.assertEqual(len(self.saved()), 1)
        self.s.before_request(self.svc, conv(2, 900))                           # the switch keeps what the engine held
        self.assertEqual(len(self.saved()), 2)

    def test_a_short_live_conversation_that_shares_the_system_prompt_does_not_block_a_restore(self):
        a = conv(1, 900)
        self.finish(a)
        fresh, _ = store(self.tmp.name)                                         # a restart, then the warm-up requests (short, same system prompt)
        warm = SYSTEM + list(range(100))
        self.finish(warm, fresh)
        self.svc.calls.clear()
        fresh.before_request(self.svc, a + list(range(60000, 60030)))
        self.assertEqual([c[0] for c in self.svc.calls], ["restore"])

    def test_the_live_conversation_wins_a_tie_so_nothing_is_restored_over_it(self):
        a = conv(1, 900)
        self.finish(a)                                                          # saved, and still what the engine holds
        self.svc.calls.clear()
        self.s.before_request(self.svc, a + list(range(60000, 60030)))
        self.assertEqual(self.svc.calls, [])

    def test_the_index_survives_a_restart(self):
        a = conv(1, 900)
        self.finish(a)
        again, _ = store(self.tmp.name)
        self.assertEqual(len(again.index), 1)
        self.svc.calls.clear()
        again.before_request(self.svc, a + list(range(60000, 60030)))
        self.assertEqual([c[0] for c in self.svc.calls], ["restore"])

    def test_the_oldest_go_first_when_the_folder_is_over_its_budget(self):
        small, _ = store(self.tmp.name, max_bytes=2500)
        for tag in (1, 2, 3):
            self.finish(conv(tag, 900), small)
        self.assertEqual(len(small.index), 2)
        names = [r["name"] for r in small.index]
        self.assertEqual(sorted(os.listdir(self.tmp.name)), sorted(["index.json"] + names + [n + ".ids" for n in names]))

    def test_failures_never_reach_the_request(self):
        a = conv(1, 900)
        self.svc.fail = "save"
        self.finish(a)                                                          # no exception
        self.assertEqual(self.s.index, [])
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.startswith("conv-")], [])
        self.assertEqual(len(self.log), 1)                                      # said once
        self.svc.fail = None
        self.finish(a + [1] * 500)
        self.svc.fail = "restore"
        self.s.before_request(self.svc, conv(2, 900))
        self.s.before_request(self.svc, a + [1] * 520)                           # a restore that fails: carries on
        self.assertIsNone(self.s.live)

    def test_cancelled_and_picture_requests_are_not_saved(self):
        self.s.before_request(self.svc, conv(1, 900))
        self.s.after_request(self.svc, conv(1, 900), "cancel")
        self.s.after_request(self.svc, conv(1, 900), "stop", pictures=True)
        self.assertEqual(self.svc.calls, [])

    def test_nothing_is_saved_when_the_disk_is_nearly_full(self):
        with mock.patch.object(C.shutil, "disk_usage", return_value=mock.Mock(free=10 * 2 ** 20)):
            self.finish(conv(1, 900))
        self.assertEqual(self.svc.calls, [])


if __name__ == "__main__":
    unittest.main()
