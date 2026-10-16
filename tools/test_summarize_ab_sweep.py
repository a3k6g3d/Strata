"""A/B summary: cold/warm reference choice, launch-line grouping, turn-paired medians.

Offline: builds results.json files in a temp folder, runs no engine.

    python -m unittest tools.test_summarize_ab_sweep
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import summarize_ab_sweep as S  # noqa: E402

BASE = ["--pack", "p", "--spec", "4", "--mtp"]


def write_run(root: Path, arm: str, tps: list[float], t0: float, launch: str,
              extra: list[str] = (), engine: dict | None = None) -> None:
    """One arm's results.json: turns carry a turn number, decode_tps and an ok flag."""
    d = root / arm.replace(" ", "-")
    d.mkdir(parents=True, exist_ok=True)
    turns = [{"turn": i, "ok": True, "decode_tps": t, "ttft_s": 1.0,
              "prompt_tokens": 200, "generated_tokens": int(t * 8)} for i, t in enumerate(tps)]
    body = {
        "elapsed_s": t0 + 60.0,
        "machine": {"launch": launch},
        "params": {"arm": arm, "engine_arg": list(BASE) + list(extra)},
        "results": {"phases": [{"name": "agent session", "from_s": t0}],
                    "agent": {"sessions": [turns]},
                    "engine": {"per_test": {"agent session": engine or {}}}},
    }
    (d / "results.json").write_text(json.dumps(body), encoding="utf-8")


def load(root: Path):
    rows = S.collect(str(root))
    S.position(rows)
    return rows


class Reference(unittest.TestCase):
    def test_a_cold_reference_is_not_used_when_a_warm_twin_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(root, "as configured", [16.0], 0.0, "exe " + " ".join(BASE))
            write_run(root, "as configured (repeat)", [26.0], 5400.0, "exe " + " ".join(BASE))
            write_run(root, "arm warm", [26.5], 7200.0, "exe " + " ".join(BASE) + " --x 1")
            write_run(root, "arm cold", [16.2], 300.0, "exe " + " ".join(BASE) + " --y 1")
            rows = load(root)
            ref = S.pick_reference(rows)
            self.assertEqual(ref["arm"], "as configured (repeat)")   # the warm twin, not the first
            self.assertFalse(S.is_cold(ref))
            out = {r["arm"]: d for r, d, _ in S.paired(rows, ref)}
            self.assertAlmostEqual(out["arm warm"], 1.9, places=1)   # noise, not a win
            self.assertLess(out["arm cold"], -30.0)                  # the machine warming up

    def test_all_cold_falls_back_to_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(root, "as configured", [16.0], 0.0, "exe a")
            write_run(root, "as configured (repeat)", [17.0], 300.0, "exe a")
            rows = load(root)
            self.assertEqual(S.pick_reference(rows)["arm"], "as configured")
            self.assertTrue(all(S.is_cold(r) for r in rows))

    def test_nothing_to_pair_against(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(S.pick_reference(load(Path(tmp))))


class Grouping(unittest.TestCase):
    def test_identical_launch_groups_even_when_named_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(root, "as configured", [20.0, 22.0], 0.0, "exe same", extra=["--a", "1"])
            write_run(root, "as configured (repeat)", [19.0, 21.0], 5400.0, "exe same",
                      extra=["--a", "1"])
            write_run(root, "arm", [10.0], 7200.0, "exe other", extra=["--b", "2"])
            rows = load(root)
            groups = [g for g in S.by_launch(rows).values() if len(g) > 1]
            self.assertEqual(len(groups), 1)
            self.assertEqual({g["arm"] for g in groups[0]},
                             {"as configured", "as configured (repeat)"})

    def test_position_is_measured_from_the_earliest_arm_of_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(root, "first", [20.0], 120.0, "exe a")
            write_run(root, "second", [20.0], 6120.0, "exe b")
            rows = {r["arm"]: r for r in load(root)}
            self.assertEqual(rows["first"]["tmin"], 0.0)
            self.assertEqual(rows["second"]["tmin"], 100.0)


class Paired(unittest.TestCase):
    def test_median_of_two_runs_of_the_same_arm_beats_a_lucky_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(root, "ref", [20.0, 20.0, 20.0], 5400.0, "exe a")
            write_run(root, "arm", [100.0, 20.0, 20.0], 7200.0, "exe b", extra=["--x", "1"])
            rows = load(root)
            row = {r["arm"]: r for r in rows}
            one = S.paired(rows, row["ref"])[0][1]
            self.assertAlmostEqual(one, 0.0, places=1)   # median; the mean would have said +133%

    def test_turns_that_do_not_line_up_are_not_paired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(root, "ref", [20.0], 5400.0, "exe a")
            write_run(root, "arm", [20.0], 7200.0, "exe b", extra=["--x", "1"])
            rows = load(root)
            ref = [r for r in rows if r["arm"] == "ref"][0]
            [r for r in rows if r["arm"] == "arm"][0]["turns"] = [
                {"turn": 7, "ok": True, "decode_tps": 30.0}]        # a turn the reference never ran
            self.assertEqual(S.paired(rows, ref), [])

    def test_failed_turns_are_dropped_by_collect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(root, "arm", [20.0], 5400.0, "exe a")
            p = root / "arm" / "results.json"
            j = json.loads(p.read_text(encoding="utf-8"))
            j["results"]["agent"]["sessions"][0].append({"turn": 1, "ok": False, "decode_tps": 5.0})
            p.write_text(json.dumps(j), encoding="utf-8")
            self.assertEqual(load(Path(tmp))[0]["n"], 1)


class Render(unittest.TestCase):
    def test_cold_mark_and_noise_floor_appear_in_the_table(self):
        import io
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(root, "as configured", [16.0], 0.0, "exe a")
            write_run(root, "as configured (repeat)", [26.0], 5400.0, "exe a")
            write_run(root, "arm cold", [16.1], 120.0, "exe b", extra=["--x", "1"],
                      engine={"decode_tok_s_median": 17.0, "cache_hit_pct": 51.0,
                              "pcie_share_pct": 9.0, "prompt_reuse_pct": 80.0,
                              "draft_accept_pct": 75.0})
            buf = io.StringIO()
            argv = sys.argv
            sys.argv = ["summarize_ab_sweep.py", str(root)]
            try:
                with contextlib.redirect_stdout(buf):
                    S.main()
            finally:
                sys.argv = argv
            out = buf.getvalue()
        self.assertIn("arm cold", out)
        self.assertIn("*cold", out)                                  # flagged, not read as a result
        self.assertIn("identical launch line x2", out)               # the noise floor is printed
        self.assertIn("paired by turn number against 'as configured (repeat)'", out)



def write_launch(root: Path, arm: str, n: int, tps: list[float], t0: float, launch: str) -> None:
    """The n-th launch of an arm: its own folder, the same arm label (how `strata_ab.py --rounds` leaves them)."""
    d = root / f"{arm.replace(' ', '-')}-{n}"
    d.mkdir(parents=True, exist_ok=True)
    turns = [{"turn": i, "ok": True, "decode_tps": t, "ttft_s": 1.0} for i, t in enumerate(tps)]
    body = {"elapsed_s": 60.0, "machine": {"launch": launch}, "params": {"arm": arm, "engine_arg": list(BASE)},
            "results": {"phases": [{"name": "agent session", "from_s": t0}], "agent": {"sessions": [turns]},
                        "engine": {"per_test": {"agent session": {}}}}}
    (d / "results.json").write_text(json.dumps(body), encoding="utf-8")


def scaled(base: list[float], factor: float) -> list[float]:
    return [round(x * factor, 4) for x in base]


def summary_text(*argv) -> str:
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        S.main(list(argv))
    return buf.getvalue()


TURNS = [20.0, 24.0, 22.0, 26.0]
LAUNCH_A, LAUNCH_B, LAUNCH_C = "exe a", "exe b", "exe c"


class Interleaved(unittest.TestCase):
    def build(self, root: Path, drift=(1.0, 1.1, 1.2), b_factor=1.05, c_factor=1.01):
        """Three rounds of arms A (the reference), B and C; the machine speeds up across the sweep, B really is 5% faster, C 1%."""
        for n, (drift_n, t0) in enumerate(zip(drift, (3000.0, 6000.0, 9000.0)), 1):         # all past the cold region
            write_launch(root, "as configured", n, scaled(TURNS, drift_n), t0, LAUNCH_A)
            write_launch(root, "arm b", n, scaled(TURNS, drift_n * b_factor), t0 + 200, LAUNCH_B)
            write_launch(root, "arm c", n, scaled(TURNS, drift_n * c_factor), t0 + 400, LAUNCH_C)

    def test_rounds_are_numbered_in_the_order_the_launches_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.build(root)
            arms = S.assign_rounds(load(root))
            self.assertEqual([r["round"] for r in arms["arm b"]], [1, 2, 3])
            self.assertEqual([r["t0"] for r in arms["arm b"]], [3200.0, 6200.0, 9200.0])

    def test_drift_cancels_because_each_launch_is_compared_with_its_own_rounds_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.build(root)                                         # the speed rises 20% over the sweep; B is 5% faster in every round
            res = {i["arm"]: i for i in S.interleaved(load(root), "as configured")}
            self.assertEqual([d[0] for d in res["arm b"]["deltas"]], [1, 2, 3])
            for _, delta, _, cold in res["arm b"]["deltas"]:
                self.assertAlmostEqual(delta, 5.0, places=1)         # the drift is gone
            self.assertEqual([d[3] for d in res["arm b"]["deltas"]], [True, False, False])        # round 1 starts the sweep: inside its first minutes
            self.assertAlmostEqual(res["arm c"]["deltas"][0][1], 1.0, places=1)

    def test_a_drifting_reference_has_a_wide_noise_figure_so_a_small_win_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.build(root)                                         # the reference's own launches span 20%: that is the noise to beat
            out = summary_text(str(root))
            self.assertIn("noise floor: the reference arm's own launches", out)
            b = next(line for line in out.splitlines() if line.strip().startswith("arm b") and "median" in line)
            self.assertIn("median +5.0%", b)
            self.assertIn("inside noise", b)

    def test_a_steady_reference_gives_a_tight_noise_figure_and_the_verdicts_follow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.build(root, drift=(1.0, 1.015, 1.03))               # a steady machine: the reference's launches differ by about 3%
            out = summary_text(str(root))
            b = next(line for line in out.splitlines() if line.strip().startswith("arm b") and "median" in line)
            c = next(line for line in out.splitlines() if line.strip().startswith("arm c") and "median" in line)
            self.assertIn("above noise", b)                          # +5% against about 3%
            self.assertIn("inside noise", c)                         # +1% against about 3%

    def test_a_noise_floor_run_drops_its_first_launch_and_gives_the_verdict_for_single_launch_arms(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as noise_dir:
            root, nz = Path(tmp), Path(noise_dir)
            for n, (f, t0) in enumerate(((0.7, 0.0), (1.0, 3000.0), (1.02, 6000.0)), 1):         # launch 1 is the cold one
                write_launch(nz, "as configured", n, scaled(TURNS, f), t0, LAUNCH_A)
            floor = S.noise_from(str(nz))
            self.assertTrue(floor["dropped_first"])
            self.assertEqual(floor["n"], 2)
            self.assertAlmostEqual(floor["pct"], 2.0, places=1)       # 1.00 against 1.02, not 0.70
            write_launch(root, "as configured", 1, TURNS, 3000.0, LAUNCH_A)
            write_launch(root, "big win", 1, scaled(TURNS, 1.08), 3200.0, LAUNCH_B)
            write_launch(root, "small win", 1, scaled(TURNS, 1.01), 3400.0, LAUNCH_C)
            out = summary_text(str(root), "--noise", str(nz))
            self.assertIn("noise-floor run", out)
            self.assertTrue(any("big win" in line and "above noise" in line for line in out.splitlines()), out)
            self.assertTrue(any("small win" in line and "inside noise" in line for line in out.splitlines()), out)

    def test_launches_inside_the_cold_region_are_marked_and_left_out_of_the_median(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_launch(root, "as configured", 1, scaled(TURNS, 0.7), 0.0, LAUNCH_A)           # round 1: the cold region
            write_launch(root, "arm b", 1, scaled(TURNS, 0.7 * 1.5), 200.0, LAUNCH_B)             # a fake +50% from the cold region
            write_launch(root, "as configured", 2, TURNS, 3000.0, LAUNCH_A)
            write_launch(root, "arm b", 2, scaled(TURNS, 1.03), 3200.0, LAUNCH_B)
            write_launch(root, "as configured", 3, TURNS, 6000.0, LAUNCH_A)
            write_launch(root, "arm b", 3, scaled(TURNS, 1.03), 6200.0, LAUNCH_B)
            res = S.interleaved(load(root), "as configured")[0]
            self.assertEqual([d[3] for d in res["deltas"]], [True, False, False])
            out = summary_text(str(root))
            line = next(line for line in out.splitlines() if line.strip().startswith("arm b") and "median" in line)
            self.assertIn("median +3.0%", line)                       # the cold +50% is not in it
            self.assertIn("+50.0*", line)                             # but it is shown, flagged

    def test_runs_jsonl_gives_the_same_rows_and_the_same_comparison(self):
        import bench_record
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs.jsonl"
            for drift, t0 in ((1.0, 3000.0), (1.1, 6000.0)):
                for arm, factor, launch in (("as configured", 1.0, LAUNCH_A), ("arm b", 1.05, LAUNCH_B)):
                    rec = {"arm": arm, "engine_arg": list(BASE), "launch": launch, "start_epoch": t0 + (200 if arm == "arm b" else 0),
                           "decode_tps_median": 22.0 * drift * factor, "elapsed_s": 60,
                           "turns": [{"turn": i, "decode_tps": t * drift * factor, "ttft_s": 1.0} for i, t in enumerate(TURNS)]}
                    bench_record.append(path, rec)
            rows = S.collect_jsonl(str(path))
            self.assertEqual(len(rows), 4)
            self.assertEqual(len(rows[0]["turns"]), 4)
            res = S.interleaved(rows, "as configured")[0]
            self.assertAlmostEqual(res["deltas"][0][1], 5.0, places=1)
            out = summary_text(str(path))
            self.assertIn("per round, against the reference arm 'as configured'", out)


class WarmAxis(unittest.TestCase):
    """The warm-up belongs to each launch, not to the machine's boot (tools/check_cold_axis.py).

    Every arm reloads a 12 GiB model, so run 0 of EVERY launch is the slow one.  The summarizer has to drop it per arm,
    and the noise floor can then come out of the sweep's own repeats instead of a separate noise-floor run.
    """

    @staticmethod
    def write_multi(root: Path, arm: str, runs: list[list[float]], t0: float, launch: str) -> None:
        """One arm, several runs of the same turns: sessions are runs, and each turn carries its run number."""
        d = root / arm.replace(" ", "-")
        d.mkdir(parents=True, exist_ok=True)
        sessions = [[{"turn": i, "run": r, "ok": True, "decode_tps": t, "ttft_s": 1.0,
                      "prompt_tokens": 200, "generated_tokens": 100} for i, t in enumerate(tps)]
                    for r, tps in enumerate(runs)]
        body = {"elapsed_s": t0 + 60.0, "machine": {"launch": launch},
                "params": {"arm": arm, "engine_arg": list(BASE)},
                "results": {"phases": [{"name": "agent session", "from_s": t0}],
                            "agent": {"sessions": sessions},
                            "engine": {"per_test": {"agent session": {}}}}}
        (d / "results.json").write_text(json.dumps(body), encoding="utf-8")

    def test_run_zero_is_the_cold_sample_of_every_launch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # run 0 is slow in both arms; the flags differ, so the difference between the WARM medians is the finding
            self.write_multi(root, "as configured", [[16.0, 17.0], [24.0, 25.0]], 100.0, "strata --pack p --spec 4")
            self.write_multi(root, "flag x", [[16.0, 17.0], [26.0, 27.0]], 110.0, "strata --pack p --spec 4 --flag-x")
            rows = {r["arm"]: r for r in S.collect(str(root))}
            base, flag = rows["as configured"], rows["flag x"]
            self.assertAlmostEqual(base["decode"], 20.5, places=1)      # dragged down by the warm-up run
            self.assertAlmostEqual(base["decode_warm"], 24.5, places=1)  # the launch at temperature
            self.assertAlmostEqual(flag["decode_warm"], 26.5, places=1)
            self.assertEqual(base["warm_n"], 2)

    def test_a_single_run_arm_is_kept_as_measured(self):
        turns = [{"turn": 0, "run": 0, "ok": True, "decode_tps": 20.0}]
        kept, dropped = S.warm_turns(turns)
        self.assertFalse(dropped)
        self.assertEqual(kept, turns)

    def test_the_noise_floor_comes_from_the_sweeps_own_warm_repeats(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # three runs each: run 0 is half speed (the launch warming up), so it must not reach the floor, and only
            # runs 1 and 2 can be paired with each other.
            self.write_multi(root, "a", [[10.0, 10.0], [20.0, 20.0], [20.0, 20.0]], 100.0, "strata --pack p --spec 4")
            self.write_multi(root, "b", [[10.0, 10.0], [21.0, 19.0], [19.0, 21.0]], 1400.0,
                             "strata --pack p --spec 4 --flag-b")
            rows = S.collect(str(root))
            n = S.noise_within(rows)
            self.assertEqual(n["n"], 4, "one pair per turn per arm, warm runs only")
            self.assertAlmostEqual(n["pct"], 10.0, places=1)   # arm b's turns: 21 vs 19 over their mean
            self.assertLess(n["pct"], 25.0, "run 0 must not inflate the floor")
            # and the verdict line now has a figure without being handed a --noise folder
            out = summary_text(str(root))
            self.assertIn("noise floor from this sweep's own repeats", out)
            self.assertIn("inside noise", out)


if __name__ == "__main__":
    unittest.main()
