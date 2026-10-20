"""Tests for subsync_repair/external/process.py: the external tools run without standard input, and a timeout ends every
process they started."""
import subprocess
import sys
import time
import unittest

from subsync_repair.external import process
from support import TempFolder

CHILD_WRITES_LATER = """
import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import time, sys; time.sleep(1.5); open(sys.argv[1], 'w').write('alive')",
                  sys.argv[1]])
import time
time.sleep(30)
"""


class Run(TempFolder):
    def test_output_and_exit_code_come_back_and_there_is_no_standard_input(self):
        r = process.run([sys.executable, "-c", "import sys; print(sys.stdin.read() == ''); sys.exit(3)"], 30)
        self.assertEqual((r.returncode, r.stdout.strip()), (3, "True"))

    def test_a_timeout_kills_the_tool_and_every_process_it_started(self):
        marker = self.tmp / "grandchild.txt"
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            process.run([sys.executable, "-c", CHILD_WRITES_LATER, marker], 0.5)
        self.assertLess(time.monotonic() - started, 10)
        time.sleep(2.0)
        self.assertFalse(marker.exists(), "a process the tool started outlived the timeout")

    def test_a_tool_that_does_not_exist_is_an_os_error(self):
        with self.assertRaises(OSError):
            process.run([str(self.tmp / "no-such-tool")], 5)

    def test_the_tail_prefers_the_error_output(self):
        r = subprocess.CompletedProcess([], 1, "out" * 200, "err")
        self.assertEqual(process.tail(r), "err")
        self.assertEqual(len(process.tail(subprocess.CompletedProcess([], 1, "x" * 1000, ""))), process.OUTPUT_TAIL)


if __name__ == "__main__":
    unittest.main()
