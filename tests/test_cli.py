"""The Hyprpm helper works without a legacy installation or release receipt."""
import importlib.util
import io
import json
import os
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

    def test_color_fill_does_not_change_spoiler_tint_and_black_remains_black(self):
        self.controller.appearance["color"] = "#aabbcc"
        colored = self.controller.run("color", appearance={"mask_color": "#1A2B3C"})
        self.assertEqual((colored["mode"], colored["mask_color"], colored["appearance"]["color"]),
                         ("color", "#1a2b3c", "#aabbcc"))
        black = self.controller.run("black")
        self.assertEqual(black["mode"], "black")
        self.assertEqual(black["mask_color"], "#1a2b3c")
        self.assertNotIn(("reload",), self.controller.commands)

    def test_render_status_fields_are_top_level_in_both_envelopes(self):
        self.controller.render.update(mask_color="#123456", image_fit="tile")
        result = self.controller.run("status")
        for key, value in self.controller.render.items():
            self.assertEqual(result[key], value)
            self.assertEqual(result["status"][key], value)
            self.assertNotIn(key, result["appearance"])
        self.assertEqual(result["image_path"], result["status"]["image_path"])

    def test_partial_image_fit_preserves_mode_image_path_and_other_values(self):
        image = self.root / "retained.png"
        image.write_bytes(fixtures.fixtures.png())
        self.controller.run("image", image=image)
        self.controller.appearance["grain"] = 81
        for fit in service.IMAGE_FITS:
            result = self.controller.run("configure", appearance={"image_fit": fit, "mask_color": "#123456"})
            self.assertEqual((result["mode"], result["image_path"], result["image_fit"]), ("image", str(image), fit))
            self.assertEqual(result["appearance"]["grain"], 81)
        saved = service.parse_lua_settings(self.source.read_bytes())[3]
        self.assertEqual(saved["mask_color"], "#123456")
        self.assertNotIn(("reload",), self.controller.commands)

    def test_custom_shader_text_is_saved_without_compiling_or_executing_it(self):
        shader = self.root / "a custom.FRAG"
        shader.write_text("this is intentionally invalid GLSL, not executable Lua\n")
        result = self.controller.run("spoiler", appearance={"shader_path": str(shader)})
        self.assertEqual(result["mode"], "spoiler")
        self.assertEqual(result["shader_path"], str(shader))
        self.assertEqual(service.parse_lua_settings(self.source.read_bytes())[3]["shader_path"], str(shader))
        self.assertNotIn(("reload",), self.controller.commands)
        cleared = self.controller.run("configure", appearance={"shader_path": ""})
        self.assertEqual(cleared["shader_path"], "")

    def test_custom_icon_patch_is_atomic_and_can_be_cleared_with_builtin_selection(self):
        image = self.root / "icon with spaces"
        image.write_bytes(fixtures.fixtures.png())
        result = self.controller.run("configure", appearance={"icon": "custom", "icon_path": str(image), "icon_opacity": 31})
        self.assertEqual((result["appearance"]["icon"], result["icon_path"], result["appearance"]["icon_opacity"]),
                         ("custom", str(image), 31))
        cleared = self.controller.run("configure", appearance={"icon": "none", "icon_path": ""})
        self.assertEqual((cleared["appearance"]["icon"], cleared["icon_path"]), ("none", ""))

    def test_removed_retained_assets_do_not_prevent_black_fallback(self):
        shader = self.root / "removed.frag"
        shader.write_text("invalid GLSL but valid source text")
        icon = self.root / "removed.png"
        icon.write_bytes(fixtures.fixtures.png())
        self.controller.run("configure", appearance={"shader_path": str(shader), "icon": "custom", "icon_path": str(icon)})
        shader.unlink()
        icon.unlink()
        self.assertEqual(self.controller.run("black")["mode"], "black")

    def test_incomplete_direct_lua_icon_configuration_does_not_block_black(self):
        # Native registry assignments arrive separately; a user can select
        # custom before assigning its path. Safety mode must remain available.
        self.controller.mode = "color"
        self.controller.appearance["icon"] = "custom"
        self.controller.render.update(mask_color="#123456", icon_path="")
        self.assertEqual(self.controller.run("black")["mode"], "black")

    def test_new_render_patches_are_refused_before_eval_on_old_native_api(self):
        current = self.controller.native("status")
        old = {key: value for key, value in current.items() if key not in service.DEFAULT_RENDER}
        for patch_value in ({"image_fit": "fit"}, {"mask_color": "#123456"}, {"shader_path": ""},
                            {"icon_path": ""}, {"mode": "color"}, {"icon": "custom"}):
            with self.subTest(patch=patch_value), self.assertRaisesRegex(service.Refused, "does not support"):
                service.Controller.native_configure(self.controller, patch_value, old)
        self.assertFalse(any(args[0] == "eval" for args in self.controller.commands))

    def test_invalid_new_patch_types_are_refused_before_ipc(self):
        for key, value in (("mask_color", []), ("mask_color", "#fff"), ("image_fit", {}), ("image_fit", "cover"),
                           ("shader_path", "relative.frag"), ("shader_path", "/tmp/a.vert"),
                           ("icon_path", False), ("image_path", "/tmp/a\nb.png"),
                           ("shader_path", "/tmp/" + chr(0xd800) + ".frag")):
            with self.subTest(key=key, value=value), self.assertRaises(service.Refused):
                self.controller.run("configure", appearance={key: value})
        self.assertEqual(self.controller.commands, [])

    def test_new_asset_preflight_rejects_links_fifo_oversize_and_binary_shader(self):
        shader = self.root / "safe.frag"
        shader.write_text("void main() {}")
        self.assertEqual(self.controller.safe_shader(str(shader)), shader)
        link = self.root / "linked.frag"
        link.symlink_to(shader)
        parent = self.root / "linked-parent"
        parent.symlink_to(self.root, target_is_directory=True)
        fifo = self.root / "pipe.frag"
        os.mkfifo(fifo)
        for path in (link, parent / shader.name, fifo, self.root / "missing.frag"):
            with self.subTest(path=path), self.assertRaises((OSError, service.Refused)):
                self.controller.safe_shader(str(path))
        for content in (b"\x00", b"\xff", b"x" * (64 * 1024 + 1)):
            shader.write_bytes(content)
            with self.subTest(content_size=len(content)), self.assertRaises(service.Refused):
                self.controller.safe_shader(str(shader))

    def test_real_native_cas_compares_new_top_level_fields_and_normalizes_mask_color(self):
        current = self.controller.native("status")
        emitted = []
        original = self.controller.raw

        def commit(*args):
            if args[0] == "eval":
                emitted.append(args[1])
                self.controller.render.update(mask_color="#aabbcc", image_fit="fit")
                return "ok"
            return original(*args)

        with patch.object(self.controller, "raw", side_effect=commit):
            result = service.Controller.native_configure(self.controller, {"mask_color": "#AABBCC", "image_fit": "fit"}, current)
        self.assertEqual(result["mask_color"], "#aabbcc")
        self.assertIn('s.mask_color == "#000000"', emitted[0])
        self.assertIn('s.image_fit == "stretch"', emitted[0])
        self.assertIn('mask_color="#aabbcc"', emitted[0])
        self.assertNotIn("s.appearance.mask_color", emitted[0])

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
        self.assertNotIn(("reload",), self.controller.commands)

    def test_partial_icon_save_keeps_mode_image_and_other_appearance_without_reload(self):
        self.controller.mode = "omit"
        self.controller.image_path = str(self.root / "retained.png")
        self.controller.appearance.update(variant="matrix", color="#234567", grain=32, speed=17, darkness=68,
                                          eye=False, eye_size=64, icon="shield", icon_opacity=29)
        expected = service.native_values(self.controller.native("status"))
        expected["icon_opacity"] = 53
        result = self.controller.run("configure", appearance={"icon_opacity": 53})
        self.assertTrue(result["persisted"])
        self.assertEqual(service.native_values(result), expected)
        self.assertEqual(service.parse_lua_settings(self.source.read_bytes())[3], expected)
        self.assertNotIn(("reload",), self.controller.commands)

    def test_noop_save_stays_persisted_without_writes_or_global_reload(self):
        self.controller.apply_lua()
        before = self.source.read_bytes()
        info = self.source.stat()
        with patch.object(service, "atomic_lua_settings", wraps=service.atomic_lua_settings) as writer:
            result = self.controller.run("configure", appearance={})
        self.assertTrue(result["persisted"])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual((self.source.stat().st_ino, self.source.stat().st_mtime_ns), (info.st_ino, info.st_mtime_ns))
        writer.assert_not_called()
        self.assertNotIn(("reload",), self.controller.commands)

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

    def test_cli_exposes_render_fields_without_expanding_appearance(self):
        for argv, action, image, patch_value in (
            (["color", "#ABCDEF", "--runtime"], "color", None, {"mask_color": "#ABCDEF"}),
            (["image", "/tmp/example.png", "--fit", "fit", "--runtime"], "image", Path("/tmp/example.png"), {"image_fit": "fit"}),
            (["spoiler", "--shader-path", "", "--runtime"], "spoiler", None, {"shader_path": ""}),
            (["configure", "--icon", "custom", "--icon-path", "/tmp/icon.png", "--image-fit", "tile", "--runtime"],
             "configure", None, {"icon": "custom", "icon_path": "/tmp/icon.png", "image_fit": "tile"}),
        ):
            with self.subTest(argv=argv), patch.object(cli, "Controller") as constructor, patch("sys.stdout", new_callable=io.StringIO):
                constructor.return_value.run.return_value = {"persisted": False}
                self.assertEqual(cli.main(argv), 0)
                constructor.return_value.run.assert_called_once_with(action, image, patch_value, persist=False)

    def test_all_ten_materials_are_exposed_by_the_standalone_cli(self):
        for variant in service.APPEARANCE_VARIANTS:
            with self.subTest(variant=variant), patch.object(cli, "Controller") as constructor, \
                    patch("sys.stdout", new_callable=io.StringIO):
                constructor.return_value.run.return_value = {"persisted": False}
                self.assertEqual(cli.main(["configure", "--variant", variant, "--runtime"]), 0)
                constructor.return_value.run.assert_called_once_with("configure", None, {"variant": variant}, persist=False)


if __name__ == "__main__":
    unittest.main()
