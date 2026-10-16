import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_state as B  # noqa: E402


def sample(sm, temp, power, ram, throttle=(), busy=()):
    return {"gpu": {"sm_mhz": sm, "temp_c": temp, "power_w": power, "throttle_reasons": list(throttle)}, "free_ram_gib": ram,
            "background_busy": list(busy)}


class Summaries(unittest.TestCase):
    def test_what_the_samples_say(self):
        s = B.summarize_samples([sample(2800, 60, 180.0, 6.0), sample(2400, 71, 215.5, 3.5, ["sw thermal"], [("msmpeng.exe", 42)]),
                                 sample(2700, 65, 200.0, 5.0, [], [("msmpeng.exe", 20), ("tiworker.exe", 33)])])
        self.assertEqual(s["samples"], 3)
        self.assertEqual(s["sm_mhz"], {"min": 2400, "max": 2800, "mean": 2633})
        self.assertEqual((s["temp_c_max"], s["power_w_max"], s["free_ram_gib_min"]), (71, 215.5, 3.5))
        self.assertEqual(s["throttle_seen"], ["sw thermal"])
        self.assertEqual(s["background_busy"], {"msmpeng.exe": 42, "tiworker.exe": 33})          # the busiest each was, not the last

    def test_missing_readings_are_left_out_not_invented(self):
        s = B.summarize_samples([{"gpu": None, "free_ram_gib": None}, {"gpu": {"sm_mhz": None}, "free_ram_gib": 4.0}])
        self.assertIsNone(s["sm_mhz"])
        self.assertIsNone(s["temp_c_max"])
        self.assertEqual(s["free_ram_gib_min"], 4.0)
        self.assertEqual(B.summarize_samples([])["samples"], 0)

    def test_idle_is_not_throttling(self):
        self.assertEqual(B.throttle_names(0x1), [])
        self.assertEqual(B.throttle_names(0x1 | 0x20 | 0x4), ["sw power cap", "sw thermal"])
        self.assertEqual(B.throttle_names(None), [])


class Snapshot(unittest.TestCase):
    def test_a_snapshot_never_raises_and_has_every_field(self):
        s = B.snapshot(background=False)
        for key in ("time", "uptime_s", "free_ram_gib", "gpu"):
            self.assertIn(key, s)
        self.assertGreater(s["time"], 1e9)

    def test_no_gpu_library_gives_none(self):
        with mock.patch.object(B, "_nvml", return_value=None):
            self.assertIsNone(B.gpu_state())
            self.assertIsNone(B.snapshot(background=False)["gpu"])

    def test_a_failing_reading_is_swallowed(self):
        lib = mock.Mock()
        lib.nvmlDeviceGetHandleByIndex_v2.side_effect = OSError("driver went away")
        with mock.patch.object(B, "_nvml", return_value=lib):
            self.assertIsNone(B.gpu_state())


class Sampling(unittest.TestCase):
    def test_the_sampler_collects_and_stops(self):
        calls = []

        def fake(background=True):
            calls.append(1)
            return sample(2500, 60, 150.0, 5.0)
        with mock.patch.object(B, "snapshot", fake):
            s = B.Sampler(interval=0.02).start()
            time.sleep(0.25)
            summary = s.stop()
        self.assertGreaterEqual(summary["samples"], 3)
        self.assertEqual(summary["sm_mhz"]["max"], 2500)
        n = len(calls)
        time.sleep(0.1)
        self.assertEqual(len(calls), n)                                                         # stopped

    def test_an_exception_in_a_snapshot_does_not_end_the_sampler(self):
        n = [0]

        def flaky(background=True):
            n[0] += 1
            if n[0] == 1:
                raise RuntimeError("boom")
            return sample(2500, 60, 150.0, 5.0)
        with mock.patch.object(B, "snapshot", flaky):
            s = B.Sampler(interval=0.02).start()
            time.sleep(0.2)
            summary = s.stop()
        self.assertGreaterEqual(summary["samples"], 2)


if __name__ == "__main__":
    unittest.main()
