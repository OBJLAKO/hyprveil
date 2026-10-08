import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import test_service as fixtures

service = fixtures.service


class NativeController(fixtures.FakeController):
    def __init__(self, config):
        super().__init__(config)
        self.image_path = ""
        self.errors = ""
        self.override = None
        self.reparse_on_load = False

    def apply_lua(self):
        _, _, _, values = service.parse_lua_settings(service.read_lua_settings(self.lua_path)[0])
        self.mode = values["mode"]
        self.image_path = values["image_path"]
        self.appearance = {key: values[key] for key in service.DEFAULT_APPEARANCE}
        if self.override:
            self.appearance.update(self.override)

    def native_configure(self, patch, expected):
        current = self.native("status")
        if service.native_values(current) != service.native_values(expected):
            raise service.Refused("native settings changed; retry")
        values = dict(service.native_values(current), **patch)
        service.lua_settings_block(values)
        self.mode, self.image_path = values["mode"], values["image_path"]
        self.appearance = {key: values[key] for key in service.DEFAULT_APPEARANCE}
        return self.native("status")

    def raw(self, *args):
        if args == ("reload",):
            self.commands.append(args)
            self.apply_lua()
            return "ok"
        if args == ("configerrors",):
            self.commands.append(args)
            return self.errors
        result = super().raw(*args)
        if args[:2] == ("plugin", "load") and self.reparse_on_load:
            # Native loading may reread Lua before its first IPC status reply.
            self.apply_lua()
        if args[0] == "hyprveil":
            value = json.loads(result)
            if "error" not in value:
                if args[1] == "image":
                    self.image_path = args[2]
                value.update(config_api=1, image_path=self.image_path)
            return json.dumps(value)
        return result


class NativePersistenceTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing admission fixture without inheriting its tests.
        self.fixture = fixtures.ServiceTests("runTest")
        self.fixture.setUp()
        self.root = self.fixture.root
        self.hypr = self.root / ".config/hypr"
        self.hypr.mkdir(mode=0o700, parents=True)
        self.source = self.hypr / "hyprveil-settings.lua"
        self.settings = service.manifest(self.fixture.settings)
        self.source.write_bytes(service.lua_settings_source(self.settings))
        self.source.chmod(0o600)
        self.controller = NativeController(self.fixture.config)
        self.controller.loaded = True

    def tearDown(self):
        self.fixture.tearDown()

    def values(self):
        return service.parse_lua_settings(self.source.read_bytes())[3]

    def test_partial_gui_change_preserves_runtime_lua_values_and_mode(self):
        self.controller.mode = "spoiler"
        self.controller.appearance.update(color="#123456", grain=73)
        status = self.controller.run("configure", appearance={"speed": 0})
        self.assertEqual(status["mode"], "spoiler")
        self.assertEqual(status["appearance"], dict(service.DEFAULT_APPEARANCE, color="#123456", grain=73, speed=0))
        self.assertEqual(self.values()["color"], "#123456")
        self.assertEqual(self.values()["mode"], "spoiler")
        saved = json.loads(service.read_private(self.fixture.config))
        self.assertEqual(saved["appearance"], status["appearance"])
        self.assertEqual(saved["desired_mode"], "spoiler")
        self.assertIn(("reload",), self.controller.commands)
        backup = self.root / ".local/state/hyprveil/last-settings.lua"
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_mode_save_preserves_actual_lua_appearance(self):
        self.controller.appearance["grain"] = 92
        status = self.controller.run("spoiler")
        self.assertEqual(status["appearance"]["grain"], 92)
        self.assertEqual(self.values()["grain"], 92)
        self.assertEqual(self.values()["mode"], "spoiler")

    def test_start_applies_lua_without_stale_json_override(self):
        values = dict(self.values(), mode="spoiler", color="#abcdef", grain=17)
        self.source.write_text(service.lua_settings_block(values) + '\nlocal _, missing = hl.get_config("plugin.hyprveil.mode")\nif not missing then hl.config({plugin={hyprveil=hyprveil_settings}}) end\n')
        status = self.controller.run("start")
        self.assertEqual(status["mode"], "spoiler")
        self.assertEqual(status["appearance"]["grain"], 17)
        self.assertEqual(status["appearance"]["color"], "#abcdef")
        self.assertFalse(any(command[:2] == ("hyprveil", "appearance") and len(command) > 2 for command in self.controller.commands))

    def prepare_reparsed_load(self, mode="spoiler"):
        values = dict(self.values(), mode=mode, variant="telegram", color="#abcdef", grain=17, speed=0)
        self.source.write_bytes(service.lua_settings_block(values).encode() + b"\n-- retained user customization\n")
        self.controller.loaded = False
        self.controller.reparse_on_load = True
        return values, self.source.read_bytes(), self.fixture.config.read_bytes()

    def assert_failed_load_remains_reviewable(self, phases, source, manifest, phase="refused"):
        self.assertTrue(self.controller.loaded)
        self.assertNotIn(("reload",), self.controller.commands)
        self.assertNotIn("ready-black", [call.args[0] for call in phases.call_args_list])
        self.assertNotIn("healthy", [call.args[0] for call in phases.call_args_list])
        self.assertEqual(json.loads(service.read_private(self.controller.startup_path))["phase"], phase)
        self.assertFalse((self.fixture.runtime / ".hyprveil-live-111").exists())
        self.assertEqual(self.source.read_bytes(), source)
        self.assertEqual(self.fixture.config.read_bytes(), manifest)

    def test_new_load_reparse_selects_and_attests_black_before_restoring_lua(self):
        values, source, manifest = self.prepare_reparsed_load()
        original = self.controller.raw

        def observe_reload(*args):
            if args == ("reload",):
                self.assertEqual(self.controller.mode, "black")
                self.assertEqual(json.loads(service.read_private(self.controller.startup_path))["phase"], "ready-black")
            return original(*args)

        with patch.object(self.controller, "raw", side_effect=observe_reload), \
                patch.object(self.controller, "startup_record", wraps=self.controller.startup_record) as phases:
            status = self.controller.run("start")
        self.assertEqual(service.native_values(status), values)
        native_commands = [command for command in self.controller.commands if command[0] == "hyprveil"]
        self.assertEqual(native_commands[:3], [("hyprveil", "status"), ("hyprveil", "black"), ("hyprveil", "status")])
        self.assertEqual([call.args[0] for call in phases.call_args_list], ["pending", "ready-black", "healthy"])
        self.assertEqual(sum(command[:2] == ("plugin", "load") for command in self.controller.commands), 1)
        self.assertTrue(all(command[1] in ("status", "black") for command in native_commands))
        self.assertEqual(self.source.read_bytes(), source)
        self.assertEqual(self.fixture.config.read_bytes(), manifest)
        self.assertEqual(json.loads(service.read_private(self.controller.startup_path))["phase"], "healthy")
        self.assertFalse((self.fixture.runtime / ".hyprveil-live-111").exists())

    def test_new_load_already_black_reloads_saved_lua_without_extra_mode_write(self):
        values, source, manifest = self.prepare_reparsed_load(mode="black")
        status = self.controller.run("start")
        self.assertEqual(service.native_values(status), values)
        self.assertNotIn(("hyprveil", "black"), self.controller.commands)
        self.assertIn(("reload",), self.controller.commands)
        self.assertEqual(self.source.read_bytes(), source)
        self.assertEqual(self.fixture.config.read_bytes(), manifest)

    def test_new_load_black_selection_failure_refuses_before_reload(self):
        _, source, manifest = self.prepare_reparsed_load()
        original = self.controller.raw
        attempts = 0

        def refuse_first_black(*args):
            nonlocal attempts
            if args == ("hyprveil", "black"):
                attempts += 1
                if attempts == 1:
                    self.controller.commands.append(args)
                    return json.dumps({"error": "synthetic black selection refusal"})
            return original(*args)

        with patch.object(self.controller, "raw", side_effect=refuse_first_black), \
                patch.object(self.controller, "startup_record", wraps=self.controller.startup_record) as phases:
            with self.assertRaisesRegex(service.Refused, "black selection refusal"):
                self.controller.run("start")
        self.assertEqual(attempts, 2)  # The recovery attempt is independent of admission.
        self.assertEqual(self.controller.mode, "black")
        self.assert_failed_load_remains_reviewable(phases, source, manifest)

    def test_new_load_black_ack_then_reparse_is_not_accepted_as_black(self):
        _, source, manifest = self.prepare_reparsed_load()
        original = self.controller.raw
        attempts = 0

        def reparse_after_black_ack(*args):
            nonlocal attempts
            result = original(*args)
            if args == ("hyprveil", "black"):
                attempts += 1
                if attempts == 1:
                    # The command's reply is black, but another config pass
                    # changes the actual mode before the attestation query.
                    self.controller.mode = "spoiler"
            return result

        with patch.object(self.controller, "raw", side_effect=reparse_after_black_ack), \
                patch.object(self.controller, "startup_record", wraps=self.controller.startup_record) as phases:
            with self.assertRaises(service.Refused):
                self.controller.run("start")
        native_commands = [command for command in self.controller.commands if command[0] == "hyprveil"]
        self.assertEqual(native_commands[:3], [("hyprveil", "status"), ("hyprveil", "black"), ("hyprveil", "status")])
        self.assertEqual(attempts, 2)
        self.assertEqual(self.controller.mode, "black")
        self.assert_failed_load_remains_reviewable(phases, source, manifest)

    def test_new_load_unconfirmed_black_keeps_pending_without_success_marker(self):
        _, source, manifest = self.prepare_reparsed_load()
        original = self.controller.raw

        def refuse_black(*args):
            if args == ("hyprveil", "black"):
                self.controller.commands.append(args)
                return json.dumps({"error": "synthetic persistent black selection refusal"})
            return original(*args)

        with patch.object(self.controller, "raw", side_effect=refuse_black), \
                patch.object(self.controller, "startup_record", wraps=self.controller.startup_record) as phases:
            with self.assertRaisesRegex(service.Refused, "black selection refusal"):
                self.controller.run("start")
        self.assertEqual(self.controller.mode, "spoiler")
        self.assert_failed_load_remains_reviewable(phases, source, manifest, phase="pending")

    def test_explicit_reload_returns_new_lua_values(self):
        values = dict(self.values(), mode="omit", grain=0)
        self.source.write_bytes(service.lua_settings_block(values).encode())
        status = self.controller.run("reload-config")
        self.assertEqual(status["mode"], "omit")
        self.assertEqual(status["appearance"]["grain"], 0)

    def test_custom_code_outside_block_is_preserved(self):
        suffix = '\n-- User override\nif false then hl.plugin.hyprveil.configure({grain=99}) end\n'
        self.source.write_text(self.source.read_text() + suffix)
        self.controller.run("configure", appearance={"grain": 22})
        self.assertTrue(self.source.read_text().endswith(suffix))

    def test_custom_expression_in_block_refuses_without_touching_state(self):
        self.source.write_text(self.source.read_text().replace("grain = 50", "grain = math.floor(50)"))
        self.controller.mode = "spoiler"
        before = self.source.read_bytes()
        with self.assertRaises(service.Refused):
            self.controller.run("configure", appearance={"grain": 22})
        self.assertEqual(self.controller.mode, "spoiler")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertNotIn(("reload",), self.controller.commands)

    def test_concurrent_edit_is_not_overwritten(self):
        self.controller.mode = "spoiler"
        original = self.controller.native_configure
        edited = self.source.read_bytes() + b"\n-- concurrent user edit\n"

        def concurrent(value, expected):
            result = original(value, expected)
            self.source.write_bytes(edited)
            return result

        with patch.object(self.controller, "native_configure", side_effect=concurrent), self.assertRaisesRegex(service.Refused, "changed"):
            self.controller.run("configure", appearance={"grain": 22})
        self.assertEqual(self.source.read_bytes(), edited)
        self.assertEqual(self.controller.mode, "black")

    def test_config_error_and_other_lua_override_fail_black(self):
        for failure in ("errors", "override"):
            with self.subTest(failure=failure):
                self.controller.errors = "synthetic error" if failure == "errors" else ""
                self.controller.override = {"grain": 99} if failure == "override" else None
                self.controller.mode = "spoiler"
                with self.assertRaises(service.Refused):
                    self.controller.run("configure", appearance={"grain": 22})
                self.assertEqual(self.controller.mode, "black")

    def test_owned_reader_rejects_links_fifo_and_writable_file(self):
        outside = self.root / "outside.lua"
        outside.write_bytes(self.source.read_bytes())
        for kind in ("symlink", "hardlink", "fifo", "writable"):
            self.source.unlink()
            if kind == "symlink":
                self.source.symlink_to(outside)
            elif kind == "hardlink":
                os.link(outside, self.source)
            elif kind == "fifo":
                os.mkfifo(self.source, 0o600)
            else:
                self.source.write_bytes(outside.read_bytes())
                self.source.chmod(0o666)
            with self.subTest(kind=kind), self.assertRaises((OSError, service.Refused)):
                self.controller.run("configure", appearance={"grain": 22})

    def test_lua_strings_roundtrip_unicode_quotes_and_code_looking_paths(self):
        image = '/tmp/секрет " \\ $(touch never); return false.png'
        values = dict(self.values(), image_path=image)
        encoded = service.lua_settings_block(values).encode()
        self.assertEqual(service.parse_lua_settings(encoded)[3]["image_path"], image)

    def test_parser_rejects_duplicate_keys_types_and_code(self):
        good = self.source.read_text()
        for bad in (good.replace("grain = 50", "grain = true"),
                    good.replace("grain = 50,", "grain = 50, grain = 99,"),
                    good.replace("eye = true", "eye = 1"),
                    good.replace("grain = 50", "unknown = 50"),
                    good.replace('color = "#ffffff"', 'color = "#ffffff"; os.execute("no")'),
                    good.replace(service.LUA_END, "")):
            with self.subTest(bad=bad), self.assertRaises(service.Refused):
                service.parse_lua_settings(bad.encode())


if __name__ == "__main__":
    unittest.main()
