"""Regression cases found by the independent Lua-persistence review.

The admission fixture and all source changes stay in a temporary HOME. No
compositor socket or user's desktop configuration is contacted.
"""
import unittest
from unittest.mock import patch

import test_native_persistence as fixtures

service = fixtures.service


class NativeConfigReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativePersistenceTests("runTest")
        self.fixture.setUp()
        self.controller = self.fixture.controller
        self.source = self.fixture.source
        self.controller.mode = "spoiler"

    def tearDown(self):
        self.fixture.tearDown()

    def test_mode_change_during_appearance_apply_is_not_persisted_over(self):
        original = self.controller.native_configure
        before = self.source.read_bytes()

        def concurrent(value, expected):
            # A native dispatcher/Lua action does not share the Python flock.
            self.controller.mode = "omit"
            return original(value, expected)

        with patch.object(self.controller, "native_configure", side_effect=concurrent):
            try:
                result = self.controller.run("configure", appearance={"grain": 22})
            except service.Refused:
                self.assertEqual(self.source.read_bytes(), before)
                self.assertIn(self.controller.mode, ("omit", "black"))
            else:
                self.assertEqual(result["mode"], "omit")
                self.assertEqual(self.fixture.values()["mode"], "omit")

    def test_partial_patch_does_not_undo_new_unrequested_native_value(self):
        original = self.controller.apply_appearance
        before = self.source.read_bytes()

        def concurrent(value, persist=False, **kwargs):
            # The first CLI merge used white; a native color change arrives
            # before apply. Grain-only requests must not restore the old color.
            self.controller.appearance["color"] = "#123456"
            return original(value, persist=persist, **kwargs)

        with patch.object(self.controller, "apply_appearance", side_effect=concurrent):
            try:
                result = self.controller.run("configure", appearance={"grain": 22})
            except service.Refused:
                self.assertEqual(self.source.read_bytes(), before)
                self.assertEqual(self.controller.appearance["color"], "#123456")
            else:
                self.assertEqual(result["appearance"]["color"], "#123456")
                self.assertEqual(self.fixture.values()["color"], "#123456")

    def test_edit_during_backup_write_is_not_replaced(self):
        original = service.atomic_lua_settings
        edited = self.source.read_bytes() + b"\n-- newer user edit during backup fsync\n"

        def concurrent(path, contents, mode=0o600):
            result = original(path, contents, mode)
            if path.name == "last-settings.lua":
                self.source.write_bytes(edited)
            return result

        with patch.object(service, "atomic_lua_settings", side_effect=concurrent):
            with self.assertRaisesRegex(service.Refused, "changed"):
                self.controller.run("configure", appearance={"grain": 22})
        self.assertEqual(self.source.read_bytes(), edited)
        self.assertEqual(self.controller.mode, "black")


if __name__ == "__main__":
    unittest.main()
