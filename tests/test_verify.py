"""Tests for the privacy oracle and fixture guard, without a compositor."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cairo


ROOT = Path(__file__).parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = load("verify_capture", ROOT / "verify_capture.py")
client = load("fixture_client", ROOT / "fixtures" / "client.py")
PRIVATE = (232, 64, 144)
BACKGROUND = (36, 132, 196)


class CaptureOracleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="hyprveil-oracle-")
        self.root = Path(self.directory.name)
        self.rect = (0, 0, 4, 4)

    def tearDown(self):
        self.directory.cleanup()

    def image(self, name, color, leak=False, alpha=False):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32 if alpha else cairo.FORMAT_RGB24, 4, 4)
        context = cairo.Context(surface)
        context.set_source_rgb(*(component / 255 for component in color))
        context.paint()
        if leak:
            context.set_source_rgb(*(component / 255 for component in PRIVATE))
            context.rectangle(0, 0, 1, 1)
            context.fill()
        path = self.root / name
        surface.write_to_png(str(path))
        return verify.Image(path)

    def test_revealed_underlay_matches_and_contains_no_secret(self):
        local = self.image("local.png", PRIVATE)
        capture = self.image("capture.png", BACKGROUND, alpha=True)
        underlay = self.image("underlay.png", BACKGROUND)
        self.assertEqual(verify.color_fraction(local, self.rect, PRIVATE, 3), 1)
        self.assertEqual(verify.color_fraction(capture, self.rect, PRIVATE, 3), 0)
        self.assertEqual(verify.differing_fraction(capture, self.rect, underlay, self.rect, 3), 0)

    def test_black_box_does_not_count_as_revealed_underlay(self):
        capture = self.image("black.png", (0, 0, 0))
        underlay = self.image("underlay.png", BACKGROUND)
        self.assertEqual(verify.color_fraction(capture, self.rect, PRIVATE, 3), 0)
        self.assertEqual(verify.differing_fraction(capture, self.rect, underlay, self.rect, 3), 1)

    def test_transparent_black_is_not_an_opaque_denial_frame(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 4, 4)
        path = self.root / "transparent-black.png"
        surface.write_to_png(str(path))
        image = verify.Image(path)
        self.assertEqual(verify.color_fraction(image, self.rect, (0, 0, 0), 0), 1)
        self.assertEqual(verify.opacity_fraction(image, self.rect), 0)

    def test_one_transparent_pixel_invalidates_full_opacity(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 4, 4)
        context = cairo.Context(surface)
        context.set_source_rgb(0, 0, 0)
        context.paint()
        context.set_operator(cairo.OPERATOR_CLEAR)
        context.rectangle(0, 0, 1, 1)
        context.fill()
        path = self.root / "one-transparent.png"
        surface.write_to_png(str(path))
        self.assertEqual(verify.opacity_fraction(verify.Image(path), self.rect), 15 / 16)

    def test_rgb_black_denial_frame_is_opaque(self):
        image = self.image("opaque-black.png", (0, 0, 0))
        self.assertEqual(verify.opacity_fraction(image, self.rect), 1)

    def test_one_leaking_pixel_is_detected(self):
        capture = self.image("leak.png", BACKGROUND, leak=True)
        self.assertEqual(verify.color_fraction(capture, self.rect, PRIVATE, 3), 1 / 16)

    def test_full_frame_oracle_rejects_secret_outside_tested_mask_region(self):
        capture = self.image("outside-leak.png", BACKGROUND, leak=True)
        # An ordinary mask ROI succeeds while a single pixel elsewhere leaks.
        self.assertEqual(verify.color_fraction(capture, (1, 1, 3, 3), BACKGROUND, 0), 1)
        report = verify.absence_report(capture, PRIVATE)
        self.assertFalse(report["ok"])
        self.assertEqual(report["forbidden_fraction"], 1 / 16)

    def test_full_frame_oracle_rejects_near_color_leak(self):
        capture = self.image("near-leak.png", (230, 67, 143))
        self.assertFalse(verify.absence_report(capture, PRIVATE, tolerance=3)["ok"])
        self.assertTrue(verify.absence_report(capture, PRIVATE, tolerance=0)["ok"])

    def test_out_of_bounds_region_fails(self):
        image = self.image("solid.png", BACKGROUND)
        with self.assertRaises(ValueError):
            verify.color_fraction(image, (3, 3, 2, 2), BACKGROUND, 0)

    def test_geometry_change_is_detected(self):
        before = {"address": "0xabc", "class": "org.hyprveil.fixture.protected",
                  "at": [100, 100], "size": [640, 480], "workspace": {"id": 1, "name": "1"},
                  "monitor": 0, "floating": True, "fullscreen": 0}
        after = dict(before, at=[101, 100])
        before_path, after_path = self.root / "before.json", self.root / "after.json"
        before_path.write_text(json.dumps([before]))
        after_path.write_text(json.dumps([after]))
        report = verify.geometry_report(before_path, after_path, before["class"])
        self.assertFalse(report["ok"])
        self.assertEqual(list(report["changed"]), ["at"])
        after_path.write_text(json.dumps([before]))
        self.assertTrue(verify.geometry_report(before_path, after_path, before["class"])["ok"])

    def test_ambiguous_window_selection_fails(self):
        path = self.root / "ambiguous.json"
        path.write_text(json.dumps([{"class": "private"}, {"class": "private"}]))
        with self.assertRaises(ValueError):
            verify.geometry_report(path, path, "private")


class FixtureGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="hyprveil-guard-")
        self.root = Path(self.directory.name)
        self.display = "wayland-fixture-test"
        # The guard only checks filesystem socket metadata; no test opens a
        # display connection. Socket creation is prohibited in Codex's sandbox.
        self.socket_metadata = patch.object(Path, "is_socket", return_value=True)
        self.socket_metadata.start()
        (self.root / ".hyprveil-lab").write_text(json.dumps({
            "runtime_dir": str(self.root), "wayland_display": self.display}))
        self.environment = {"XDG_RUNTIME_DIR": str(self.root), "WAYLAND_DISPLAY": self.display}

    def tearDown(self):
        self.socket_metadata.stop()
        self.directory.cleanup()

    def test_explicit_lab_socket_sets_native_wayland(self):
        with patch.dict(os.environ, self.environment, clear=True):
            client.lab_environment(self.root, self.display)
            self.assertEqual(os.environ["GDK_BACKEND"], "wayland")

    def test_inherited_host_wayland_socket_is_rejected(self):
        with patch.dict(os.environ, dict(self.environment, WAYLAND_DISPLAY="wayland-0"), clear=True):
            with self.assertRaises(ValueError):
                client.lab_environment(self.root, self.display)

    def test_inherited_x11_display_is_rejected(self):
        with patch.dict(os.environ, dict(self.environment, DISPLAY=":0"), clear=True):
            with self.assertRaises(ValueError):
                client.lab_environment(self.root, self.display)

    def test_non_socket_marker_target_is_rejected(self):
        with patch.dict(os.environ, self.environment, clear=True), patch.object(Path, "is_socket", return_value=False):
            with self.assertRaises(ValueError):
                client.lab_environment(self.root, self.display)


if __name__ == "__main__":
    unittest.main()
