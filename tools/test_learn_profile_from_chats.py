import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import learn_profile_from_chats as L  # noqa: E402


def chat(n_rounds=6):
    msgs = [{"role": "user", "text": "please fix the build"}]
    for i in range(n_rounds):
        msgs.append({"role": "assistant", "text": f"looking at step {i}",
                     "tools": [{"name": "run_command", "arguments": {"command": f"make step{i}"}, "result": "ok " * 50}]})
    msgs.append({"role": "assistant", "text": "done", "tools": []})
    msgs.append({"role": "user", "text": "thanks, now the tests"})
    return {"messages": msgs}


class Turns(unittest.TestCase):
    def test_every_tool_round_becomes_a_prompt(self):
        turns = L.session_turns(chat(6))
        self.assertEqual(len(turns), 7)                                   # six tool results plus the second user message
        for t in turns:
            self.assertEqual(t[0]["role"], "user")                        # a replay prompt never starts with the assistant
            self.assertEqual(t[-1]["role"], "user")                       # and ends where the model has to answer
            self.assertGreaterEqual(len(t), 3)
        self.assertIn("[tool run_command]", " ".join(m["content"] for m in turns[-1]))

    def test_long_histories_keep_the_newest_part_and_long_results_are_cut(self):
        big = {"messages": [{"role": "user", "text": "start"}] + [
            {"role": "assistant", "text": "x" * 3000, "tools": [{"name": "t", "arguments": "a", "result": "r" * 5000}]} for _ in range(30)]}
        turns = L.session_turns(big)
        self.assertTrue(turns)
        for t in turns:
            self.assertLess(sum(len(m["content"]) for m in t), L.CHARS + 2500)
            self.assertTrue(all("[... cut ...]" in m["content"] or len(m["content"]) < L.CUT + 50 for m in t))

    def test_split_keeps_held_out_prompts_away_from_the_training_ones(self):
        turns = [[{"role": "user", "content": str(i)}] * 3 for i in range(200)]
        train, held = L.split_turns(turns, 40, 10)
        self.assertEqual(len(train), 40)
        self.assertEqual(len(held), 10)
        newest_train = max(int(t[0]["content"]) for t in train)
        self.assertLess(newest_train, 140)                                # the first 70%
        self.assertTrue(all(int(t[0]["content"]) >= 150 for t in held))   # the last 25%

    def test_a_folder_of_sessions_is_read_and_meta_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            for name, data in (("s-a.json", chat()), ("s-a.meta.json", {"messages": 3}), ("s-broken.json", None)):
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write("not json" if data is None else json.dumps(data))
            self.assertEqual(len(L.load_turns(d)), 7)

    def test_too_few_turns_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "s-a.json"), "w", encoding="utf-8") as f:
                json.dump(chat(2), f)
            self.assertEqual(L.main(["--sessions", d]), 1)

    def test_generic_prompts_come_from_the_corpus_format(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "g.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump([{"d": "code", "p": "write fizzbuzz"}, "plain string"], f)
            g = L.generic_prompts(p)
            self.assertEqual([m[0]["content"] for m in g], ["write fizzbuzz", "plain string"])
            self.assertEqual(L.generic_prompts(os.path.join(d, "missing.json")), [])


if __name__ == "__main__":
    unittest.main()
