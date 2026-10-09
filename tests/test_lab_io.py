"""Concurrent report publication must not follow links or mix writer data."""
import concurrent.futures
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("lab_io", Path(__file__).resolve().parents[1] / "tools/lab.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class LabAdmissionEnvironmentTests(unittest.TestCase):
    def test_standard_admission_omits_only_plugin_lab_marker(self):
        runtime = Path("/tmp/hv-synthetic-environment")
        inherited = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "DISPLAY": ":0",
                     "WAYLAND_DISPLAY": "host-display", "HYPRLAND_INSTANCE_SIGNATURE": "host-instance",
                     "DBUS_SESSION_BUS_ADDRESS": "host-bus", "HOME": "/host/home", "SECRET": "keep-out"}
        with patch.dict(lab.os.environ, inherited, clear=True):
            diagnostic = lab.compositor_env(runtime)
            standard = lab.compositor_env(runtime, standard_plugin_admission=True)
        self.assertEqual(diagnostic, dict(standard, HYPRVEIL_LAB_RUNTIME=str(runtime)))
        self.assertEqual(standard["LIBSEAT_BACKEND"], "hyprveil-disabled")
        self.assertEqual(standard["XDG_RUNTIME_DIR"], str(runtime))
        self.assertEqual(standard["HOME"], str(runtime / "home"))
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "DBUS_SESSION_BUS_ADDRESS", "SECRET"):
            self.assertNotIn(key, standard)

    def test_fixture_environment_retains_explicit_lab_identity(self):
        runtime = Path("/tmp/hv-synthetic-environment")
        data = {"wayland_display": "wayland-synthetic", "signature": "synthetic-instance",
                "standard_plugin_admission": True}
        fixtures = lab.lab_env(runtime, data)
        self.assertEqual(fixtures["HYPRVEIL_LAB_RUNTIME"], str(runtime))
        self.assertEqual(fixtures["WAYLAND_DISPLAY"], data["wayland_display"])
        self.assertEqual(fixtures["HYPRLAND_INSTANCE_SIGNATURE"], data["signature"])


class LabPublicationTests(unittest.TestCase):
    def test_concurrent_writers_publish_complete_private_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "latest-lab.json"
            def publish(index):
                payload = {"writer": index, "padding": str(index) * 4096}
                for _ in range(4):
                    lab.write_json(path, payload)
                    observed = json.loads(path.read_text())
                    self.assertEqual(observed["padding"], str(observed["writer"]) * 4096)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as workers:
                list(workers.map(publish, range(16)))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse(list(Path(directory).glob(".hyprveil-*.tmp")))

    def test_predictable_temporary_link_does_not_modify_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "unrelated"
            target.write_text("keep")
            target.chmod(0o640)
            (root / "latest-lab.tmp").symlink_to(target)
            lab.write_json(root / "latest-lab.json", {"ready": True})
            self.assertEqual(target.read_text(), "keep")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_directory_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real").mkdir()
            (root / "link").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaises(OSError):
                lab.write_json(root / "link/state.json", {"ready": True})
            self.assertFalse((root / "real/state.json").exists())


if __name__ == "__main__":
    unittest.main()
