import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import read_window_profile as R  # noqa: E402

TIMING = ("strata decode timing: 26 windows, avg T 2.62, 2.04 tokens/window, 50.00 ms/window = verify 44.27 (GPU-reach wait 18.33 + "
          "per-layer host 24.68 [plan 0.13 actq 0.21 jobs 1.71 CPU 22.60] + stage 0.01) + commit/emit 0.12 + draft 2.12; per layer-window: "
          "CPU experts 4.25 (5.79 entries), VRAM hits 14.02, PCIe 2.53; adapt wait 3.46 join 1.30, 12.9 swaps/window")
STAGES = ("strata decode GPU stages (ms/window): GDN layers: router+plan+bell 0.46 waitA 0.20 VRAM hits 1.53 waitB 20.00 PCIe grp 0.46 "
          "waitCPU 2.00 copy+combine 0.45 | QSA layers: waitA 0.05 waitB 5.00 waitCPU 0.50 | total 56.47 ms/window over 26 windows")


def run(text):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            rc = R.main([f.name])
    finally:
        os.unlink(f.name)
    return rc, out.getvalue()


class ReadWindowProfile(unittest.TestCase):
    def test_the_waits_add_gdn_and_qsa_and_are_shared_over_the_window(self):
        rc, out = run(TIMING + "\n" + STAGES + "\n")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"waitA +median +0\.25 .*share +0\.5%")          # 0.20 + 0.05 of a 50 ms window
        self.assertRegex(out, r"waitB +median +25\.00 .*share +50\.0%")
        self.assertRegex(out, r"waitCPU +median +2\.50 .*share +5\.0%")
        self.assertRegex(out, r"all three +median +27\.75 .*share +55\.5%")
        self.assertIn("1 requests", out)

    def test_a_stage_name_inside_another_is_not_matched(self):
        # "VRAM hits" appears in the timing line too, and "waitB" must not be read out of a longer name
        self.assertEqual(R.stage("GDN layers: xwaitB 9.00 waitB 1.00", "GDN", "waitB"), 1.0)

    def test_a_log_without_both_lines_is_refused(self):
        rc, _ = run(TIMING + "\n")
        self.assertEqual(rc, 1)


    def test_the_submission_counts_are_read_and_the_ratio_is_the_reading(self):
        # the instrumented build stamps the submissions per window; the counts are exact, and blobs per
        # submission is what proves which path ran - ~1 is the loop, far more is the batch.
        rc, out = run(TIMING + "\n" + STAGES + "\n"
                      "strata decode submissions (per window): 4 submissions, 9 blobs, 3 batched\n")
        self.assertEqual(rc, 0)
        self.assertIn("what the copy path handed to the runtime", out)
        self.assertRegex(out, r"submissions/window +median +4\.00")
        self.assertRegex(out, r"blobs/window +median +9\.00")
        self.assertRegex(out, r"batched submissions/window +median +3\.00")
        self.assertRegex(out, r"blobs per submission +median +2\.25 .*~1 is the loop")

    def test_a_log_that_never_stamped_submissions_prints_nothing_about_them(self):
        # the previous batch's binary did not stamp this line; a reader that invented a section for a log
        # that never printed it would read a zero as a finding
        rc, out = run(TIMING + "\n" + STAGES + "\n")
        self.assertEqual(rc, 0)
        self.assertNotIn("handed to the runtime", out)


if __name__ == "__main__":
    unittest.main()
