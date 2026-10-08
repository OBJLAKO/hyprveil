"""Concurrent report publication must not follow links or mix writer data."""
import concurrent.futures
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("lab_io", Path(__file__).resolve().parents[1] / "tools/lab.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


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
