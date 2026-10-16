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


if __name__ == "__main__":
    unittest.main()
