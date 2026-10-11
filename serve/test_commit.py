import sys
import unittest

from serve import commit


class CommitTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(commit.classify(50, 100), "ok")
        self.assertEqual(commit.classify(93, 100), "low")        # 7 GiB left, under 8
        self.assertEqual(commit.classify(95, 100), "low")
        self.assertEqual(commit.classify(98, 100), "critical")   # 2 GiB left
        self.assertEqual(commit.classify(300, 400), "ok")        # 100 GiB left
        self.assertEqual(commit.classify(370, 400), "low")       # 30 GiB left but under 10% would need 40: 30<40

    def test_advice(self):
        self.assertEqual(commit.advice(None), "")
        self.assertEqual(commit.advice({"level": "ok"}), "")
        c = {"level": "critical", "used_gib": 98.0, "limit_gib": 100.0, "free_gib": 2.0, "pagefile_gib": 4.0}
        self.assertIn("pagefile", commit.advice(c))
        c["pagefile_gib"] = 64.0
        self.assertNotIn("pagefile", commit.advice(c))
        self.assertIn("resident-budget", commit.advice(c))

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_read_on_windows(self):
        c = commit.read()
        self.assertIsNotNone(c)
        self.assertGreater(c["limit_gib"], c["used_gib"] - 1e-6 if c["level"] == "ok" else 0)
        self.assertIn(c["level"], ("ok", "low", "critical"))


if __name__ == "__main__":
    unittest.main()
