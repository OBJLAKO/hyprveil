import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import service
import install


class MockDesktop(service.Controller):
    def __init__(self, signature):
        super().__init__(signature=signature)
        self.commands = []
        self.errors_count = 0
        self.post_errors = False
        self.refuse_reload = False
        self.loaded_plugin = False
        self.mapping_matches = True

    def select_target(self):
        return service.Target(313, self.signature, "synthetic-start")

    def raw(self, *args):
        self.commands.append(args)
        if args == ("-j", "version"):
            return json.dumps({"abiHash": "tested-abi"})
        if args == ("-j", "plugin", "list"):
            return json.dumps([{"name": "hyprveil"}] if self.loaded_plugin else [])
        if args == ("configerrors",):
            self.errors_count += 1
            return "synthetic configuration error" if self.post_errors and self.errors_count == 2 else ""
        if args == ("reload",):
            return "refused" if self.refuse_reload else "ok"
        raise AssertionError("unexpected IPC, including forbidden plugin load: " + str(args))

    def mapped_release(self):
        return self.mapping_matches


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hyprveil-install-test-")
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.project = self.root / "project"
        self.project.mkdir(mode=0o700)
        self.hypr = self.root / ".config/hypr"
        self.hypr.mkdir(mode=0o700, parents=True)
        self.original = b'-- synthetic configuration\nhl = {}\no = {}\n'
        (self.hypr / "hyprland.lua").write_bytes(self.original)
        (self.hypr / "hyprland.lua").chmod(0o600)
        self.source = self.project / "hyprveil.so"
        self.source.write_bytes(b"synthetic pinned release; not executable")
        self.source.chmod(0o400)
        self.args = argparse.Namespace(plugin=self.source, plugin_sha256=install.sha(self.source.read_bytes()),
                                       compositor_sha256="b" * 64, abi_hash="tested-abi", signature="synthetic-instance", pid=313)
        self.patches = [patch.object(service.Path, "home", return_value=self.root),
                        patch.object(service, "standard_runtime", return_value=self.runtime),
                        patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)}),
                        patch.object(install, "PROJECT", self.project)]
        for item in self.patches:
            item.start()
        self.desktop = MockDesktop(self.args.signature)
        self.patches.extend([patch.object(service, "Controller", return_value=self.desktop),
                             patch.object(service, "digest_file", return_value="b" * 64)])
        for item in self.patches[-2:]:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def run_install(self):
        instance = install.Installer(self.args)
        return instance, instance.run()

    def test_install_pins_private_files_and_only_reloads_configuration(self):
        instance, report = self.run_install()
        self.assertTrue(report["installed"])
        self.assertFalse(report["loaded_by_installer"])
        self.assertEqual(stat.S_IMODE(instance.release.stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE(instance.controller_path.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(instance.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(instance.config.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(instance.report_dir.stat().st_mode), 0o700)
        self.assertEqual(os.readlink(instance.cli), str(instance.controller_path))
        settings = service.manifest(json.loads(service.read_private(instance.config)))
        self.assertEqual(settings["desired_mode"], "omit")
        self.assertIn(install.BLOCK.encode(), instance.main.read_bytes())
        self.assertEqual(self.desktop.commands.count(("reload",)), 1)
        self.assertFalse(any(args[0] in ("eval", "hyprveil", "plugin") for args in self.desktop.commands))
        self.assertTrue(Path(report["report"]).exists())

    def test_repeat_preserves_user_choice_without_unnecessary_reload(self):
        first, _ = self.run_install()
        settings = json.loads(service.read_private(first.config))
        settings.update(enabled=False, desired_mode="black")
        service.atomic_json(first.config, settings)
        self.desktop.commands.clear()
        second, report = self.run_install()
        after = json.loads(service.read_private(second.config))
        self.assertFalse(after["enabled"])
        self.assertEqual(after["desired_mode"], "black")
        self.assertEqual(second.main.read_bytes().count(install.BEGIN.encode()), 1)
        self.assertNotIn(("reload",), self.desktop.commands)
        self.assertFalse(report["reload_validated"])

    def test_upgrade_preserves_all_appearance_settings_and_mode(self):
        first, _ = self.run_install()
        wanted = dict(service.DEFAULT_APPEARANCE, variant="signal", color="#1a2b3c", grain=0,
                      speed=200, darkness=100, eye=False, eye_size=128)
        settings = json.loads(service.read_private(first.config))
        settings.update(appearance=wanted, desired_mode="spoiler", enabled=False)
        service.atomic_json(first.config, settings)
        self.source.chmod(0o600)
        self.source.write_bytes(b"new synthetic customization release; not executable")
        self.source.chmod(0o400)
        self.args.plugin_sha256 = install.sha(self.source.read_bytes())
        self.desktop.commands.clear()
        second, _ = self.run_install()
        after = service.manifest(json.loads(service.read_private(second.config)))
        self.assertEqual(after["appearance"], wanted)
        self.assertEqual(after["desired_mode"], "spoiler")
        self.assertFalse(after["enabled"])
        self.assertEqual(after["plugin_sha256"], self.args.plugin_sha256)
        self.assertFalse(any(args[0] == "hyprveil" or args[:2] == ("plugin", "load") for args in self.desktop.commands))

    def test_legacy_repeat_manifest_gets_appearance_defaults(self):
        first, _ = self.run_install()
        settings = json.loads(service.read_private(first.config))
        settings.pop("appearance")
        service.atomic_json(first.config, settings)
        second, _ = self.run_install()
        self.assertEqual(json.loads(service.read_private(second.config))["appearance"], service.DEFAULT_APPEARANCE)

    def test_source_hash_and_compositor_mismatch_never_write_configuration(self):
        for kind in ("source", "compositor"):
            with self.subTest(kind=kind):
                if kind == "source":
                    self.args.plugin_sha256 = "a" * 64
                    context = contextlib.nullcontext()
                else:
                    self.args.plugin_sha256 = install.sha(self.source.read_bytes())
                    context = patch.object(service, "digest_file", return_value="c" * 64)
                with context, self.assertRaises(service.Refused):
                    self.run_install()
                self.assertEqual((self.hypr / "hyprland.lua").read_bytes(), self.original)
                self.assertFalse((self.root / ".config/hyprveil/config.json").exists())
                self.assertNotIn(("reload",), self.desktop.commands)

    def test_upgrade_refuses_old_loaded_release_without_stranding_controller_pins(self):
        first, _ = self.run_install()
        original_config = first.config.read_bytes()
        original_main = first.main.read_bytes()
        self.source.chmod(0o600)
        self.source.write_bytes(b"new synthetic pinned release; not executable")
        self.source.chmod(0o400)
        self.args.plugin_sha256 = install.sha(self.source.read_bytes())
        self.desktop.loaded_plugin = True
        self.desktop.mapping_matches = False
        self.desktop.commands.clear()
        with self.assertRaisesRegex(service.Refused, "stop the current Hyprveil release"):
            self.run_install()
        self.assertEqual(first.config.read_bytes(), original_config)
        self.assertEqual(first.main.read_bytes(), original_main)
        self.assertFalse((first.base / "releases" / self.args.plugin_sha256).exists())
        self.assertNotIn(("reload",), self.desktop.commands)

    def test_same_loaded_release_permits_idempotent_install(self):
        first, _ = self.run_install()
        self.desktop.loaded_plugin = True
        self.desktop.commands.clear()
        second, report = self.run_install()
        self.assertTrue(report["installed"])
        self.assertEqual(first.config.read_bytes(), second.config.read_bytes())
        self.assertNotIn(("reload",), self.desktop.commands)

    def test_foreign_release_loaded_during_install_rolls_back_pins_and_cli(self):
        instance = install.Installer(self.args)
        original_setup = instance.setup_files
        def concurrent_load():
            original_setup()
            self.desktop.loaded_plugin = True
            self.desktop.mapping_matches = False
        with patch.object(instance, "setup_files", side_effect=concurrent_load), \
             self.assertRaisesRegex(service.Refused, "exact configured release"):
            instance.run()
        self.assertEqual(instance.main.read_bytes(), self.original)
        self.assertFalse(instance.config.exists())
        self.assertFalse(os.path.lexists(instance.cli))
        self.assertFalse(instance.release.exists())
        self.assertTrue(instance.report["rollback"]["restored"])

    def test_foreign_cli_refusal_does_not_reload_or_overwrite(self):
        instance = install.Installer(self.args)
        instance.cli.parent.mkdir(mode=0o755, parents=True)
        instance.cli.write_bytes(b"unrelated command")
        with self.assertRaisesRegex(service.Refused, "CLI is unrelated"):
            instance.run()
        self.assertEqual(instance.cli.read_bytes(), b"unrelated command")
        self.assertEqual(instance.main.read_bytes(), self.original)
        self.assertNotIn(("reload",), self.desktop.commands)
        self.assertTrue(instance.report["rollback"]["restored"])

    def test_foreign_controller_and_module_are_not_clobbered(self):
        for name in ("controller", "module"):
            with self.subTest(name=name):
                instance = install.Installer(self.args)
                path = instance.controller_path if name == "controller" else instance.lua
                install.ensure(path.parent, private=name == "controller")
                path.write_bytes(b"unrelated contents")
                path.chmod(0o600)
                with self.assertRaisesRegex(service.Refused, "unrelated existing file"):
                    instance.run()
                self.assertEqual(path.read_bytes(), b"unrelated contents")
                self.assertEqual(instance.main.read_bytes(), self.original)
                self.assertNotIn(("reload",), self.desktop.commands)
                path.unlink()

    def test_config_errors_restore_main_before_removing_module(self):
        self.desktop.post_errors = True
        instance = install.Installer(self.args)
        original_atomic = install.atomic
        observed = []

        def observe(path, data, mode):
            observed.append((path, data))
            return original_atomic(path, data, mode)

        with patch.object(install, "atomic", side_effect=observe), self.assertRaisesRegex(service.Refused, "configuration errors appeared"):
            instance.run()
        self.assertEqual(instance.main.read_bytes(), self.original)
        self.assertFalse(instance.lua.exists())
        self.assertFalse(instance.controller_path.exists())
        self.assertFalse(instance.cli.exists())
        self.assertTrue(instance.report["rollback"]["restored"])
        self.assertEqual(self.desktop.commands.count(("reload",)), 2)
        self.assertIn((instance.main, self.original), observed)
        self.assertFalse(json.loads((instance.report_dir / "report.json").read_text())["installed"])

    def test_write_failure_after_replace_is_rolled_back(self):
        instance = install.Installer(self.args)
        original_atomic = install.atomic
        fired = False

        def partial(path, contents, mode):
            nonlocal fired
            original_atomic(path, contents, mode)
            if path == instance.controller_path and not fired:
                fired = True
                raise OSError("synthetic fsync error after replace")

        with patch.object(install, "atomic", side_effect=partial), self.assertRaisesRegex(OSError, "fsync error"):
            instance.run()
        self.assertFalse(instance.controller_path.exists())
        self.assertFalse(instance.release.exists())
        self.assertEqual(instance.main.read_bytes(), self.original)
        self.assertNotIn(("reload",), self.desktop.commands)

    def test_final_report_failure_rolls_back_successful_install(self):
        instance = install.Installer(self.args)
        original_atomic = install.atomic
        fired = False

        def refuse_first_report(path, contents, mode):
            nonlocal fired
            if path.name == "report.json" and not fired:
                fired = True
                raise OSError("synthetic report failure")
            return original_atomic(path, contents, mode)

        with patch.object(install, "atomic", side_effect=refuse_first_report), self.assertRaisesRegex(OSError, "report failure"):
            instance.run()
        self.assertEqual(instance.main.read_bytes(), self.original)
        self.assertFalse(instance.lua.exists())
        self.assertTrue(instance.report["rollback"]["restored"])
        self.assertFalse(json.loads((instance.report_dir / "report.json").read_text())["installed"])

    def test_concurrent_main_change_is_preserved_and_dependencies_retained(self):
        instance = install.Installer(self.args)
        original_setup = instance.setup_files
        changed_main = instance.main_bytes if hasattr(instance, "main_bytes") else b""

        def concurrent_change():
            original_setup()
            instance.main.write_bytes(b"-- user's concurrent edit\n")
            self.desktop.post_errors = True

        with patch.object(instance, "setup_files", side_effect=concurrent_change), self.assertRaises(service.Refused):
            instance.run()
        self.assertEqual(instance.main.read_bytes(), b"-- user's concurrent edit\n")
        self.assertFalse(instance.report["rollback"]["restored"])
        self.assertTrue(instance.lua.exists())
        self.assertTrue(instance.controller_path.exists())

    def test_malformed_managed_require_and_unsafe_directories_refuse(self):
        with self.assertRaises(service.Refused):
            install.main_config((install.BEGIN + '\nrequire("evil")\n' + install.END).encode())
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        with self.assertRaises(service.Refused):
            install.directory(unsafe)
        link = self.root / "linked"
        link.symlink_to(self.hypr)
        with self.assertRaises(service.Refused):
            install.directory(link)

    def test_lua_registers_start_only_and_shell_quotes_controller_path(self):
        folder = self.root / "lua-test"
        folder.mkdir(mode=0o700)
        path = self.root / "controller'quote.py"
        module = install.lua_module(path, self.source)
        (self.hypr / "hyprveil-settings.lua").write_bytes(service.lua_settings_source(service.manifest({
            "version": 1, "enabled": True, "plugin": str(self.source), "plugin_sha256": "a" * 64,
            "compositor_sha256": "b" * 64, "abi_hash": "test"})))
        install.validate_lua(module, self.original, folder)
        script = folder / "probe.lua"
        script.write_text("local callback; local commands = {}; local permission; hl = { get_config = function() return nil, true end, permission = function(rule) assert(not callback); assert(rule.type == 'plugin'); assert(rule.mode == 'allow'); permission = rule.binary end, on = function(event, fn) assert(permission); assert(event == 'hyprland.start'); callback = fn end, exec_cmd = function(cmd) table.insert(commands, cmd) end }; o = { shell_quote = function(value) return '\\'' .. value:gsub('\\'', '\\'\\\\\\'\\'') .. '\\'' end }; dofile(arg[1]); assert(#commands == 0); callback(); assert(#commands == 1); print(commands[1])")
        result = subprocess.run(["/usr/bin/lua", str(script), str(folder / "module.lua")], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(shlex.split(result.stdout.strip()), ["/usr/bin/python3", str(path), "start"])
        self.assertNotIn("o.shell_quote", module.decode())

    def test_permission_regex_matches_only_exact_pinned_release(self):
        import re
        path = Path("/home/user/.local/share/hyprveil/releases/abc/hyprveil.so")
        pattern = install.permission_pattern(path)
        self.assertTrue(re.fullmatch(pattern, str(path)))
        self.assertFalse(re.fullmatch(pattern, str(path).replace(".local", "xlocal")))
        self.assertFalse(re.fullmatch(pattern, str(path).replace(".so", "xso")))
        self.assertFalse(re.fullmatch(pattern, str(path) + ".other"))

    def test_managed_defaults_load_after_bootstrap_before_user_config_idempotently(self):
        before = b'dofile(os.getenv("HOME") .. "/.config/omarchy/bootstrap.lua")\nrequire("hypr.looknfeel")\n'
        first = install.main_config(before)
        self.assertLess(first.index(install.BLOCK.encode()), first.index(b'require("hypr.looknfeel")'))
        self.assertEqual(first, install.main_config(first))
        moved = install.main_config(before + b"\n" + install.BLOCK.encode() + b"\n")
        self.assertEqual(moved.count(install.BLOCK.encode()), 1)
        self.assertLess(moved.index(install.BLOCK.encode()), moved.index(b'require("hypr.looknfeel")'))

    def test_plain_hyprland_autoload_needs_no_omarchy_or_package_path(self):
        plain = b'hl.config({general={gaps_in=7}})\n'
        result = install.main_config(plain)
        self.assertTrue(result.startswith(install.BLOCK.encode()))
        self.assertNotIn(b'require("hypr.hyprveil")', result)
        self.assertEqual(install.main_config(result), result)

    def test_bootstrap_comment_does_not_precede_real_bootstrap(self):
        plain = b'-- Example: dofile("/default/hypr/bootstrap.lua")\ndofile("/default/hypr/bootstrap.lua")\n'
        result = install.main_config(plain)
        self.assertGreater(result.index(install.BLOCK.encode()), result.index(b'\ndofile('))

    def test_repeat_preserves_custom_lua_settings_bytes(self):
        first, _ = self.run_install()
        custom = first.lua_settings.read_bytes().replace(b"grain = 50", b"grain = 23") + b"\n-- user custom code\n"
        first.lua_settings.write_bytes(custom)
        self.desktop.commands.clear()
        second, _ = self.run_install()
        self.assertEqual(second.lua_settings.read_bytes(), custom)
        self.assertNotIn(("reload",), self.desktop.commands)

    def test_settings_lua_syntax_error_does_not_replace_release_or_main(self):
        first, _ = self.run_install()
        first.lua_settings.write_text("local broken = {\n")
        original = first.main.read_bytes()
        with self.assertRaisesRegex(service.Refused, "Lua settings syntax"):
            self.run_install()
        self.assertEqual(first.main.read_bytes(), original)
        self.assertEqual(first.lua_settings.read_text(), "local broken = {\n")


if __name__ == "__main__":
    unittest.main()
