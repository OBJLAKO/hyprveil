"""Real subprocess regressions for bounded compositor IPC output and teardown."""
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_service import service


class BoundedIPCTests(unittest.TestCase):
    def command(self, source, **options):
        return service.bounded_command([sys.executable, "-c", source],
            env={"PATH": "/usr/bin:/bin"}, timeout=options.pop("timeout", 2), **options)

    def test_interleaved_stdout_stderr_and_nonzero_status_are_preserved(self):
        value = self.command("import os; os.write(1,b'hello'); os.write(2,b'error'); raise SystemExit(7)")
        self.assertEqual((value.returncode, value.stdout, value.stderr), (7, "hello", "error"))

    def test_no_newline_stdout_is_bounded_and_process_is_reaped(self):
        self.assert_refused_and_reaped("import os; os.write(1,b'x'*1000000)", service.Refused, limit=1024)

    def test_stderr_shares_the_same_budget(self):
        self.assert_refused_and_reaped("import os; os.write(1,b'x'*600); os.write(2,b'y'*600)", service.Refused, limit=1024)

    def test_timeout_kills_and_reaps_a_child_ignoring_sigterm(self):
        self.assert_refused_and_reaped("import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)",
                                      subprocess.TimeoutExpired, timeout=0.1)

    def test_exact_byte_limit_without_newline_is_accepted(self):
        self.assertEqual(len(self.command("import os; os.write(1,b'x'*1024)", limit=1024).stdout), 1024)

    def assert_refused_and_reaped(self, source, error, **options):
        children = []
        spawn = subprocess.Popen
        def remember(*args, **kwargs):
            child = spawn(*args, **kwargs)
            children.append(child)
            return child
        with patch.object(service.subprocess, "Popen", side_effect=remember):
            with self.assertRaises(error):
                self.command(source, **options)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())


if __name__ == "__main__":
    unittest.main()
