"""The native-consent stand-in must fail closed outside its explicit lab."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).parents[1] / "tools"
with patch.object(sys, "path", [str(TOOLS), *sys.path]):
    spec = importlib.util.spec_from_file_location("permission_smoke", TOOLS / "permission_smoke.py")
    permission = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(permission)


class NativeConsentGuardTests(unittest.TestCase):
    def call_dialog(self, environment, allowed_runtime):
        with tempfile.TemporaryDirectory(prefix="hyprveil-dialog-guard-") as directory:
            root = Path(directory)
            script = root / "hyprland-dialog"
            script.write_text(permission.DIALOG.format(python=sys.executable))
            (root / "allowed-runtime").write_text(str(allowed_runtime))
            return subprocess.run([sys.executable, str(script), "--buttons", "Deny;Allow once"],
                env=environment, capture_output=True, text=True, timeout=2)

    def test_inherited_host_runtime_cannot_answer_a_permission_prompt(self):
        result = self.call_dialog({"XDG_RUNTIME_DIR": "/run/user/1000"}, "/tmp/hv-explicit")
        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stdout, "")

    def test_lab_variable_must_match_explicit_allowed_runtime(self):
        result = self.call_dialog({"XDG_RUNTIME_DIR": "/tmp/hv-explicit", "HYPRVEIL_LAB_RUNTIME": "/tmp/hv-other"},
                                  "/tmp/hv-explicit")
        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stdout, "")

    def test_matching_variables_without_a_real_lab_marker_cannot_grant(self):
        with tempfile.TemporaryDirectory(prefix="hv-dialog-no-marker-") as directory:
            result = self.call_dialog({"XDG_RUNTIME_DIR": directory, "HYPRVEIL_LAB_RUNTIME": directory}, directory)
            self.assertEqual(result.returncode, 71)
            self.assertEqual(result.stdout, "")
