"""The fingerprint harness's own logic: hashing, the stable/unstable call, the parity gate.

Offline: fabricated answers, no server, no GPU.

    python -m pytest tools/test_output_fingerprint.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import output_fingerprint as F  # noqa: E402

A, B, C = "The capital of France is Paris.", "The capital of France is Paris!", "Paris is the capital of France."


def runs(hashes, errs=(), cold_first=True):
    return [{"run": i, "cold": cold_first and i == 0, "hash": F.fingerprint(h), "err": None}
            for i, h in enumerate(hashes)] + [{"run": "x", "cold": False, "hash": F.fingerprint(""), "err": e}
                                              for e in errs]


class FingerprintTests(unittest.TestCase):
    def test_stable_and_sensitive(self):
        self.assertEqual(F.fingerprint(A), F.fingerprint(A))
        self.assertNotEqual(F.fingerprint(A), F.fingerprint(B))            # one character
        self.assertEqual(len(F.fingerprint("")), 64)                       # an empty answer is still an answer


class ClassifyTests(unittest.TestCase):
    def test_all_agreeing_is_stable(self):
        self.assertEqual(F.classify(runs([A, A, A]))["verdict"], F.STABLE)

    def test_a_cold_run_may_differ_when_the_warm_ones_agree(self):
        v = F.classify(runs([A, B, B, B]))
        self.assertEqual(v["verdict"], F.WARM_ONLY)
        self.assertEqual(v["hash"], F.fingerprint(B))

    def test_warm_runs_disagreeing_is_unstable(self):
        self.assertEqual(F.classify(runs([A, A, B, A]))["verdict"], F.UNSTABLE)

    def test_one_cold_and_one_warm_is_nothing_to_conclude(self):
        self.assertEqual(F.classify(runs([A, B]))["verdict"], F.UNSTABLE)

    def test_the_majority_hash_is_reported_not_hidden(self):
        self.assertEqual(F.classify(runs([A, B, B]))["hash"], F.fingerprint(B))

    def test_an_errored_run_does_not_vote_and_is_named(self):
        v = F.classify(runs([A, A], errs=["boom"]))
        self.assertEqual((v["verdict"], v["errors"], v["runs"]), (F.STABLE, 1, 3))

    def test_all_runs_errored_is_no_data_not_a_match(self):
        v = F.classify(runs([], errs=["boom"]))
        self.assertIsNone(v["verdict"])
        self.assertEqual(v["errors"], 1)


class CompareTests(unittest.TestCase):
    def verdict(self, text, stable=True):
        return {"verdict": F.STABLE if stable else F.UNSTABLE, "hash": F.fingerprint(text),
                "errors": 0, "runs": 3, "distinct": 1 if stable else 2}

    def rows(self):
        return {"agrees": (self.verdict(A), self.verdict(A)),
                "differs": (self.verdict(A), self.verdict(B)),
                "shaky": (self.verdict(A, stable=False), self.verdict(A))}

    def test_the_gate_is_not_vacuous(self):
        self.assertEqual({r["prompt"]: r["verdict"] for r in F.compare(self.rows())},
                         {"agrees": "match", "differs": "mismatch", "shaky": "not comparable"})

    def test_an_unstable_prompt_is_not_compared_even_when_the_hashes_agree(self):
        row = next(r for r in F.compare(self.rows()) if r["prompt"] == "shaky")
        self.assertEqual(row["verdict"], "not comparable")
        self.assertIn("unstable", row["why"])                                 # says why, in words


if __name__ == "__main__":
    unittest.main()
