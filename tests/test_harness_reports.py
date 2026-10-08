"""A successful rendering check must not hide an unfinished lab cleanup."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(__file__).parents[1] / "tools"
with patch.object(sys, "path", [str(TOOLS), *sys.path]):
    spec = importlib.util.spec_from_file_location("stress", TOOLS / "stress.py")
    stress = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stress)


class CleanupReportTests(unittest.TestCase):
    def run_object(self):
        run = stress.Stress(SimpleNamespace())
        run.launcher = Mock()
        run.launcher.poll.return_value = 0
        run.save = Mock()
        run.report["ok"] = True
        return run

    def test_unload_failure_invalidates_otherwise_passing_report(self):
        run = self.run_object()
        run.loaded = True
        run.unload = Mock(side_effect=RuntimeError("synthetic unload failure"))
        run.cleanup()
        self.assertFalse(run.report["ok"])
        self.assertEqual(run.report["cleanup_errors"], ["synthetic unload failure"])
        self.assertTrue(run.report["lab_stopped"])

    def test_exited_launcher_cannot_prove_compositor_stopped(self):
        run = self.run_object()
        # This process is deliberately alive; cleanup performs no IPC or
        # signals when its mocked launcher is already exited.
        run.state = {"pid": os.getpid()}
        run.cleanup()
        self.assertFalse(run.report["lab_stopped"])
        self.assertFalse(run.report["ok"])


if __name__ == "__main__":
    unittest.main()
