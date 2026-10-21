"""noise_floor_unattended.py --harness-args / --log, offline: no process is started or stopped.

The driver used to have one shape hardcoded - three launches of the baseline, one fixed log.  The
interleaved control needs a different schedule and its own log, and the batches have already shown
why the log matters: three of them wrote over one file.  So both are now overridable, and both have
to keep the old defaults when nothing is asked for - the .bat files in this repo pass no flags and
must go on meaning the same run they meant before.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import noise_floor_unattended as N  # noqa: E402


class Opts(unittest.TestCase):
    def test_nothing_asked_is_the_run_that_ran_before(self):
        harness, log = N.opts([])
        self.assertEqual(harness, N.HARNESS)
        self.assertEqual(log, N.LOG)
        # and the default command is still the shape the batch file relies on
        self.assertIn("--noise-floor", harness)
        self.assertEqual(harness[harness.index("--noise-floor") + 1], "3")

    def test_harness_args_are_appended_after_the_default_ones(self):
        harness, _ = N.opts(["--harness-args", "--sweep noise-interleaved --rounds 3"])
        self.assertEqual(harness, N.HARNESS + ["--sweep", "noise-interleaved", "--rounds", "3"])

    def test_the_log_can_be_moved(self):
        _, log = N.opts(["--log", "bench/results/other.log"])
        self.assertEqual(log, Path("bench/results/other.log"))
        self.assertNotEqual(log, N.LOG)

    def test_both_together(self):
        harness, log = N.opts(["--log", "bench/results/other.log",
                               "--harness-args", "--sweep noise-interleaved --rounds 3 --runs 6"])
        self.assertEqual(harness[-6:], ["--sweep", "noise-interleaved", "--rounds", "3", "--runs", "6"])
        self.assertEqual(log, Path("bench/results/other.log"))

    def test_a_flag_with_nothing_after_it_is_not_an_error_to_give_it(self):
        # the driver runs unattended and dies in the middle of the thing it interrupts; a missing
        # value must not be the reason a run that could have happened did not
        harness, log = N.opts(["--harness-args"])
        self.assertEqual(harness, N.HARNESS)
        self.assertEqual(log, N.LOG)

    def test_the_harness_output_file_follows_the_log_it_was_written_for(self):
        # main() names it LOG.stem + "-harness-out.log"; three batches sharing one log used to share
        # one output file as well, which is how a run can happen and its story never be told
        self.assertEqual(N.LOG.with_name(N.LOG.stem + "-harness-out.log").name,
                         "noise-floor-unattended-harness-out.log")
        moved = Path("bench/results/noise-interleaved.log")
        self.assertEqual(moved.with_name(moved.stem + "-harness-out.log").name,
                         "noise-interleaved-harness-out.log")
        self.assertNotEqual(N.LOG.with_name(N.LOG.stem + "-harness-out.log"),
                            moved.with_name(moved.stem + "-harness-out.log"))


if __name__ == "__main__":
    unittest.main()
