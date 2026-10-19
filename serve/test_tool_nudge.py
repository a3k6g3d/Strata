import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from serve import server
from serve.frontend import Event, ToolCall


class FakeHub:
    settings = {"max_rounds": 1000, "approval_timeout_s": 5}
    gate = SimpleNamespace(always=set())

    def routes(self):
        return {"t": (SimpleNamespace(name="srv"), "t")}

    def kind_of(self, name):
        return "read"

    blocked = blocked_text = protected = protected_text = staticmethod(lambda *a, **k: False)

    def auto_risk(self, *a):
        return None

    def call(self, name, arguments, cancel):
        return {"ok": True, "text": "the file", "chars": 8, "truncated": False, "ms": 1}


class FakeService:
    """A model that plays a script: each round is a list of events and the finish reason."""
    def __init__(self, rounds):
        self.rounds, self.prepared = list(rounds), []

    def run(self, ids, thinking, tools, max_new, sampling, cancel):
        events, finish = self.rounds.pop(0)
        for e in events:
            yield "event", e
        yield "done", {"finish": finish, "completion_tokens": 10, "prompt_tokens": len(ids)}

    def prepare(self, messages, tools, kw, max_req, cancel=None):
        self.prepared.append([dict(m) for m in messages])
        return [1, 2, 3], False, 100


def fit_nothing(svc, messages, tools, kw, max_req, cancel):
    return 0, 0
    yield


def fit_once(svc, messages, tools, kw, max_req, cancel):
    # a real compaction, once: the next fit finds nothing more to summarize (the summary cache covers it), which is
    # what keeps one compaction from earning a second nudge
    yield "ping", None
    fit_once.calls += 1
    return (0, 1) if fit_once.calls == 1 else (0, 0)


fit_once.calls = 0

CALL = [Event("tool_call", call=ToolCall("t", {"path": "x"}))]
WORDS = [Event("content", text="All done.")]


def turn(rounds, fit=fit_nothing):
    svc = FakeService(rounds)
    text, mcp = [], []
    fit_once.calls = 0
    with mock.patch.object(server, "fit_context_live", fit):
        for kind, x in server.run_with_mcp(svc, FakeHub(), [{"role": "user", "content": "go"}], [], {}, [1], False, 100, 100,
                                           {}, threading.Event(), {"t"}, mode="full"):
            if kind == "event" and x.kind == "content":
                text.append(x.text)
            elif kind == "mcp":
                mcp.append(x)
    return svc, "".join(text), mcp


class Nudge(unittest.TestCase):
    def test_an_empty_round_after_a_tool_result_is_asked_once_to_carry_on(self):
        svc, text, _ = turn([(CALL, "stop"), ([], "stop"), (WORDS, "stop")])
        self.assertEqual(text, "All done.")
        last = svc.prepared[-1]
        self.assertEqual((last[-2], last[-1]), ({"role": "assistant", "content": ""}, {"role": "user", "content": server.NUDGE_PROMPT}))
        self.assertEqual(svc.rounds, [])                                    # all three rounds were played

    def test_only_one_nudge_per_turn(self):
        svc, text, _ = turn([(CALL, "stop"), ([], "stop"), ([], "stop")])
        self.assertEqual(text, "")                                          # the second empty round ends the turn
        self.assertEqual(sum(m.get("content") == server.NUDGE_PROMPT for p in svc.prepared[-1:] for m in p), 1)
        self.assertEqual(len(svc.prepared), 2)                              # one prepare after the tool, one after the nudge

    def test_a_reply_with_words_or_a_new_call_is_left_alone(self):
        svc, text, _ = turn([(CALL, "stop"), (WORDS, "stop")])
        self.assertEqual((text, len(svc.prepared)), ("All done.", 1))
        svc, _, _ = turn([(CALL, "stop"), (CALL, "stop"), (WORDS, "stop")])
        self.assertTrue(all(m.get("content") != server.NUDGE_PROMPT for p in svc.prepared for m in p))

    def test_no_nudge_before_any_tool_was_used(self):
        svc, text, _ = turn([([], "stop")])
        self.assertEqual((text, svc.prepared), ("", []))

    def test_no_nudge_after_a_cut_off_round(self):
        svc, _, _ = turn([(CALL, "stop"), ([], "length")])
        self.assertEqual(len(svc.prepared), 1)

    def test_needs_nudge_rules(self):
        base = dict(rounds=1, nudged=False, calls=[], own_calls=0, finish="stop", cancelled=False, text="  ")
        self.assertTrue(server.needs_nudge(**base))
        for change in ({"rounds": 0}, {"nudged": True}, {"calls": [1]}, {"own_calls": 1}, {"finish": "length"},
                       {"cancelled": True}, {"text": "hi"}):
            self.assertFalse(server.needs_nudge(**{**base, **change}), change)

    def test_needs_nudge_after_a_compaction_even_with_words(self):
        base = dict(rounds=1, nudged=False, calls=[], own_calls=0, finish="stop", cancelled=False, text="a paragraph")
        self.assertTrue(server.needs_nudge(**base, compacted=1))
        for change in ({"rounds": 0}, {"calls": [1]}, {"own_calls": 1}, {"finish": "length"}, {"cancelled": True}):
            self.assertFalse(server.needs_nudge(**{**base, "compacted": 1, **change}), change)

    def test_a_compaction_mid_turn_then_a_paragraph_is_asked_to_carry_on(self):
        # what happened overnight: tools, then the history summarized, then the summary read out as the answer
        svc, text, mcp = turn([(CALL, "stop"), (WORDS, "stop"), (WORDS, "stop")], fit=fit_once)
        self.assertEqual(len(svc.prepared), 2)                              # after the tool, after the nudge
        self.assertIn(server.NUDGE_AFTER_SUMMARY, [m.get("content") for m in svc.prepared[-1]])
        self.assertEqual(text, "All done.All done.")                       # the third round is the real answer
        self.assertEqual([e["event"] for e in mcp if e["event"] == "compact"], ["compact"])

    def test_one_compaction_earns_one_nudge(self):
        svc, text, _ = turn([(CALL, "stop"), (WORDS, "stop"), ([], "stop")], fit=fit_once)
        self.assertEqual(len(svc.prepared), 2)                              # the nudge, then the turn ends
        self.assertEqual(sum(server.NUDGE_AFTER_SUMMARY in [m.get("content") for m in p] for p in svc.prepared), 1)

    def test_no_nudge_from_a_compaction_before_any_tool_round(self):
        svc, text, _ = turn([([], "stop")], fit=fit_once)                  # nothing ran: no fit, no nudge
        self.assertEqual((text, svc.prepared), ("", []))


if __name__ == "__main__":
    unittest.main()
