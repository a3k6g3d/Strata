import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_record as R  # noqa: E402


def results(decodes=(24.0, 26.0, 25.0)):
    turns = [{"ok": True, "turn": i + 1, "decode_tps": d, "ttft_s": 1.0 + i} for i, d in enumerate(decodes)] + [{"ok": False, "turn": 9}]
    return {"params": {"arm": "kv-resident 24576", "engine_arg": ["--kv-resident", "24576"]},
            "machine": {"launch": "strata --kv-resident 24576", "engine_version": "0.1.43", "strata_commit": "abc123"},
            "elapsed_s": 600,
            "state": {"start": {"uptime_s": 1234.5, "gpu": {"sm_mhz": 2500}}, "end": {"uptime_s": 1834.5}, "during": {"samples": 60}},
            "results": {"agent": {"sessions": [turns]},
                        "engine": {"per_test": {"agent session": {"decode_tok_s_median": 27.1, "cache_hit_pct": 48.2, "pcie_share_pct": 6.1,
                                                                   "prompt_reuse_pct": 91.0, "draft_accept_pct": 70.5}},
                                   "whole_suite": {"cache_hit_pct": 50.0}}}}


class Records(unittest.TestCase):
    def test_a_run_becomes_one_flat_record(self):
        r = R.make_record(results(), folder="2026-10-15-x", now=1_800_000_600.0)
        self.assertEqual((r["arm"], r["engine_arg"], r["launch"]), ("kv-resident 24576", ["--kv-resident", "24576"], "strata --kv-resident 24576"))
        self.assertEqual((r["decode_tps_median"], r["ttft_s_median"]), (25.0, 2.0))                   # the failed turn is not counted
        self.assertEqual((r["engine_decode_tok_s"], r["hit_pct"], r["pcie_share_pct"], r["draft_accept_pct"]), (27.1, 48.2, 6.1, 70.5))
        self.assertEqual(r["start_epoch"], 1_800_000_000.0)                                           # the end minus the run's length
        self.assertEqual(r["uptime_s_at_start"], 1234.5)
        self.assertEqual(r["state"]["during"], {"samples": 60})
        self.assertEqual([t["turn"] for t in r["turns"]], [1, 2, 3])
        self.assertEqual(r["folder"], "2026-10-15-x")
        json.dumps(r)                                                                                    # plain JSON all the way down

    def test_a_run_without_an_agent_session_falls_back_to_the_chat_figures(self):
        res = results()
        res["results"]["agent"] = {}
        res["results"]["chat"] = {"decode_tps": {"median": 31.9}, "ttft_s": {"median": 1.656}}
        r = R.make_record(res, now=1_800_000_600.0)
        self.assertEqual((r["decode_tps_median"], r["ttft_s_median"], r["decode_source"]), (31.9, 1.656, "chat"))
        self.assertEqual(R.make_record(results(), now=1_800_000_600.0)["decode_source"], "agent")

    def test_a_run_with_nothing_measured_still_makes_a_record(self):
        r = R.make_record({}, now=1_800_000_000.0)
        self.assertIsNone(r["decode_tps_median"])
        self.assertEqual((r["arm"], r["turns"], r["state"]["start"]), ("", [], None))

    def test_state_hash_lines_are_counted_and_digested(self):
        lines = ["x", "STATE_HASH L=12 gdn=ab12 ple=cd34 tail=ef56 pooled=1,2", "STATE_HASH L=13 gdn=ab13 ple=cd35 tail=ef57 pooled=3"]
        h = R.state_hash_summary(lines)
        self.assertEqual(h["lines"], 2)
        self.assertEqual(h["digest"], R.state_hash_summary(lines)["digest"])                           # the same lines, the same digest
        self.assertNotEqual(h["digest"], R.state_hash_summary(lines[:2])["digest"])
        self.assertIsNone(R.state_hash_summary(["nothing"]))
        self.assertIsNone(R.state_hash_summary(None))

    def test_append_and_read_skip_bad_lines_and_keep_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "runs.jsonl")
            R.append(p, {"arm": "a", "n": 1})
            with open(p, "a", encoding="utf-8") as f:
                f.write("this is not json\n\n[1, 2]\n")
            R.append(p, {"arm": "b", "n": 2})
            self.assertEqual([r["arm"] for r in R.read_records(p)], ["a", "b"])
            self.assertEqual(R.read_records(os.path.join(d, "missing.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
