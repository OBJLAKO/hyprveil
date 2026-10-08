import ctypes
import importlib.util
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import x11_guard


class X11GuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hyprveil-x11-guard-")
        self.runtime = Path(self.temporary.name)
        self.data = {"pid": 313, "wayland_display": "wayland-0"}
        self.marker = patch.object(x11_guard, "load_lab", return_value=(self.runtime, self.data))
        self.marker.start()
        self.environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime), "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ""})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.marker.stop()
        self.temporary.cleanup()

    def endpoint(self, pid=313, uid=None):
        endpoint = MagicMock()
        endpoint.family = socket.AF_UNIX
        endpoint.type = socket.SOCK_STREAM
        endpoint.getsockopt.return_value = struct.pack("3i", pid, os.getuid() if uid is None else uid, 0)
        endpoint.__enter__.return_value = endpoint
        return endpoint

    def test_inherited_main_display_refuses_before_touching_fd(self):
        with patch.dict(os.environ, {"DISPLAY": ":0"}), patch.object(x11_guard.os, "dup") as dup, self.assertRaisesRegex(RuntimeError, "inherited DISPLAY"):
            x11_guard.attest_fd(123, self.runtime)
        dup.assert_not_called()

    def test_exact_fd_peer_is_checked_for_lab_and_user(self):
        for pid, uid, allowed in ((313, os.getuid(), True), (999, os.getuid(), False), (313, os.getuid() + 1, True)):
            with self.subTest(pid=pid, uid=uid), patch.object(x11_guard.os, "dup", return_value=321), \
                 patch.object(x11_guard.socket, "socket", return_value=self.endpoint(pid, uid)), \
                 patch.object(x11_guard, "lab_peer", return_value=allowed):
                if pid == 313 and uid == os.getuid():
                    self.assertEqual(x11_guard.attest_fd(123, self.runtime)["peer_pid"], 313)
                else:
                    with self.assertRaisesRegex(RuntimeError, "peer is not"):
                        x11_guard.attest_fd(123, self.runtime)

    def test_global_main_lock_is_skipped_without_connecting_to_main_socket(self):
        main, own = self.runtime / ".X0-lock", self.runtime / ".X11-lock"
        main.write_text("999\n")
        own.write_text("313\n")
        endpoint = self.endpoint()
        with patch.object(x11_guard.Path, "glob", return_value=[main, own]), \
             patch.object(x11_guard, "lab_peer", side_effect=lambda runtime, pid: pid == 313), \
             patch.object(x11_guard.Path, "is_symlink", return_value=False), patch.object(x11_guard.Path, "is_socket", return_value=True), \
             patch.object(x11_guard.socket, "socket", return_value=endpoint) as create:
            connected, report = x11_guard.connect_lab(self.runtime)
        self.assertIs(connected, endpoint)
        self.assertEqual(report["display"], ":11")
        self.assertEqual(create.call_count, 1)
        endpoint.connect.assert_called_once_with("/tmp/.X11-unix/X11")

    def test_mismatching_socket_peer_is_closed_before_x11_handshake(self):
        lock = self.runtime / ".X11-lock"
        lock.write_text("313\n")
        endpoint = self.endpoint(pid=999)
        with patch.object(x11_guard.Path, "glob", return_value=[lock]), \
             patch.object(x11_guard, "lab_peer", side_effect=lambda runtime, pid: pid == 313), \
             patch.object(x11_guard.Path, "is_symlink", return_value=False), patch.object(x11_guard.Path, "is_socket", return_value=True), \
             patch.object(x11_guard.socket, "socket", return_value=endpoint), self.assertRaisesRegex(RuntimeError, "does not match"):
            x11_guard.connect_lab(self.runtime)
        endpoint.close.assert_called_once()

    def test_fixture_rejection_precedes_libxcb_loading(self):
        spec = importlib.util.spec_from_file_location("hyprveil_x11_fixture", ROOT / "tests/fixtures/x11.py")
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        with patch.object(fixture, "attest_fd", side_effect=RuntimeError("synthetic main FD refusal")), \
             patch.object(ctypes, "CDLL") as library, self.assertRaisesRegex(RuntimeError, "main FD refusal"):
            fixture.main(["--lab-dir", str(self.runtime), "--xcb-fd", "123"])
        library.assert_not_called()

    def test_non_socket_fixture_fd_does_not_leak_its_duplicate(self):
        # Python keeps fileno open if socket construction rejects a pipe.
        read_fd, write_fd = os.pipe()
        try:
            original = os.dup
            duplicates = []
            def record_duplicate(fd):
                duplicate = original(fd)
                duplicates.append(duplicate)
                return duplicate
            with patch.object(x11_guard.os, "dup", side_effect=record_duplicate), self.assertRaises(OSError):
                x11_guard.attest_fd(read_fd, self.runtime)
            self.assertEqual(len(duplicates), 1)
            with self.assertRaises(OSError):
                os.fstat(duplicates[0])
            os.fstat(read_fd)  # The caller's original FD still belongs to it.
        finally:
            os.close(read_fd)
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
