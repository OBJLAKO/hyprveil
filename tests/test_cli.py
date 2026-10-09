"""The Hyprpm helper works without a legacy installation or release receipt."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_native_persistence as fixtures

service = fixtures.service
spec = importlib.util.spec_from_file_location("hyprveil_native_cli", Path(__file__).resolve().parents[1] / "tools/cli.py")
cli = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"service": service}):
    spec.loader.exec_module(cli)


class NativeCLI(cli.Controller, fixtures.NativeController):
    def __init__(self, config):
        fixtures.NativeController.__init__(self, config)
        self.settings = {"abi_hash": service.TESTED_ABI}
        self.abi = service.TESTED_ABI


class NativeCLITests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativePersistenceTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.source = self.fixture.source
        self.controller = NativeCLI(self.fixture.fixture.config)
        self.controller.loaded = True
        # No manifest, installation receipt or managed release is consulted.
        self.fixture.fixture.config.unlink()

    def test_unloaded_status_requires_no_manifest_api_or_supported_abi(self):
        self.controller.loaded = False
        self.controller.abi = "new unsupported compositor"
        result = self.controller.run("status")
        self.assertFalse(result["loaded"])
        self.assertFalse(result["enabled"])
        self.assertFalse(result["load_supported"])
        self.assertIsNone(result["status"])
        self.assertTrue(result["persistence_supported"])
        self.assertEqual(self.controller.commands, [("-j", "plugin", "list")])

    def test_loaded_status_reports_actual_settings_without_writing(self):
        self.controller.mode = "spoiler"
        self.controller.appearance["grain"] = 71
        before = self.source.read_bytes()
        result = self.controller.run("status")
        self.assertTrue(result["loaded"])
        self.assertEqual(result["desired_mode"], "spoiler")
        self.assertEqual(result["appearance"]["grain"], 71)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.controller.state_path.exists())

    def test_unloaded_mutation_reports_hyprpm_activation_without_loading(self):
        self.controller.loaded = False
        with self.assertRaisesRegex(service.Refused, "hyprpm enable hyprveil"):
            self.controller.run("spoiler")
        self.assertFalse(any(args[0] in ("plugin", "eval", "hyprveil") for args in self.controller.commands))

    def test_unsupported_abi_refuses_before_native_mutation(self):
        self.controller.abi = "unsupported"
        with self.assertRaisesRegex(service.Refused, "ABI"):
            self.controller.run("spoiler")
        self.assertFalse(any(args[0] in ("eval", "hyprveil") for args in self.controller.commands))

    def test_legacy_native_api_refuses_before_mutation(self):
        original = self.controller.raw

        def legacy(*args):
            result = original(*args)
            if args == ("hyprveil", "status"):
                value = json.loads(result)
                value.pop("config_api")
                return json.dumps(value)
            return result

        with patch.object(self.controller, "raw", side_effect=legacy):
            with self.assertRaisesRegex(service.Refused, "API 1"):
                self.controller.run("spoiler")
        self.assertNotIn(("hyprveil", "spoiler"), self.controller.commands)
        self.assertFalse(any(args[0] == "eval" for args in self.controller.commands))

    def test_without_lua_settings_changes_are_explicitly_runtime_only(self):
        self.source.unlink()
        result = self.controller.run("spoiler")
        self.assertEqual(result["mode"], "spoiler")
        self.assertFalse(result["persisted"])
        self.assertFalse(self.source.exists())
        self.assertNotIn(("reload",), self.controller.commands)
        self.assertFalse(self.controller.run("status")["persistence_supported"])

    def test_partial_save_preserves_native_mode_appearance_and_custom_suffix(self):
        suffix = b"\n-- retained user code\n"
        self.source.write_bytes(self.source.read_bytes() + suffix)
        self.controller.mode = "spoiler"
        self.controller.appearance.update(color="#abcdef", grain=71)
        result = self.controller.run("configure", appearance={"speed": 0})
        self.assertTrue(result["persisted"])
        self.assertEqual(result["mode"], "spoiler")
        self.assertEqual(result["appearance"], dict(service.DEFAULT_APPEARANCE, color="#abcdef", grain=71, speed=0))
        self.assertTrue(self.source.read_bytes().endswith(suffix))
        self.assertEqual(service.parse_lua_settings(self.source.read_bytes())[3]["grain"], 71)
        self.assertFalse(self.fixture.fixture.config.exists())

    def test_custom_settings_refuse_before_mutation_but_allow_explicit_runtime(self):
        self.source.write_bytes(b"-- user Lua\nhl.plugin.hyprveil.configure({grain=73})\n")
        self.controller.mode = "spoiler"
        before = self.source.read_bytes()
        with self.assertRaisesRegex(service.Refused, "managed settings block"):
            self.controller.run("configure", appearance={"grain": 22})
        self.assertEqual(self.controller.mode, "spoiler")
        result = self.controller.run("configure", appearance={"grain": 22}, persist=False)
        self.assertFalse(result["persisted"])
        self.assertEqual(result["appearance"]["grain"], 22)
        self.assertEqual(self.source.read_bytes(), before)

    def test_concurrent_file_edit_survives_and_failed_save_returns_black(self):
        original = self.controller.native_configure
        concurrent = self.source.read_bytes() + b"\n-- concurrent edit\n"

        def changed(patch_value, expected):
            result = original(patch_value, expected)
            self.source.write_bytes(concurrent)
            return result

        with patch.object(self.controller, "native_configure", side_effect=changed):
            with self.assertRaisesRegex(service.Refused, "changed"):
                self.controller.run("configure", appearance={"grain": 22})
        self.assertEqual(self.source.read_bytes(), concurrent)
        self.assertEqual(self.controller.mode, "black")

    def test_invalid_patch_refuses_before_any_ipc(self):
        with self.assertRaises(service.Refused):
            self.controller.run("configure", appearance={"grain": True})
        self.assertEqual(self.controller.commands, [])

    def test_invalid_image_returns_black_without_saving_or_loading(self):
        self.controller.mode = "spoiler"
        before = self.source.read_bytes()
        with self.assertRaises(OSError):
            self.controller.run("image", image=self.root / "missing.png")
        self.assertEqual(self.controller.mode, "black")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(any(args[0] == "plugin" for args in self.controller.commands))

    def test_reload_uses_native_config_and_does_not_create_legacy_journal(self):
        self.assertEqual(self.controller.run("reload-config")["mode"], "omit")
        self.assertFalse(self.controller.state_path.exists())
        self.assertFalse(self.fixture.fixture.config.exists())

    def test_cli_keeps_partial_patch_and_runtime_flag_typed(self):
        with patch.object(cli, "Controller") as constructor, patch("sys.stdout", new_callable=io.StringIO):
            constructor.return_value.run.return_value = {"persisted": False}
            self.assertEqual(cli.main(["--signature", "synthetic-instance", "configure", "--runtime", "--grain", "0", "--eye", "off"]), 0)
        constructor.assert_called_once_with("synthetic-instance")
        constructor.return_value.run.assert_called_once_with("configure", None, {"grain": 0, "eye": False}, persist=False)

    def test_all_ten_materials_are_exposed_by_the_standalone_cli(self):
        for variant in service.APPEARANCE_VARIANTS:
            with self.subTest(variant=variant), patch.object(cli, "Controller") as constructor, \
                    patch("sys.stdout", new_callable=io.StringIO):
                constructor.return_value.run.return_value = {"persisted": False}
                self.assertEqual(cli.main(["configure", "--variant", variant, "--runtime"]), 0)
                constructor.return_value.run.assert_called_once_with("configure", None, {"variant": variant}, persist=False)


if __name__ == "__main__":
    unittest.main()
