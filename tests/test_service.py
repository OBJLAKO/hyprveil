import hashlib
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib


MODULE = Path(__file__).resolve().parents[1] / "tools/service.py"
SPEC = importlib.util.spec_from_file_location("hyprveil_service", MODULE)
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def png(width=1, height=1):
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\0\0\0\xff")) + chunk(b"IEND", b"")


class FakeController(service.Controller):
    def __init__(self, config):
        super().__init__(config, "synthetic-instance")
        self.commands = []
        self.loaded = False
        self.mode = "black"
        self.mapping_matches = True
        self.abi = "tested-abi"
        self.refuse_omit = False
        self.initial_mode = "black"
        self.appearance = dict(service.DEFAULT_APPEARANCE)
        self.appearance_reply = None
        self.refuse_appearance = False
        self.legacy = False

    def select_target(self):
        return service.Target(111, self.signature, "12345")

    def mapped_release(self):
        return self.mapping_matches

    def raw(self, *args):
        self.commands.append(args)
        if args == ("-j", "version"):
            return json.dumps({"abiHash": self.abi})
        if args == ("-j", "plugin", "list"):
            return json.dumps([{"name": "hyprveil", "handle": "1234"}] if self.loaded else [])
        if args[0] == "eval":
            return "ok"
        if args[:2] == ("plugin", "load"):
            self.loaded = True
            self.mode = self.initial_mode
            return "ok"
        if args[:2] == ("plugin", "unload"):
            self.loaded = False
            return "ok"
        if args[0] == "hyprveil":
            if args[1] == "omit" and self.refuse_omit:
                return json.dumps({"error": "synthetic refusal"})
            if args[1] == "appearance":
                if self.refuse_appearance or self.legacy:
                    return json.dumps({"error": "synthetic appearance refusal"})
                if len(args) > 2:
                    self.appearance = {"variant": args[2], "color": args[3], "grain": int(args[4]),
                                       "speed": int(args[5]), "darkness": int(args[6]), "eye": args[7] == "1", "eye_size": int(args[8])}
                    self.appearance.update(icon=args[9] if len(args) == 11 else "eye", icon_opacity=int(args[10]) if len(args) == 11 else 75)
            elif args[1] != "status":
                self.mode = args[1]
            status = {"session": "live", "mode": self.mode, "local_dump": "disabled-in-live", "image_status": "pending"}
            if not self.legacy:
                status["appearance"] = self.appearance_reply if self.appearance_reply is not None else self.appearance
                status.update(icon=self.appearance["icon"], icon_opacity=self.appearance["icon_opacity"])
            return json.dumps(status)
        raise AssertionError(args)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hyprveil-service-test-")
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.config_dir = self.root / "config"
        self.config_dir.mkdir(mode=0o700)
        self.home_patch = patch.object(service.Path, "home", return_value=self.root)
        self.home_patch.start()
        self.config = self.config_dir / "config.json"
        self.plugin = self.root / "hyprveil.so"
        self.plugin.write_bytes(b"synthetic release, not executable")
        self.plugin.chmod(0o400)
        self.settings = {"version": 1, "enabled": True, "plugin": str(self.plugin),
                         "plugin_sha256": hashlib.sha256(self.plugin.read_bytes()).hexdigest(),
                         "compositor_sha256": "b" * 64, "abi_hash": "tested-abi", "desired_mode": "omit", "image_path": ""}
        self.write_settings()
        self.runtime_patch = patch.object(service, "standard_runtime", return_value=self.runtime)
        self.runtime_patch.start()
        self.environment_patch = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)}, clear=False)
        self.environment_patch.start()
        real_digest = service.digest_file
        self.digest_patch = patch.object(service, "digest_file", side_effect=lambda path, *args, **kwargs:
                                        "b" * 64 if kwargs.get("process_executable") or path == Path("/usr/bin/Hyprland") else real_digest(path, *args, **kwargs))
        self.digest_mock = self.digest_patch.start()

    def tearDown(self):
        self.digest_patch.stop()
        self.environment_patch.stop()
        self.runtime_patch.stop()
        self.home_patch.stop()
        self.temporary.cleanup()

    def write_settings(self):
        self.config.write_text(json.dumps(self.settings))
        self.config.chmod(0o600)

    def test_private_reader_rejects_symlink_hardlink_fifo_and_oversize(self):
        for kind in ("symlink", "hardlink", "fifo", "oversize"):
            with self.subTest(kind=kind):
                path = self.config_dir / kind
                if kind == "symlink":
                    path.symlink_to(self.config)
                elif kind == "hardlink":
                    os.link(self.config, path)
                elif kind == "fifo":
                    os.mkfifo(path, 0o600)
                else:
                    path.write_bytes(b"x" * 10)
                    path.chmod(0o600)
                with self.assertRaises((OSError, service.Refused)):
                    service.read_private(path, 5)
                path.unlink()

    def test_atomic_write_replaces_link_without_touching_target(self):
        outside = self.root / "outside"
        outside.write_text("unchanged")
        output = self.config_dir / "new.json"
        output.symlink_to(outside)
        service.atomic_json(output, {"safe": True})
        self.assertEqual(outside.read_text(), "unchanged")
        self.assertFalse(output.is_symlink())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(json.loads(service.read_private(output)), {"safe": True})
        self.assertEqual(list(self.config_dir.glob(".hyprveil-*.tmp")), [])

    def test_lock_keeps_inode_and_rejects_concurrent_command(self):
        first, second = FakeController(self.config), FakeController(self.config)
        with first.locked():
            with self.assertRaisesRegex(service.Refused, "another controller"):
                with second.locked():
                    pass
        path = self.runtime / ".hyprveil-controller.lock"
        inode = path.stat().st_ino
        with second.locked():
            self.assertEqual(path.stat().st_ino, inode)

    def test_lock_rejects_symlink(self):
        (self.runtime / ".hyprveil-controller.lock").symlink_to(self.config)
        with self.assertRaises((OSError, service.Refused)):
            with FakeController(self.config).locked():
                pass

    def test_manifest_schema_is_strict(self):
        for update in ({"enabled": 1}, {"version": True}, {"plugin_sha256": "broken"}, {"desired_mode": "blur"},
                       {"image_path": "relative.png"}, {"unknown": True}):
            with self.subTest(update=update), self.assertRaises(service.Refused):
                service.manifest(dict(self.settings, **update))

    def test_legacy_manifest_receives_independent_canonical_defaults(self):
        parsed = service.manifest(self.settings)
        self.assertEqual(parsed["appearance"], service.DEFAULT_APPEARANCE)
        parsed["appearance"]["grain"] = 0
        self.assertEqual(service.DEFAULT_APPEARANCE["grain"], 50)
        customized = dict(service.DEFAULT_APPEARANCE, color="#AABBCC", variant="signal")
        self.assertEqual(service.manifest(dict(self.settings, appearance=customized))["appearance"]["color"], "#aabbcc")

    def test_appearance_schema_rejects_unknown_missing_and_non_integer_values(self):
        invalid = [None, [], {}, dict(service.DEFAULT_APPEARANCE, unknown=True),
                   dict(service.DEFAULT_APPEARANCE, variant="blur"), dict(service.DEFAULT_APPEARANCE, color="#fff"),
                   dict(service.DEFAULT_APPEARANCE, color="#ffffff\n"), dict(service.DEFAULT_APPEARANCE, eye=1)]
        for field, (minimum, maximum) in service.APPEARANCE_RANGES.items():
            invalid.extend(dict(service.DEFAULT_APPEARANCE, **{field: value})
                           for value in (True, False, 1.0, float("nan"), float("inf"), "50", minimum - 1, maximum + 1))
        for appearance in invalid:
            with self.subTest(appearance=appearance), self.assertRaises(service.Refused):
                service.manifest(dict(self.settings, appearance=appearance))

    def test_ten_material_variants_are_admitted_and_persist_without_mode_change(self):
        self.assertEqual(service.APPEARANCE_VARIANTS, ("prism", "signal", "aurora", "contour", "radar", "matte", "error404", "matrix", "anonymous", "glass"))
        for variant in service.APPEARANCE_VARIANTS:
            with self.subTest(variant=variant):
                controller = FakeController(self.config)
                controller.loaded = True
                controller.mode = "spoiler"
                result = controller.run("configure", appearance={"variant": variant})
                self.assertEqual(result["appearance"]["variant"], variant)
                self.assertEqual(result["mode"], "spoiler")
                self.assertEqual(json.loads(service.read_private(self.config))["appearance"]["variant"], variant)

    def test_legacy_variants_and_seven_field_manifests_are_normalized(self):
        for alias, canonical in service.APPEARANCE_ALIASES.items():
            legacy = {key: value for key, value in dict(service.DEFAULT_APPEARANCE, variant=alias, eye=False).items() if key not in service.ICON_FIELDS}
            result = service.manifest(dict(self.settings, appearance=legacy))["appearance"]
            self.assertEqual(result["variant"], canonical)
            self.assertFalse(result["eye"])
            self.assertEqual((result["icon"], result["icon_opacity"]), ("eye", 75))

    def test_native_seven_field_status_is_enriched_from_top_level_icons(self):
        controller = FakeController(self.config)
        old = {key: value for key, value in service.DEFAULT_APPEARANCE.items() if key not in service.ICON_FIELDS}
        value = dict(session="live", mode="spoiler", local_dump="disabled-in-live", config_api=1,
                     image_path="", appearance=old, icon="shield", icon_opacity=23)
        status = controller.live_status(value)
        self.assertEqual(status["appearance"], dict(service.DEFAULT_APPEARANCE, icon="shield", icon_opacity=23))
        self.assertEqual(service.native_values(status)["icon"], "shield")
        for invalid in (dict(value, icon=[]), {key: item for key, item in value.items() if key != "icon_opacity"}):
            with self.assertRaises(service.Refused):
                controller.live_status(invalid)

    def test_real_native_cas_normalizes_alias_and_uppercase_color_preserving_icons(self):
        controller = FakeController(self.config)
        controller.loaded = True
        controller.appearance.update(icon="lock", icon_opacity=28)
        current = controller.native("status")
        original = controller.raw
        emitted = []

        def commit(*args):
            if args[0] == "eval":
                emitted.append(args[1])
                controller.appearance.update(color="#aabbcc", variant="signal")
                return "ok"
            return original(*args)

        with patch.object(controller, "raw", side_effect=commit):
            result = service.Controller.native_configure(controller, {"color": "#AABBCC", "variant": "telegram"}, current)
        self.assertEqual(result["appearance"], dict(service.DEFAULT_APPEARANCE, color="#aabbcc", variant="signal", icon="lock", icon_opacity=28))
        self.assertIn('s.icon == "lock"', emitted[0])
        self.assertIn('s.icon_opacity == 28', emitted[0])
        self.assertIn('color="#aabbcc"', emitted[0])
        self.assertNotIn("s.appearance.icon", emitted[0])

    def test_variant_and_icon_invalid_types_raise_typed_refusal(self):
        for field in ("variant", "icon"):
            for value in (None, [], {}, 7, True):
                with self.subTest(field=field, value=value), self.assertRaises(service.Refused):
                    service.validate_appearance(dict(service.DEFAULT_APPEARANCE, **{field: value}))

    def test_cli_configure_passes_a_typed_partial_patch(self):
        with patch.object(service, "Controller") as constructor, contextlib.redirect_stdout(io.StringIO()):
            constructor.return_value.run.return_value = {"mode": "spoiler"}
            self.assertEqual(service.main(["configure", "--variant", "signal", "--color", "#AABBCC",
                                           "--grain", "0", "--speed", "200", "--eye", "off", "--eye-size", "128"]), 0)
            constructor.return_value.run.assert_called_once_with("configure", None, False,
                {"variant": "signal", "color": "#AABBCC", "grain": 0, "speed": 200, "eye": False, "eye_size": 128})

    def test_cli_rejects_non_decimal_and_out_of_range_numbers_before_controller(self):
        for value in ("NaN", "1.0", "true", "-1", "+1", "001", "101", "1;touch /tmp/no"):
            with self.subTest(value=value), patch.object(service, "Controller") as constructor, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                service.main(["configure", "--grain", value])
            constructor.assert_not_called()

    def test_configure_rejects_invalid_partial_patch_before_ipc_or_persistence(self):
        for patch_value in ({"unknown": True}, {"grain": True}, {"grain": float("nan")}, {"eye": "off"},
                            {"color": "#112233 extra"}, [], False):
            controller = FakeController(self.config)
            controller.loaded = True
            before = self.config.read_bytes()
            with self.subTest(patch=patch_value), self.assertRaises(service.Refused):
                controller.run("configure", appearance=patch_value)
            self.assertEqual(controller.commands, [])
            self.assertEqual(self.config.read_bytes(), before)

    def test_configure_acknowledges_and_persists_without_changing_mode_or_privacy(self):
        controller = FakeController(self.config)
        controller.loaded = True
        controller.mode = "spoiler"
        result = controller.run("configure", appearance={"variant": "signal", "color": "#AABBCC", "speed": 0, "eye": False})
        wanted = dict(service.DEFAULT_APPEARANCE, variant="signal", color="#aabbcc", speed=0, eye=False)
        self.assertEqual(result["appearance"], wanted)
        self.assertEqual(result["mode"], "spoiler")
        saved = json.loads(service.read_private(self.config))
        self.assertEqual(saved["appearance"], wanted)
        self.assertEqual(saved["desired_mode"], "omit")
        self.assertEqual(saved["image_path"], "")
        self.assertIn(("hyprveil", "appearance", "signal", "#aabbcc", "50", "0", "50", "0", "80", "eye", "75"), controller.commands)
        self.assertFalse(any(args[0] == "eval" or args[:2] == ("plugin", "load") or args[:2] in (("hyprveil", "black"), ("hyprveil", "spoiler"))
                             for args in controller.commands))

    def test_partial_commands_reread_saved_appearance_and_lock_rejects_overlap(self):
        first, second = FakeController(self.config), FakeController(self.config)
        first.loaded = second.loaded = True
        first.run("configure", appearance={"color": "#123456"})
        with first.locked(), self.assertRaisesRegex(service.Refused, "another controller"):
            second.run("configure", appearance={"speed": 150})
        self.assertEqual(second.commands, [])
        second.run("configure", appearance={"grain": 12})
        wanted = dict(service.DEFAULT_APPEARANCE, color="#123456", grain=12)
        self.assertEqual(json.loads(service.read_private(self.config))["appearance"], wanted)
        self.assertEqual(second.appearance, wanted)

    def test_saved_appearance_restores_while_black_before_saved_spoiler(self):
        appearance = dict(service.DEFAULT_APPEARANCE, variant="signal", color="#123456", grain=99,
                          speed=0, darkness=100, eye=False, eye_size=128)
        self.settings.update(appearance=appearance, desired_mode="spoiler")
        self.write_settings()
        controller = FakeController(self.config)
        result = controller.run("start")
        command = ("hyprveil", "appearance", "signal", "#123456", "99", "0", "100", "0", "128", "eye", "75")
        self.assertEqual(result["appearance"], appearance)
        self.assertEqual(result["mode"], "spoiler")
        self.assertLess(controller.commands.index(("hyprveil", "status")), controller.commands.index(command))
        self.assertLess(controller.commands.index(command), controller.commands.index(("hyprveil", "spoiler")))

    def test_appearance_ack_mismatch_never_persists_and_falls_back_to_black(self):
        controller = FakeController(self.config)
        controller.loaded = True
        controller.mode = "spoiler"
        controller.appearance_reply = dict(service.DEFAULT_APPEARANCE)
        before = self.config.read_bytes()
        with self.assertRaisesRegex(service.Refused, "acknowledge"):
            controller.run("configure", appearance={"grain": 73})
        self.assertEqual(controller.mode, "black")
        self.assertEqual(self.config.read_bytes(), before)

    def test_appearance_command_cannot_change_mode_even_with_matching_fields(self):
        controller = FakeController(self.config)
        controller.loaded = True
        controller.mode = "spoiler"
        original = controller.raw

        def unexpected_mode(*args):
            if args[:2] == ("hyprveil", "appearance"):
                controller.mode = "omit"
            return original(*args)

        with patch.object(controller, "raw", side_effect=unexpected_mode), self.assertRaisesRegex(service.Refused, "changing mode"):
            controller.run("configure", appearance={"grain": 73})
        self.assertEqual(controller.mode, "black")

    def test_appearance_persistence_failure_masks_black_and_does_not_claim_success(self):
        for failing_path in (self.config, self.runtime / ".hyprveil-controller.json"):
            controller = FakeController(self.config)
            controller.loaded = True
            controller.mode = "spoiler"
            before = self.config.read_bytes()
            original = service.atomic_json

            def refuse(path, value):
                if path == failing_path:
                    raise OSError("synthetic disk full")
                return original(path, value)

            with self.subTest(path=failing_path), patch.object(service, "atomic_json", side_effect=refuse), self.assertRaisesRegex(OSError, "disk full"):
                controller.run("configure", appearance={"darkness": 80})
            self.assertEqual(controller.mode, "black")
            self.assertEqual(self.config.read_bytes(), before)

    def test_start_appearance_refusal_never_restores_less_restrictive_mode(self):
        controller = FakeController(self.config)
        controller.refuse_appearance = True
        with self.assertRaisesRegex(service.Refused, "appearance refusal"):
            controller.run("start")
        self.assertEqual(controller.mode, "black")
        self.assertNotIn(("hyprveil", "omit"), controller.commands)

    def test_legacy_black_and_status_allow_upgrade_but_new_start_refuses(self):
        controller = FakeController(self.config)
        controller.loaded = True
        controller.legacy = True
        self.assertEqual(controller.native("status")["mode"], "black")
        self.assertEqual(controller.native("black")["mode"], "black")
        with self.assertRaisesRegex(service.Refused, "appearance refusal"):
            controller.run("start")
        self.assertEqual(controller.mode, "black")
        self.assertFalse(controller.run("stop")["loaded"])

    def test_status_reports_actual_native_appearance_after_lua_or_keybinding_change(self):
        controller = FakeController(self.config)
        unloaded = controller.run("status")
        self.assertEqual(unloaded["appearance"], service.DEFAULT_APPEARANCE)
        self.assertIsNone(unloaded["status"])
        controller.loaded = True
        value = controller.run("status")
        self.assertEqual(value["appearance"], value["status"]["appearance"])
        controller.appearance["grain"] = 51
        controller.commands.clear()
        changed = controller.run("status")
        self.assertEqual(changed["appearance"]["grain"], 51)
        self.assertEqual(changed["appearance"], changed["status"]["appearance"])
        self.assertNotIn(("hyprveil", "black"), controller.commands)

    def test_native_appearance_must_be_canonical_and_strict(self):
        controller = FakeController(self.config)
        controller.loaded = True
        for update in ({"color": "#AABBCC"}, {"speed": True}, {"eye_size": 129}, {"unknown": True}):
            controller.appearance_reply = dict(service.DEFAULT_APPEARANCE, **update)
            with self.subTest(update=update), self.assertRaises(service.Refused):
                controller.run("configure", appearance={"grain": 73})
        self.assertEqual(json.loads(service.read_private(self.config))["desired_mode"], "omit")

    def test_disabled_start_does_not_use_ipc(self):
        self.settings["enabled"] = False
        self.write_settings()
        controller = FakeController(self.config)
        self.assertEqual(controller.run("start"), {"enabled": False, "action": "skipped"})
        self.assertEqual(controller.commands, [])

    def test_start_loads_and_validates_black_before_omit(self):
        controller = FakeController(self.config)
        result = controller.run("start")
        self.assertEqual(result["mode"], "omit")
        load = next(i for i, args in enumerate(controller.commands) if args[:2] == ("plugin", "load"))
        validate = controller.commands.index(("hyprveil", "status"))
        omit = controller.commands.index(("hyprveil", "omit"))
        self.assertLess(load, validate)
        self.assertLess(validate, omit)
        self.assertEqual((self.runtime / ".hyprveil-live-111").read_text().splitlines()[3:], ["cancelled", "0"])
        self.assertEqual(json.loads(service.read_private(controller.state_path))["status"]["mode"], "omit")

    def test_new_plugin_must_start_black(self):
        controller = FakeController(self.config)
        controller.initial_mode = "omit"
        with self.assertRaisesRegex(service.Refused, "black replacement mode"):
            controller.run("start")
        self.assertEqual(controller.mode, "black")

    def test_abi_mismatch_never_loads(self):
        controller = FakeController(self.config)
        controller.abi = "different-abi"
        with self.assertRaisesRegex(service.Refused, "ABI"):
            controller.run("start")
        self.assertFalse(any(args[0] in ("eval", "plugin") for args in controller.commands))

    def test_elf_mismatch_never_loads(self):
        self.settings["compositor_sha256"] = "c" * 64
        self.write_settings()
        controller = FakeController(self.config)
        with self.assertRaisesRegex(service.Refused, "compositor ELF"):
            controller.run("start")
        self.assertFalse(any(args[:2] == ("plugin", "load") for args in controller.commands))

    def test_release_hash_mismatch_never_loads(self):
        self.settings["plugin_sha256"] = "c" * 64
        self.write_settings()
        controller = FakeController(self.config)
        with self.assertRaisesRegex(service.Refused, "release SHA"):
            controller.run("start")
        self.assertFalse(controller.loaded)

    def test_installed_compositor_upgrade_refuses_new_load_but_allows_stop(self):
        controller = FakeController(self.config)
        original = service.digest_file
        with patch.object(service, "digest_file", side_effect=lambda path, *args, **kwargs:
                          "c" * 64 if path == Path("/usr/bin/Hyprland") else original(path, *args, **kwargs)):
            with self.assertRaisesRegex(service.Refused, "installed compositor ELF"):
                controller.run("start")
            self.assertFalse(controller.loaded)
            controller.loaded = True
            self.assertFalse(controller.run("stop")["loaded"])

    def test_missing_persistent_image_leaves_black(self):
        self.settings.update(desired_mode="image", image_path=str(self.root / "missing.png"))
        self.write_settings()
        controller = FakeController(self.config)
        with self.assertRaises(OSError):
            controller.run("start")
        self.assertTrue(controller.loaded)
        self.assertEqual(controller.mode, "black")
        self.assertNotIn(("hyprveil", "omit"), controller.commands)
        self.assertEqual((self.runtime / ".hyprveil-live-111").read_text().splitlines()[3:], ["cancelled", "0"])

    def test_unknown_marker_is_not_removed(self):
        marker = self.runtime / ".hyprveil-live-111"
        content = "hyprveil-live-v1\n111\nsome-other-instance\nblack\n123\n"
        marker.write_text(content)
        marker.chmod(0o600)
        with self.assertRaisesRegex(service.Refused, "marker"):
            FakeController(self.config).run("start")
        self.assertEqual(marker.read_text(), content)

    def test_no_marker_cancellation_does_not_block_normal_hyprpm_admission(self):
        controller = FakeController(self.config)
        controller.connect()
        controller.remove_marker()
        self.assertFalse(controller.marker_path().exists())

    def test_cancelled_trial_is_idempotent_and_explicitly_rearmed(self):
        controller = FakeController(self.config)
        controller.connect()
        with patch.object(service.time, "time", return_value=1000):
            self.assertEqual(controller.create_marker(), 1120)
        controller.remove_marker()
        cancelled = service.read_private(controller.marker_path())
        self.assertEqual(cancelled.decode().splitlines()[3:], ["cancelled", "0"])
        controller.remove_marker()
        self.assertEqual(service.read_private(controller.marker_path()), cancelled)
        with patch.object(service.time, "time", return_value=2000):
            self.assertEqual(controller.create_marker(lifetime=7200), 9200)
        self.assertEqual(service.read_private(controller.marker_path()).decode().splitlines()[3:], ["black", "9200"])

    def test_marker_updates_never_follow_links_or_overwrite_unknown_contents(self):
        controller = FakeController(self.config)
        controller.connect()
        target = self.root / "do-not-change-marker"
        original = b"hyprveil-live-v1\n111\nsynthetic-instance\nblack\n1\n"
        target.write_bytes(original)
        target.chmod(0o600)
        marker = controller.marker_path()
        marker.symlink_to(target)
        for action in (controller.remove_marker, controller.create_marker):
            with self.assertRaises((OSError, service.Refused)):
                action()
            self.assertEqual(target.read_bytes(), original)
        marker.unlink()
        marker.write_text("hyprveil-live-v1\n111\nsynthetic-instance\ncancelled\n123\n")
        marker.chmod(0o600)
        unknown = marker.read_bytes()
        for action in (controller.remove_marker, controller.create_marker):
            with self.assertRaises(service.Refused):
                action()
            self.assertEqual(marker.read_bytes(), unknown)

    def test_malformed_marker_line_endings_and_encoding_are_preserved(self):
        controller = FakeController(self.config)
        controller.connect()
        marker = controller.marker_path()
        for content in (b"hyprveil-live-v1\r\n111\r\nsynthetic-instance\r\nblack\r\n1\r\n",
                        b"hyprveil-live-v1\n111\nsynthetic-instance\nblack\v1\n",
                        b"hyprveil-live-v1\n111\nsynthetic-instance\nblack\n\xff\n"):
            marker.write_bytes(content)
            marker.chmod(0o600)
            for action in (controller.create_marker, controller.remove_marker):
                with self.assertRaises(service.Refused):
                    action()
                self.assertEqual(marker.read_bytes(), content)

    def test_matching_expired_marker_can_be_replaced(self):
        marker = self.runtime / ".hyprveil-live-111"
        marker.write_text("hyprveil-live-v1\n111\nsynthetic-instance\nblack\n1\n")
        marker.chmod(0o600)
        self.assertEqual(FakeController(self.config).run("start")["mode"], "omit")
        self.assertEqual(marker.read_text().splitlines()[3:], ["cancelled", "0"])

    def test_foreign_mapped_plugin_refuses_mode_and_unload(self):
        for action in ("omit", "stop"):
            with self.subTest(action=action):
                controller = FakeController(self.config)
                controller.loaded = True
                controller.mapping_matches = False
                with self.assertRaisesRegex(service.Refused, "exact configured"):
                    controller.run(action)
                self.assertFalse(any(args[0] == "hyprveil" or args[:2] == ("plugin", "unload") for args in controller.commands))

    def test_native_refusal_does_not_persist_choice(self):
        self.settings["desired_mode"] = "image"
        self.settings["image_path"] = str(self.root / "remembered.png")
        self.write_settings()
        controller = FakeController(self.config)
        controller.loaded = True
        controller.mode = "image"
        controller.refuse_omit = True
        with self.assertRaisesRegex(service.Refused, "refused"):
            controller.run("omit")
        self.assertEqual(controller.mode, "black")
        self.assertEqual(json.loads(service.read_private(self.config))["desired_mode"], "image")

    def test_successful_mode_is_persisted(self):
        controller = FakeController(self.config)
        controller.loaded = True
        self.assertEqual(controller.run("black")["mode"], "black")
        self.assertEqual(json.loads(service.read_private(self.config))["desired_mode"], "black")

    def test_spoiler_is_persisted_without_touching_privacy_or_loading(self):
        controller = FakeController(self.config)
        controller.loaded = True
        self.assertEqual(controller.run("spoiler")["mode"], "spoiler")
        self.assertEqual(json.loads(service.read_private(self.config))["desired_mode"], "spoiler")
        self.assertFalse(any(args[0] == "eval" or args[:2] == ("plugin", "load") for args in controller.commands))

    def test_saved_spoiler_starts_only_after_validated_black(self):
        self.settings["desired_mode"] = "spoiler"
        self.write_settings()
        controller = FakeController(self.config)
        self.assertEqual(controller.run("start")["mode"], "spoiler")
        self.assertLess(controller.commands.index(("hyprveil", "status")), controller.commands.index(("hyprveil", "spoiler")))

    def test_spoiler_persistence_failure_keeps_safe_black(self):
        controller = FakeController(self.config)
        controller.loaded = True
        with patch.object(service, "atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                controller.run("spoiler")
        self.assertEqual(controller.mode, "black")
        self.assertEqual(json.loads(service.read_private(self.config))["desired_mode"], "omit")

    def test_disable_persists_then_masks_and_unloads(self):
        controller = FakeController(self.config)
        controller.loaded = True
        controller.mode = "omit"
        value = controller.run("disable")
        self.assertFalse(value["enabled"])
        self.assertFalse(controller.loaded)
        self.assertEqual(value["mode"], "native")
        self.assertEqual(json.loads(service.read_private(controller.state_path))["status"], {"mode": "native", "loaded": False})
        self.assertFalse(json.loads(service.read_private(self.config))["enabled"])
        self.assertLess(controller.commands.index(("hyprveil", "black")), controller.commands.index(("plugin", "unload", str(self.plugin))))

    def test_stop_reports_native_renderer_instead_of_claiming_a_black_replacement(self):
        controller = FakeController(self.config)
        controller.loaded = True
        self.assertEqual(controller.run("stop"), {"enabled": True, "loaded": False, "mode": "native"})
        self.assertEqual(json.loads(service.read_private(controller.state_path))["status"]["mode"], "native")

    def test_status_does_not_hash_binary_or_change_mode(self):
        controller = FakeController(self.config)
        controller.loaded = True
        controller.mode = "omit"
        value = controller.run("status")
        self.assertEqual(value["status"]["mode"], "omit")
        self.digest_mock.assert_not_called()

    def test_png_validation_accepts_valid_and_rejects_damage_or_oversize(self):
        controller = FakeController(self.config)
        image = self.root / "replacement.png"
        image.write_bytes(png())
        self.assertEqual(controller.safe_image(str(image)), image)
        for content in (png(100000, 100000), png()[:-1], png()[:-13] + b"invalid chunk"):
            image.write_bytes(content)
            with self.subTest(content=content[:20]), self.assertRaises(service.Refused):
                controller.safe_image(str(image))

    def test_image_success_persists_path(self):
        image = self.root / "replacement.png"
        image.write_bytes(png())
        controller = FakeController(self.config)
        controller.loaded = True
        self.assertEqual(controller.run("image", image)["mode"], "image")
        value = json.loads(service.read_private(self.config))
        self.assertEqual(value["desired_mode"], "image")
        self.assertEqual(value["image_path"], str(image))

    def test_16_bit_decode_budget_refuses_before_inflate(self):
        image = self.root / "oversized-decoded.png"
        # Eight million pixels fit the encoded/area limits, but Cairo could
        # require 128 MiB when decoding this legal 16-bit header.
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4096, 2048, 16, 6, 0, 0, 0)) +
                          chunk(b"IDAT", b"header-only probe") + chunk(b"IEND", b""))
        controller = FakeController(self.config)
        controller.loaded = True
        with patch.object(service.zlib, "decompressobj") as decoder, self.assertRaisesRegex(service.Refused, "decoded image limit"):
            controller.run("image", image)
        decoder.assert_not_called()
        self.assertEqual(controller.mode, "black")
        self.assertEqual(json.loads(service.read_private(self.config))["desired_mode"], "omit")

    def test_many_ancillary_chunks_refuse_before_inflate(self):
        image = self.root / "too-many-chunks.png"
        valid = png()
        image.write_bytes(valid[:33] + chunk(b"tEXt", b"") * 16383 + valid[33:])
        controller = FakeController(self.config)
        with patch.object(service.zlib, "decompressobj") as decoder, self.assertRaisesRegex(service.Refused, "chunk limit"):
            controller.safe_image(str(image))
        decoder.assert_not_called()

    def test_native_rejected_chunk_types_and_alpha_never_persist_image(self):
        image = self.root / "native-rejected.png"
        valid = png()
        prefix = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        rgb_data = chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
        alpha = chunk(b"tRNS", b"\0" * 6)
        malformed = [(kind.decode(), valid[:33] + chunk(kind, payload) + valid[33:])
                     for kind, payload in ((b"a1At", b""), (b"aaat", b""), (b"tRNS", b"\0"))]
        malformed.extend((("duplicate-alpha", prefix + alpha + alpha + rgb_data + chunk(b"IEND", b"")),
                          ("alpha-after-data", prefix + rgb_data + alpha + chunk(b"IEND", b""))))
        for name, content in malformed:
            image.write_bytes(content)
            controller = FakeController(self.config)
            controller.loaded = True
            with self.subTest(name=name), self.assertRaisesRegex(service.Refused, "chunk"):
                controller.run("image", image)
            self.assertEqual(controller.mode, "black")
            self.assertEqual(json.loads(service.read_private(self.config))["desired_mode"], "omit")

    def test_indexed_png_palette_and_transparency_remain_supported(self):
        image = self.root / "palette-alpha.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0)) +
                          chunk(b"PLTE", b"\xff\0\0") + chunk(b"tRNS", b"\x80") +
                          chunk(b"IDAT", zlib.compress(b"\0\0")) + chunk(b"IEND", b""))
        self.assertEqual(FakeController(self.config).safe_image(str(image)), image)

    def test_invalid_compressed_png_never_changes_persisted_choice(self):
        image = self.root / "malformed.png"
        content = png()
        size = struct.unpack(">I", content[33:37])[0]
        payload = b"not deflate"
        replacement = struct.pack(">I", len(payload)) + b"IDAT" + payload + struct.pack(">I", zlib.crc32(b"IDAT" + payload) & 0xffffffff)
        image.write_bytes(content[:33] + replacement + content[33 + size + 12:])
        controller = FakeController(self.config)
        controller.loaded = True
        with self.assertRaisesRegex(service.Refused, "compressed PNG"):
            controller.run("image", image)
        self.assertEqual(controller.mode, "black")
        self.assertEqual(json.loads(service.read_private(self.config))["desired_mode"], "omit")

    def test_configuration_write_refusal_returns_to_black(self):
        controller = FakeController(self.config)
        controller.loaded = True
        original = service.atomic_json
        with patch.object(service, "atomic_json", side_effect=lambda path, value:
                          (_ for _ in ()).throw(service.Refused("synthetic directory refusal")) if path == self.config else original(path, value)):
            with self.assertRaisesRegex(service.Refused, "directory refusal"):
                controller.run("omit")
        self.assertEqual(controller.mode, "black")

    def test_unconfirmed_prior_load_requires_explicit_recovery(self):
        controller = FakeController(self.config)
        controller.settings = self.settings
        controller.target = service.Target(222, "old-instance", "old-start")
        controller.startup_record("pending")
        with self.assertRaisesRegex(service.Refused, "not confirmed"):
            controller.run("start")
        self.assertFalse(controller.loaded)
        self.assertEqual(controller.run("start", recover=True)["mode"], "omit")
        self.assertEqual(json.loads(service.read_private(controller.startup_path))["phase"], "healthy")

    def test_healthy_prior_session_does_not_block_login(self):
        controller = FakeController(self.config)
        controller.settings = self.settings
        controller.target = service.Target(222, "old-instance", "old-start")
        controller.startup_record("healthy")
        self.assertEqual(controller.run("start")["mode"], "omit")

    def test_current_confirmed_mapped_pending_load_is_not_reloaded(self):
        controller = FakeController(self.config)
        controller.settings = self.settings
        controller.target = controller.select_target()
        controller.startup_record("pending")
        controller.loaded = True
        self.assertEqual(controller.run("start")["mode"], "omit")
        self.assertFalse(any(args[:2] == ("plugin", "load") for args in controller.commands))

    def test_failed_native_init_keeps_pending_and_blocks_next_automatic_start(self):
        controller = FakeController(self.config)
        original = controller.raw
        crashed = False

        def raw(*args):
            nonlocal crashed
            if args[:2] == ("plugin", "load"):
                crashed = True
                raise service.NotReady("synthetic compositor died during initialization")
            if crashed:
                raise service.NotReady("synthetic compositor is gone")
            return original(*args)

        def target():
            if crashed:
                raise service.NotReady("synthetic compositor is gone")
            return service.Target(111, controller.signature, "12345")

        with patch.object(controller, "raw", side_effect=raw), patch.object(controller, "select_target", side_effect=target):
            with self.assertRaisesRegex(service.Refused, "died"):
                controller.run("start")
        self.assertEqual(json.loads(service.read_private(controller.startup_path))["phase"], "pending")
        self.assertEqual((self.runtime / ".hyprveil-live-111").read_text().splitlines()[3:], ["cancelled", "0"])
        next_login = FakeController(self.config)
        with self.assertRaisesRegex(service.Refused, "not confirmed"):
            next_login.run("start")
        self.assertFalse(next_login.loaded)
        self.assertEqual(next_login.run("enable")["mode"], "omit")

    def test_identity_change_between_hashing_and_load_never_grants_permission(self):
        controller = FakeController(self.config)
        calls = 0

        def target():
            nonlocal calls
            calls += 1
            return service.Target(111, controller.signature, "12345" if calls == 1 else "new-start")

        with patch.object(controller, "select_target", side_effect=target), self.assertRaisesRegex(service.Refused, "identity changed"):
            controller.run("start")
        self.assertFalse(any(args[0] in ("eval", "plugin") for args in controller.commands))

    def test_startup_raw_timeout_is_bounded_by_remaining_deadline(self):
        controller = service.Controller(self.config, "synthetic-instance")
        controller.connection_deadline = 12
        response = SimpleNamespace(returncode=0, stdout="[]", stderr="")
        with patch.object(service.time, "monotonic", return_value=10.5), patch.object(service, "bounded_command", return_value=response) as run:
            self.assertEqual(controller.raw("-j", "instances"), "[]")
            self.assertEqual(run.call_args.kwargs["timeout"], 1.5)
        with patch.object(service.time, "monotonic", return_value=13), patch.object(service, "bounded_command") as run:
            with self.assertRaises(service.NotReady):
                controller.raw("-j", "instances")
            run.assert_not_called()

    def test_only_connection_not_ready_is_retried(self):
        controller = FakeController(self.config)
        values = [service.NotReady("synthetic starting"), controller.select_target()]
        with patch.object(controller, "select_target", side_effect=values) as target, patch.object(service.time, "sleep"):
            controller.connect(retry=True)
            self.assertEqual(target.call_count, 2)
        with patch.object(controller, "select_target", side_effect=service.Refused("synthetic wrong UID")) as target, self.assertRaises(service.Refused):
            controller.connect(retry=True)
        self.assertEqual(target.call_count, 1)

    def test_selected_socket_peer_uid_pid_and_session_environment_are_attested(self):
        controller = service.Controller(self.config, "synthetic-instance")
        environment = f"XDG_RUNTIME_DIR={self.runtime}\0".encode()
        proc_stat = "313 (Hyprland) " + " ".join(["0"] * 19 + ["started"])
        peer = SimpleNamespace(pid=313, uid=os.getuid())

        class Connection:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def settimeout(self, timeout):
                pass
            def connect(self, path):
                pass
            def getsockopt(self, *args):
                return struct.pack("3i", peer.pid, peer.uid, 0)

        with patch.object(controller, "query", return_value=[{"instance": controller.signature, "pid": 313}]), \
             patch.object(service.Path, "stat", return_value=SimpleNamespace(st_uid=os.getuid())), \
             patch.object(service.os, "readlink", return_value="/usr/bin/Hyprland (deleted)"), \
             patch.object(service.Path, "read_bytes", return_value=environment) as read_environment, \
             patch.object(service.Path, "read_text", return_value=proc_stat), \
             patch.object(service.Path, "is_symlink", return_value=False), patch.object(service.Path, "is_socket", return_value=True), \
             patch.object(service.socket, "socket", return_value=Connection()):
            self.assertEqual(controller.select_target().pid, 313)
            for pid, uid in ((999, os.getuid()), (313, os.getuid() + 1)):
                peer.pid, peer.uid = pid, uid
                with self.assertRaisesRegex(service.Refused, "socket peer"):
                    controller.select_target()
            peer.pid, peer.uid = 313, os.getuid()
            read_environment.return_value = environment + b"HYPRVEIL_LAB_RUNTIME=/tmp/synthetic\0"
            with self.assertRaisesRegex(service.Refused, "standard desktop session"):
                controller.select_target()

    def test_deleted_exact_release_still_counts_as_mapped_for_rollback(self):
        controller = service.Controller(self.config, "synthetic-instance")
        controller.settings = self.settings
        controller.target = service.Target(313, controller.signature, "started")
        contents = f"7fff-8000 r-xp 00000000 01:01 313 {self.plugin} (deleted)\n"
        with patch.object(service.Path, "read_text", return_value=contents):
            self.assertTrue(controller.mapped_release())


if __name__ == "__main__":
    unittest.main()
