"""Popup fixture file controls must stay local and preserve existing files."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("popup_fixture", Path(__file__).parent / "fixtures" / "popup.py")
popup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(popup)


class PopupControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="hyprveil-popup-guard-")
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_file_outside_lab_is_rejected(self):
        with self.assertRaises(ValueError):
            popup.lab_path(self.root, self.root.parent / "host-output", "unused")

    def test_symlink_outside_lab_is_rejected(self):
        link = self.root / "escape"
        link.symlink_to(self.root.parent)
        with self.assertRaises(ValueError):
            popup.lab_path(self.root, link / "host-output", "unused")

    def test_existing_control_file_is_not_replaced(self):
        path = self.root / "control.fifo"
        path.write_text("preserve me")
        with self.assertRaises(FileExistsError):
            popup.ControlFIFO(path)
        self.assertEqual(path.read_text(), "preserve me")

    def test_only_complete_command_lines_are_delivered(self):
        path = self.root / "control.fifo"
        fifo = popup.ControlFIFO(path)
        try:
            with path.open("wb", buffering=0) as writer:
                writer.write(b"popup-")
                self.assertEqual(fifo.read_commands(), [])
                writer.write(b"hide\nstatus\n")
                self.assertEqual(fifo.read_commands(), ["popup-hide", "status"])
        finally:
            fifo.close()
        self.assertFalse(path.exists())

    def test_cleanup_does_not_remove_replacement_file(self):
        path = self.root / "control.fifo"
        fifo = popup.ControlFIFO(path)
        path.unlink()
        path.write_text("new owner")
        fifo.close()
        self.assertEqual(path.read_text(), "new owner")


if __name__ == "__main__":
    unittest.main()
