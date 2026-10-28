import os
import subprocess
import sys
import unittest

from demos.launcher.supervisor import process_start


class ProcessStartTests(unittest.TestCase):
    def test_process_start_is_stable(self):
        first = process_start(os.getpid())
        self.assertTrue(first)
        self.assertEqual(process_start(os.getpid()), first)

    def test_reaped_child_has_no_start(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertIsNone(process_start(child.pid))


if __name__ == "__main__":
    unittest.main()
