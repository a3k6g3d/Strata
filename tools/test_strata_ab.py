"""strata_ab.py: the launch schedule (interleaved rounds), the noise-floor mode and the option checks.  Offline: only --dry-run, nothing is started."""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import strata_ab as A  # noqa: E402

ARMS = [("base", []), ("b", ["--x 1"]), ("c", ["--y 2"])]


def dry_run(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = A.main(["--config", "strata.json", "--dry-run", *args])
    return rc, out.getvalue(), err.getvalue()


class Schedule(unittest.TestCase):
    def test_one_round_is_the_arms_in_order(self):
        self.assertEqual([(r, i) for r, i, _ in A.schedule(ARMS, 1)], [(1, 0), (1, 1), (1, 2)])

    def test_every_second_round_runs_backwards_so_linear_drift_cancels(self):
        plan = A.schedule(ARMS, 4)
        self.assertEqual([a[0] for _, _, a in plan], ["base", "b", "c", "c", "b", "base", "base", "b", "c", "c", "b", "base"])
        self.assertEqual([r for r, _, _ in plan], [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4])

    def test_a_linear_drift_gives_every_arm_the_same_mean_position_in_time(self):
        plan = A.schedule(ARMS, 2)                       # base: positions 0 and 5, b: 1 and 4, c: 2 and 3 -> the same mean, 2.5
        pos = {}
        for n, (_, _, arm) in enumerate(plan):
            pos.setdefault(arm[0], []).append(n)
        self.assertEqual({k: sum(v) / len(v) for k, v in pos.items()}, {"base": 2.5, "b": 2.5, "c": 2.5})

    def test_order_same_keeps_every_round_forwards(self):
        plan = A.schedule(ARMS, 2, order="same")
        self.assertEqual([a[0] for _, _, a in plan], ["base", "b", "c", "base", "b", "c"])

    def test_every_arm_runs_once_per_round(self):
        plan = A.schedule(A.SWEEPS["adapt-kv"]["arms"], 3)
        self.assertEqual(len(plan), 3 * len(A.SWEEPS["adapt-kv"]["arms"]))
        for rnd in (1, 2, 3):
            self.assertEqual(sorted(i for r, i, _ in plan if r == rnd), list(range(len(A.SWEEPS["adapt-kv"]["arms"]))))


class NoiseInterleaved(unittest.TestCase):
    """The control the batches on disk cannot be: the same arm twice, so any difference IS the noise."""

    def setUp(self):
        self.arms = A.SWEEPS["noise-interleaved"]["arms"]

    def test_the_two_arms_are_the_same_command(self):
        # the point of the sweep: nothing about the engine changes between them, so a difference
        # between the two labels is the noise, not an effect.  Only the label differs.
        self.assertEqual(len(self.arms), 2)
        self.assertEqual([args for _, args in self.arms], [[], []])
        self.assertEqual(len({label for label, _ in self.arms}), 2)

    def test_the_rounds_put_the_arms_in_a_different_order(self):
        plan = A.schedule(self.arms, 3)
        self.assertEqual([a[0] for _, _, a in plan],
                         ["as configured", "as configured (repeat)",
                          "as configured (repeat)", "as configured",
                          "as configured", "as configured (repeat)"])
        # ... which is the whole reason it exists: each label sits at both positions, so a
        # within-round difference is not the same variable as the machine's drift.
        for label in ("as configured", "as configured (repeat)"):
            pos = [n for n, (_, _, a) in enumerate(plan) if a[0] == label]
            self.assertEqual(len(pos), 3)
            self.assertNotEqual(pos[0] % 2, pos[1] % 2)

    def test_one_round_of_it_is_still_the_arms_in_order(self):
        self.assertEqual([a[0] for _, _, a in A.schedule(self.arms, 1)],
                         ["as configured", "as configured (repeat)"])


class Options(unittest.TestCase):
    def test_a_dry_run_prints_the_rounds_and_the_runs_per_launch(self):
        rc, out, _ = dry_run("--sweep", "adapt-kv", "--rounds", "3", "--runs", "6")
        self.assertEqual(rc, 0)
        self.assertIn("launch 1/39: as configured  (round 1 of 3)", out)
        self.assertIn("launch 14/39: as configured (repeat)  (round 2 of 3)", out)       # round 2 starts with the last arm
        self.assertIn("runs 2 x", out)                                                    # 6 runs over 3 rounds

    def test_one_round_is_unchanged(self):
        rc, out, _ = dry_run("--sweep", "adapt-kv", "--runs", "3")
        self.assertEqual(rc, 0)
        self.assertIn("launch 1/13:", out)
        self.assertIn("runs 3 x", out)

    def test_runs_per_launch_is_at_least_one(self):
        _, out, _ = dry_run("--rounds", "5", "--runs", "2")
        self.assertIn("runs 1 x", out)

    def test_noise_floor_launches_the_baseline_alone_n_times_into_its_own_folder(self):
        rc, out, _ = dry_run("--noise-floor", "3")
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("as configured"), 3)
        self.assertNotIn("expert-cache off", out)
        self.assertIn("launch 3/3: as configured  (round 3 of 3)", out)
        self.assertIn("noise-floor", out)

    def test_bad_counts_are_refused(self):
        for args in (("--rounds", "0"), ("--noise-floor", "-1")):
            rc, _, err = dry_run(*args)
            self.assertEqual(rc, 2)
            self.assertIn("at least", err)


if __name__ == "__main__":
    unittest.main()
