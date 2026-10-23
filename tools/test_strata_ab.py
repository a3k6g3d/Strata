"""strata_ab.py: the launch schedule (interleaved rounds), the noise-floor mode and the option checks.  Offline: only --dry-run, nothing is started."""
from __future__ import annotations

import contextlib
import io
import json
import shlex
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import noise_floor_unattended as N  # noqa: E402
import read_sweep_warm as W  # noqa: E402
import strata_ab as A  # noqa: E402

ARMS = [("base", []), ("b", ["--x 1"]), ("c", ["--y 2"])]
HARNESS_FLAGS = N.HARNESS[3:]             # the driver's default command minus "python -u <tool>"


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


class CacheOff(unittest.TestCase):
    """The arm the original four could not make: `--expert-cache 0` is not off while a profile is in use."""

    def setUp(self):
        self.arms = A.SWEEPS["cache-off"]["arms"]

    def test_the_two_arms_differ_by_exactly_one_flag(self):
        self.assertEqual(len(self.arms), 2)
        self.assertEqual(self.arms[0][1], [])                                # the baseline, unchanged
        self.assertEqual(self.arms[1][1], ["--expert-cache 1"])

    def test_the_arm_does_not_ask_for_zero(self):
        # the whole reason this sweep exists: generate.cpp:3499 turns a 0 back into auto while a profile is in use,
        # so an arm labelled cache-off and spelled `--expert-cache 0` measures the baseline under a second label.
        for _, args in self.arms:
            for i, a in enumerate(args):
                if a.startswith("--expert-cache"):
                    val = a.split(" ", 1)[1] if " " in a else args[i + 1]
                    self.assertNotEqual(val.strip(), "0", f"{a!r} is auto on this rig, not off")

    def test_the_note_states_the_confound_it_cannot_measure_away(self):
        # a 1-expert cache frees ~3.7 GiB that the KV and prompt path will take, so the arm is two changes, not one
        note = A.SWEEPS["cache-off"]["note"].lower()
        self.assertIn("3.7", note)
        self.assertIn("generate.cpp:3499", A.SWEEPS["cache-off"]["note"])    # says why 0 is not off

    def test_the_rounds_put_the_arms_in_a_different_order(self):
        plan = A.schedule(self.arms, 3)
        labels = [a[0] for _, _, a in plan]
        self.assertEqual(labels, [self.arms[0][0], self.arms[1][0],
                                  self.arms[1][0], self.arms[0][0],
                                  self.arms[0][0], self.arms[1][0]])
        for _, args in self.arms:                                            # each configuration sits at both positions
            pos = [n for n, (_, _, a) in enumerate(plan) if a[1] == args]
            self.assertNotEqual(pos[0] % 2, pos[1] % 2)


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


class TheBatchFile(unittest.TestCase):
    """The command tools/interleaved_control.bat will actually run, taken from the file itself.

    The tests above call schedule() directly, so they were green while the run happened and did not
    interleave: the driver's default command asks for the noise floor, --harness-args only appends, and
    the harness gives the noise floor precedence - three launches of one arm, with this sweep's note
    printed above every README anyway.  Only a test that starts from the batch file's own line can see
    that, so this one does.
    """

    BAT = ROOT / "tools" / "interleaved_control.bat"

    def harness_args(self) -> list:
        # the driver's own default flags, then whatever the batch file appends - the same join opts() does
        text = self.BAT.read_text(encoding="utf-8")
        line = [l for l in text.splitlines() if l.strip().startswith("--harness-args")]
        self.assertEqual(len(line), 1, "the batch file has exactly one run line")
        quoted = line[0].split('"')[1]
        return shlex.split(" ".join(HARNESS_FLAGS) + " " + quoted)

    def test_the_batch_file_cancels_the_drivers_default_noise_floor(self):
        argv = self.harness_args()
        # argparse takes the LAST of a repeated flag, so the batch file's own --noise-floor is the one that counts
        nf = [argv[i + 1] for i, x in enumerate(argv) if x == "--noise-floor"]
        self.assertEqual(len(nf), 2, "the driver's default, and the batch file cancelling it")
        self.assertEqual(nf[-1], "0")
        self.assertEqual(argv[argv.index("--sweep") + 1], "noise-interleaved")

    def test_the_command_it_runs_launches_both_arms_and_lands_each_at_both_positions(self):
        rc, out, err = dry_run(*self.harness_args())
        self.assertEqual(rc, 0, err)
        self.assertEqual([l.split(":")[0].replace("launch ", "").split("/")[0] for l in out.splitlines()
                          if l.startswith("launch ")], [str(n) for n in range(1, 7)])
        labels = [l.split(":")[1].split("  (round")[0].strip() for l in out.splitlines() if l.startswith("launch ")]
        self.assertEqual(labels, ["as configured", "as configured (repeat)",
                                  "as configured (repeat)", "as configured",
                                  "as configured", "as configured (repeat)"])
        for label in ("as configured", "as configured (repeat)"):
            pos = [n for n, l in enumerate(labels) if l == label]
            self.assertEqual(len(pos), 3)
            self.assertNotEqual(pos[0] % 2, pos[1] % 2)                        # sits at both positions, which is the point
        self.assertIn("runs 2 x", out)                                         # 6 runs over 3 rounds, as the note says

    def test_a_command_asking_for_both_is_refused_rather_than_guessed(self):
        rc, _, err = dry_run("--sweep", "noise-interleaved", "--rounds", "3", "--noise-floor", "3")
        self.assertEqual(rc, 2)
        self.assertIn("noise-floor", err)
        self.assertIn("--noise-floor 0", err)                                  # says how to mean the sweep


class TheCacheOffBatchFile(TheBatchFile):
    """The same two checks, run against tools/cache_off.bat's own line.

    Inheriting is the point: the trap that killed the first interleaved run - the driver's default
    command asks for the noise floor, --harness-args only appends, and the harness gives the noise
    floor precedence - is a property of the driver, not of one batch file, so every batch file needs
    the same test.  The labels differ, so the schedule test is overridden below.
    """

    BAT = ROOT / "tools" / "cache_off.bat"

    def test_the_batch_file_cancels_the_drivers_default_noise_floor(self):
        argv = self.harness_args()
        nf = [argv[i + 1] for i, x in enumerate(argv) if x == "--noise-floor"]
        self.assertEqual(len(nf), 2, "the driver's default, and the batch file cancelling it")
        self.assertEqual(nf[-1], "0")
        self.assertEqual(argv[argv.index("--sweep") + 1], "cache-off")

    def test_the_command_it_runs_launches_both_arms_and_lands_each_at_both_positions(self):
        rc, out, err = dry_run(*self.harness_args())
        self.assertEqual(rc, 0, err)
        labels = [l.split(":")[1].split("  (round")[0].strip() for l in out.splitlines() if l.startswith("launch ")]
        self.assertEqual(labels, ["as configured", "expert-cache 1 (off in effect)",
                                  "expert-cache 1 (off in effect)", "as configured",
                                  "as configured", "expert-cache 1 (off in effect)"])
        for label in ("as configured", "expert-cache 1 (off in effect)"):
            pos = [n for n, l in enumerate(labels) if l == label]
            self.assertEqual(len(pos), 3)
            self.assertNotEqual(pos[0] % 2, pos[1] % 2)                        # sits at both positions, which is the point
        self.assertIn("runs 2 x", out)

    def test_it_writes_somewhere_other_than_the_arms_it_is_not_comparable_to(self):
        # bench/results/ab-cache-vs-drafts already holds the four October arms, and the config has
        # moved since they ran, so a run that landed there would be read beside numbers it cannot be
        # compared with.  The batch file has to name its own folder.
        argv = self.harness_args()
        out = [argv[i + 1] for i, x in enumerate(argv) if x == "--out"]
        self.assertEqual(len(out), 1, "the batch file names its own output folder")
        self.assertNotEqual(Path(out[0]).name, "ab-cache-vs-drafts")
        self.assertEqual(Path(out[0]).name, "ab-cache-off")

    def test_the_arm_it_runs_is_not_the_one_the_old_arms_ran(self):
        # the dry run prints the engine arguments it will pass; the flag that silently meant auto is the
        # one this whole run exists to avoid, so it must not appear
        _, out, _ = dry_run(*self.harness_args())
        self.assertNotIn("--expert-cache 0", out)
        self.assertIn("--expert-cache 1", out)


class ThePcieRerunBatchFile(TheBatchFile):
    """The same checks, run against tools/pcie_rerun.bat's own line - see TheCacheOffBatchFile for why
    every batch file needs this class and not just the one.  The labels and the counts differ, so the
    schedule test and the noise-floor test are overridden below.
    """

    BAT = ROOT / "tools" / "pcie_rerun.bat"

    def test_the_batch_file_cancels_the_drivers_default_noise_floor(self):
        argv = self.harness_args()
        nf = [argv[i + 1] for i, x in enumerate(argv) if x == "--noise-floor"]
        self.assertEqual(len(nf), 2, "the driver's default, and the batch file cancelling it")
        self.assertEqual(nf[-1], "0")
        self.assertEqual(argv[argv.index("--sweep") + 1], "pcie-frac-rerun")

    def test_the_command_it_runs_launches_both_arms_and_lands_each_at_both_positions(self):
        rc, out, err = dry_run(*self.harness_args())
        self.assertEqual(rc, 0, err)
        labels = [l.split(":")[1].split("  (round")[0].strip() for l in out.splitlines() if l.startswith("launch ")]
        self.assertEqual(len(labels), 10)                                       # five rounds x two arms
        for rnd in range(5):
            self.assertEqual(sorted(labels[rnd * 2:(rnd + 1) * 2]), ["as configured", "pcie-frac 0.35"])
        for label in ("as configured", "pcie-frac 0.35"):
            pos = [n for n, l in enumerate(labels) if l == label]
            self.assertEqual(len(pos), 5)
            self.assertNotEqual(pos[0] % 2, pos[1] % 2)                         # sits at both positions, which is the point
        self.assertIn("runs 2 x", out)                                          # 10 runs over 5 rounds

    def test_it_keeps_the_shape_the_cold_warm_split_needs(self):
        # read_sweep_warm.py splits each arm's cold run (the first of each launch) from its warm ones, so a
        # launch has to contain at least two runs for that split to exist at all.  This is the whole reason
        # the batch file asks for --runs 10 and not the driver's default 6: 6 over 5 rounds is one run a
        # launch, every run is then a cold run, and the split the last batch was read by disappears.
        argv = self.harness_args()
        runs = [argv[i + 1] for i, x in enumerate(argv) if x == "--runs"]
        self.assertEqual(runs[-1], "10")                                        # the batch file's own, not the default
        self.assertEqual(int(runs[-1]) // 5, 2)

    def test_it_writes_somewhere_other_than_the_batch_it_is_not_comparable_to(self):
        # bench/results/ab-pcie-frac holds the three-launch batch this one is settling, and it ran without
        # the profiler stamps; a run that landed there would be read beside numbers taken a different way.
        argv = self.harness_args()
        out = [argv[i + 1] for i, x in enumerate(argv) if x == "--out"]
        self.assertEqual(len(out), 1, "the batch file names its own output folder")
        self.assertEqual(Path(out[0]).name, "ab-pcie-frac-rerun")

    def test_the_batch_asks_its_own_noise_and_says_so_before_running(self):
        # the plan's rule: an arm is stated as it measures, and the figure it has to beat is the batch's own
        # noise.  With two arms there is no repeat row, so the batch's floor is the baseline's own five
        # launches - which is only true if the baseline is launched five times, i.e. the rounds.
        text = self.BAT.read_text(encoding="utf-8")
        self.assertIn("five baseline launches", text)
        self.assertIn("STRATA_VERIFY_PROFILE=1", text)                          # without these, half the run is lost
        self.assertIn("STRATA_DECODE_TIMING=1", text)

    def test_it_pins_a_config_that_actually_has_the_env_block_the_stamps_need(self):
        # the driver's default command names the live config, which has no env block, so the engine would
        # print no window split at all and the waitB half of this run would be lost; argparse takes the
        # LAST --config, so the batch file pins its own copy - and that copy has to really carry the keys.
        argv = self.harness_args()
        cfg = [argv[i + 1] for i, x in enumerate(argv) if x == "--config"]
        self.assertEqual(len(cfg), 2, "the driver's default, and the batch file pinning its own")
        path = Path(cfg[-1])
        self.assertNotEqual(path.name, "strata.json", "the live config has no env block")
        self.assertTrue(path.exists(), f"{path} is the config the run will read, so it has to be there")
        env = json.loads(path.read_text(encoding="utf-8")).get("env", {})
        self.assertEqual(env.get("STRATA_VERIFY_PROFILE"), "1")
        self.assertEqual(env.get("STRATA_DECODE_TIMING"), "1")
        live = json.loads((path.parent / "strata.json").read_text(encoding="utf-8"))
        self.assertEqual({k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items()
                          if k != "env"}, live, "the stamps are the only difference from the live config")


class DmaBatch(unittest.TestCase):
    """The batch that tests the submission batch directly: both arms on the DMA path, the only difference
    being whether the group's uploads go out one by one or as one cudaMemcpyBatchAsync.  The knob is an
    environment variable, so this is also the first sweep whose arm is spelled --engine-env."""

    def setUp(self):
        self.arms = A.SWEEPS["dma-batch"]["arms"]

    def test_both_arms_are_on_the_dma_path(self):
        # the knob only has something to act on under --pcie-mode dma; the configuration as it stands runs
        # the copy kernel, where the batch never runs - a baseline left there would measure the same code
        # twice and call the difference a win.
        self.assertEqual(len(self.arms), 2)
        for _, args in self.arms:
            self.assertIn("--pcie-mode dma", args)

    def test_the_two_arms_differ_by_exactly_the_knob(self):
        base, arm = [args for _, args in self.arms]
        # both arms name the variable - the last run's baseline left it unset and never reached the
        # submission path - so the only difference left is its value: the loop's 0 against the batch's 1.
        self.assertEqual(sorted(set(arm) - set(base)), ["--engine-env=STRATA_DMA_BATCH=1"])
        self.assertEqual(sorted(set(base) - set(arm)), ["--engine-env=STRATA_DMA_BATCH=0"])

    def test_the_note_states_the_reading_that_failed_last_time(self):
        # the previous batch's mechanism rule died because the source side did not move; this one reads the
        # submissions themselves, exactly, so the note has to say what number proves what.
        note = A.SWEEPS["dma-batch"]["note"]
        self.assertIn("STRATA_DMA_BATCH", note)
        self.assertIn("--pcie-mode dma", note)
        self.assertIn("submission", note.lower())

    def test_the_rounds_put_the_arms_in_a_different_order(self):
        plan = A.schedule(self.arms, 3)
        self.assertEqual([a[0] for _, _, a in plan],
                         ["dma loop", "dma batched", "dma batched", "dma loop", "dma loop", "dma batched"])

    def test_an_arm_item_that_is_already_an_arm_switch_passes_through_as_itself(self):
        # the arm's items reach the server as --engine-arg; --engine-env is the benchmark's own switch and
        # has to survive the trip as itself, or the arm would ask for an argument named "--engine-env ..."
        rc, out, _ = dry_run("--sweep", "dma-batch", "--rounds", "1")
        self.assertEqual(rc, 0)
        self.assertIn("--engine-env=STRATA_DMA_BATCH=1", out)
        self.assertNotIn("--engine-arg=--engine-env", out)


class TheDmaBatchFile(TheBatchFile):
    """The same checks, run against tools/dma_batch.bat's own line - see TheCacheOffBatchFile for why
    every batch file needs this class and not just the one.  The labels and the counts differ, so the
    schedule test and the noise-floor test are overridden below.
    """

    BAT = ROOT / "tools" / "dma_batch.bat"

    def test_the_batch_file_cancels_the_drivers_default_noise_floor(self):
        argv = self.harness_args()
        nf = [argv[i + 1] for i, x in enumerate(argv) if x == "--noise-floor"]
        self.assertEqual(len(nf), 2, "the driver's default, and the batch file cancelling it")
        self.assertEqual(nf[-1], "0")
        self.assertEqual(argv[argv.index("--sweep") + 1], "dma-batch")

    def test_the_command_it_runs_launches_both_arms_and_lands_each_at_both_positions(self):
        rc, out, err = dry_run(*self.harness_args())
        self.assertEqual(rc, 0, err)
        labels = [l.split(":")[1].split("  (round")[0].strip() for l in out.splitlines() if l.startswith("launch ")]
        self.assertEqual(len(labels), 6)                                        # three rounds x two arms
        for rnd in range(3):
            self.assertEqual(sorted(labels[rnd * 2:(rnd + 1) * 2]), ["dma batched", "dma loop"])
        for label in ("dma loop", "dma batched"):
            pos = [n for n, l in enumerate(labels) if l == label]
            self.assertEqual(len(pos), 3)
            self.assertNotEqual(pos[0] % 2, pos[1] % 2)                         # sits at both positions, which is the point
        self.assertIn("runs 3 x", out)                                          # 9 runs over 3 rounds

    def test_it_keeps_the_shape_the_cold_warm_split_needs(self):
        # see ThePcieRerunBatchFile: one run a launch would make every run a cold run, and the
        # cold/warm split the last batch was read by would disappear.
        argv = self.harness_args()
        runs = [argv[i + 1] for i, x in enumerate(argv) if x == "--runs"]
        self.assertEqual(runs[-1], "9")                                         # the batch file's own, not the default
        self.assertEqual(int(runs[-1]) // 3, 3)

    def test_it_writes_somewhere_other_than_the_batch_it_is_not_comparable_to(self):
        # bench/results/ab-dma-batch is the run whose baseline never reached the submission path; its numbers are
        # on the record and must not be overwritten by the rerun, which is why the rerun names a new folder.
        argv = self.harness_args()
        out = [argv[i + 1] for i, x in enumerate(argv) if x == "--out"]
        self.assertEqual(len(out), 1, "the batch file names its own output folder")
        self.assertEqual(Path(out[0]).name, "ab-dma-batch-2")

    def test_it_pins_a_config_that_actually_has_the_env_block_the_stamps_need(self):
        # the driver's default command names the live config, which has no env block and points at the
        # uninstrumented exe: no per-window stamps at all, and no submission counter.  argparse takes the
        # LAST --config, so the batch file pins its own - and that copy has to really carry the keys.
        argv = self.harness_args()
        cfg = [argv[i + 1] for i, x in enumerate(argv) if x == "--config"]
        self.assertEqual(len(cfg), 2, "the driver's default, and the batch file pinning its own")
        path = Path(cfg[-1])
        self.assertNotEqual(path.name, "strata.json", "the live config has no env block")
        self.assertTrue(path.exists(), f"{path} is the config the run will read, so it has to be there")
        env = json.loads(path.read_text(encoding="utf-8-sig")).get("env", {})
        for key in ("STRATA_VERIFY_PROFILE", "STRATA_DECODE_TIMING", "STRATA_RAM_TIER_CLOCK"):
            self.assertEqual(env.get(key), "1", f"{path} prints the per-window stamps only with {key}")

    def test_the_batch_asks_its_own_noise_and_says_so_before_running(self):
        # the plan's rule: an arm is stated as it measures, and the figure it has to beat is the batch's own
        # noise - the baseline's own three launches.  And the mechanism rule is the one that failed last time,
        # so it is stated here, before the first launch, with the void clause that goes with it.
        text = self.BAT.read_text(encoding="utf-8")
        self.assertIn("three baseline launches", text)
        self.assertIn("ALL THREE", text)                                        # three rounds leave no room for a dissent
        self.assertIn("blobs-per-submission", text)
        self.assertIn("void", text)
        self.assertIn("strata decode submissions", text)

    def test_both_arms_set_the_variable_so_both_reach_the_submission_path(self):
        # the run that failed left the baseline's variable unset and its counters read 0 on every
        # window - it never reached copy_blobs, so the arms were different code paths.  The fix is
        # that both arms name the variable, 0 against 1; the dry-run prints each arm's command.
        rc, out, err = dry_run(*self.harness_args())
        self.assertEqual(rc, 0, err)
        cmds = {l.split(":")[1].split("  (round")[0].strip(): c
                for l, c in zip(out.splitlines(), out.splitlines()[1:]) if l.startswith("launch ")}
        self.assertIn("STRATA_DMA_BATCH=0", cmds["dma loop"])
        self.assertIn("STRATA_DMA_BATCH=1", cmds["dma batched"])
        for label in ("dma loop", "dma batched"):
            self.assertIn("--pcie-mode dma", cmds[label])

    def test_the_binary_it_runs_is_the_one_that_stamps_the_counter(self):
        # the counter line is stamped by the instrumented build; the batch runs the exe the pinned config
        # names, so that exe has to carry the stamp, or every launch is void for this question.
        argv = self.harness_args()
        path = Path([argv[i + 1] for i, x in enumerate(argv) if x == "--config"][-1])
        exe = Path(json.loads(path.read_text(encoding="utf-8-sig"))["exe"].replace("/", "\\"))
        self.assertTrue(exe.exists(), f"{exe} is the exe the run will start, so it has to be there")
        self.assertIn(b"decode submissions", exe.read_bytes(), "the exe the config names does not stamp the counter")

class TestRepeatOfBaseline(unittest.TestCase):
    # a repeat of the baseline is the same command run again, so the gap on its row IS the
    # noise floor and the row cannot be judged as an arm.  The two sweeps label it
    # differently - adapt-kv writes "as configured (repeat)", the cache-off batch just runs
    # the baseline three times - so both spellings have to land the same way.  The old code
    # compared label sets and so let adapt-kv's row through as an arm; its 70% gap was then
    # read as the noise floor, which is the wrong way round.
    def test_the_two_ways_the_sweeps_label_a_repeat(self):
        self.assertTrue(W.is_repeat_of_baseline("as configured (repeat)", "as configured"))
        self.assertTrue(W.is_repeat_of_baseline("as configured", "as configured"))
        self.assertTrue(W.is_repeat_of_baseline("as configured - repeat", "as configured"))
        self.assertTrue(W.is_repeat_of_baseline("as configured 2", "as configured"))

    def test_a_real_arm_is_not_a_repeat(self):
        base = "as configured"
        for label in ("expert-cache 1 (off in effect)", "kv-3.5 (no min)", "as configured --min_keep 1"):
            self.assertFalse(W.is_repeat_of_baseline(label, base), label)

    def test_nothing_matches_an_empty_label(self):
        # the tool asks with the baseline's own label, so it never sees a doubled suffix;
        # what it does have to refuse is two blanks matching each other.
        self.assertFalse(W.is_repeat_of_baseline("", ""))
        self.assertFalse(W.is_repeat_of_baseline("(repeat)", ""))


class TestSeparable(unittest.TestCase):
    # A gap between two single medians is not a margin until the spread underneath it is looked at.
    # The tool says "cannot separate" when the two distributions overlap, and it has to get both
    # batches on disk right: adapt-kv's biggest arm (per-turn 43-97%) sits inside its own noise row
    # (54-98%), and cache-off's arm (28-44%) sits wholly outside its repeats (1-24%).
    def test_overlapping_distributions_are_not_separated(self):
        arm = [0.43, 0.51, 0.50, 0.97, 0.77, 0.65]
        noise = [0.72, 0.58, 0.54, 0.98, 0.87, 0.61]
        self.assertFalse(W.separable(arm, noise))

    def test_distributions_that_do_not_touch_are_separated(self):
        arm = [0.28, 0.30, 0.31, 0.37, 0.44, 0.32]
        noise = [0.01, 0.04, 0.02, 0.01, 0.24, 0.03]
        self.assertTrue(W.separable(arm, noise))

    def test_no_distribution_is_answered_as_no_distribution(self):
        self.assertIsNone(W.separable([0.3], []))
        self.assertIsNone(W.separable([], [0.3]))
        self.assertIsNone(W.separable([], []))


if __name__ == "__main__":
    unittest.main()
