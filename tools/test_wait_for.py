import contextlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wait_for as W  # noqa: E402


class Fake:
    """A clock and a sleep that move together, so the waits take no real time."""
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Waiting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "run.log")

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, text):
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(text)

    def test_nothing_to_wait_for(self):
        self.assertEqual(W.wait(), ("nothing", None))
        self.assertEqual(W.wait(file=self.log), ("nothing", None))

    def test_a_pattern_written_later_is_found_and_old_text_does_not_count(self):
        self.append("ALL DONE (an old run)\n")
        timer = threading.Timer(0.3, self.append, args=("working\nALL DONE now\n",))
        timer.start()
        reason, line = W.wait(self.log, r"ALL DONE", timeout=10, poll=0.05)
        timer.join()
        self.assertEqual((reason, line), ("pattern", "ALL DONE now"))

    def test_from_start_counts_what_is_already_there(self):
        self.append("x\nALL DONE\n")
        self.assertEqual(W.wait(self.log, r"ALL DONE", timeout=5, poll=0.01, from_start=True), ("pattern", "ALL DONE"))

    def test_a_log_that_does_not_exist_yet_is_waited_for(self):
        timer = threading.Timer(0.3, self.append, args=("Traceback (most recent call last)\n",))
        timer.start()
        reason, line = W.wait(self.log, r"Traceback|DONE", timeout=10, poll=0.05)
        timer.join()
        self.assertEqual(reason, "pattern")
        self.assertTrue(line.startswith("Traceback"))

    def test_timeout(self):
        f = Fake()
        self.assertEqual(W.wait(self.log, r"never", timeout=30, poll=5, clock=f.clock, sleep=f.sleep), ("timeout", None))
        self.assertGreaterEqual(f.t, 30)

    def test_a_process_ending_returns_before_the_pattern(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.4)"])
        try:
            reason, _ = W.wait(self.log, r"never", pid=p.pid, timeout=20, poll=0.05)
        finally:
            p.wait()
        self.assertEqual(reason, "exited")
        self.assertFalse(W.pid_alive(p.pid))

    def test_a_running_process_is_alive_and_a_nonsense_pid_is_not(self):
        self.assertTrue(W.pid_alive(os.getpid()))
        self.assertFalse(W.pid_alive(0))
        self.assertFalse(W.pid_alive(2 ** 22 - 3))

    def test_a_file_that_is_started_over_is_read_again(self):
        self.append("a long first line of text that is longer than what comes next\n")
        timer = threading.Timer(0.3, lambda: open(self.log, "w", encoding="utf-8").write("DONE\n"))
        timer.start()
        reason, _ = W.wait(self.log, r"DONE", timeout=10, poll=0.05)
        timer.join()
        self.assertEqual(reason, "pattern")

    def test_utf16_logs_are_read_as_utf16(self):
        with open(self.log, "wb") as f:
            f.write("start\nlaunch 1 done\n".encode("utf-16"))
        self.assertEqual(W.wait(self.log, r"launch 1 done", timeout=5, poll=0.01, from_start=True)[0], "pattern")
        self.assertEqual(W.tail_lines(self.log, 1), ["launch 1 done"])


class Command(unittest.TestCase):
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = W.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_the_tail_is_printed_with_the_matching_line_marked(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "r.log")
            open(log, "w", encoding="utf-8").write("one\ntwo\nthree\n")
            timer = threading.Timer(0.3, lambda: open(log, "a", encoding="utf-8").write("ALL DONE\nafter\n"))
            timer.start()
            rc, out, _ = self.run_main("--file", log, "--pattern", "ALL DONE", "--poll", "0.05", "--timeout", "10", "--tail", "3")
            timer.join()
        self.assertEqual(rc, 0)
        self.assertIn("[wait_for] matched", out)
        self.assertIn("> ALL DONE", out)
        self.assertIn("  after", out)

    def test_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "r.log")
            open(log, "w", encoding="utf-8").write("x\n")
            rc, out, _ = self.run_main("--file", log, "--pattern", "never", "--poll", "0.05", "--timeout", "0.3")
            self.assertEqual(rc, 2)
            self.assertIn("timed out", out)
        self.assertEqual(self.run_main()[0], 3)
        with self.assertRaises(SystemExit):
            self.run_main("--pattern", "x")                                         # a pattern needs a file


if __name__ == "__main__":
    unittest.main()
