import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import service
import session


class MockTrial(service.Controller):
    def __init__(self):
        super().__init__(signature="synthetic-instance")
        self.loaded = False
        self.mode = "black"
        self.commands = []
        self.mapping_matches = True
        self.started = "synthetic-start"
        self.invalid_native = False
        self.appearance = dict(service.DEFAULT_APPEARANCE)

    def select_target(self):
        return service.Target(313, self.signature, self.started)

    def mapped_release(self):
        return self.mapping_matches

    def raw(self, *args):
        self.commands.append(args)
        if args == ("-j", "version"):
            return json.dumps({"abiHash": "tested-abi"})
        if args == ("-j", "plugin", "list"):
            return json.dumps([{"name": "hyprveil"}] if self.loaded else [])
        if args == ("configerrors",):
            return ""
        if args[0] == "eval":
            return "ok"
        if args[:2] == ("plugin", "load"):
            self.loaded = True
            self.mode = "black"
            return "ok"
        if args[:2] == ("plugin", "unload"):
            self.loaded = False
            return "ok"
        if args[0] == "hyprveil":
            if args[1] == "appearance":
                if len(args) > 2:
                    self.appearance = {"variant": args[2], "color": args[3], "grain": int(args[4]),
                                       "speed": int(args[5]), "darkness": int(args[6]), "eye": args[7] == "1", "eye_size": int(args[8])}
                    self.appearance.update(icon=args[9] if len(args) == 11 else "eye", icon_opacity=int(args[10]) if len(args) == 11 else 75)
            elif args[1] != "status":
                self.mode = args[1]
            if self.invalid_native:
                return '{"error":"synthetic native refusal"}'
            return json.dumps({"session": "live", "mode": self.mode, "local_dump": "disabled-in-live", "appearance": self.appearance,
                               "icon": self.appearance["icon"], "icon_opacity": self.appearance["icon_opacity"]})
        raise AssertionError(args)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hyprveil-session-test-")
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.project = self.root / "project"
        self.project.mkdir(mode=0o700)
        (self.project / "build").mkdir(mode=0o700)
        (self.project / "artifacts").mkdir(mode=0o755)
        self.binary = self.project / "build/hyprveil.so"
        self.binary.write_bytes(b"synthetic trial binary; not executable")
        self.binary.chmod(0o400)
        self.state_path = self.project / "artifacts/live-session.json"
        self.patches = [patch.object(service.Path, "home", return_value=self.root),
                        patch.object(service, "standard_runtime", return_value=self.runtime),
                        patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)}),
                        patch.object(session, "PROJECT", self.project), patch.object(session, "STATE", self.state_path)]
        for item in self.patches:
            item.start()
        self.desktop = MockTrial()

        def factory(config=None, signature=None):
            self.desktop.signature = signature or "synthetic-instance"
            return self.desktop

        original = service.digest_file
        self.patches.extend([patch.object(service, "Controller", side_effect=factory),
                             patch.object(service, "digest_file", side_effect=lambda path, *args, **kwargs:
                                          "b" * 64 if kwargs.get("process_executable") or path == Path("/usr/bin/Hyprland") else original(path, *args, **kwargs))])
        for item in self.patches[-2:]:
            item.start()
        self.state = {"pid": 313, "signature": "synthetic-instance", "runtime": str(self.runtime), "abi_hash": "tested-abi",
                      "started": "synthetic-start", "plugin": str(self.binary), "plugin_sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
                      "compositor_sha256": "b" * 64, "marker": str(self.runtime / ".hyprveil-live-313")}
        self.args = argparse.Namespace(pid=313, signature="synthetic-instance", abi_hash="tested-abi")

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def save(self, state=None):
        self.state_path.write_text(json.dumps(state or self.state))
        self.state_path.chmod(0o600)

    def marker(self, signature="synthetic-instance"):
        path = Path(self.state["marker"])
        path.write_text(f"hyprveil-live-v1\n313\n{signature}\nblack\n1\n")
        path.chmod(0o600)
        return path

    def test_private_state_rejects_links_fifo_and_oversize(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(self.state))
        outside.chmod(0o600)
        for kind in ("symlink", "hardlink", "fifo", "oversize"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.state_path.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, self.state_path)
                elif kind == "fifo":
                    os.mkfifo(self.state_path, 0o600)
                else:
                    self.state_path.write_bytes(b"x" * 65537)
                    self.state_path.chmod(0o600)
                with self.assertRaises((OSError, service.Refused)):
                    session.read_state()
                self.state_path.unlink()
        self.assertEqual(self.desktop.commands, [])

    def test_prepare_recovers_stale_identity_and_replaces_own_expired_marker(self):
        self.save(dict(self.state, pid=555, signature="old-instance"))
        marker = self.marker()
        result = session.prepare(self.args)
        saved = session.read_state()
        self.assertTrue(result["prepared"])
        self.assertEqual(saved["pid"], 313)
        self.assertEqual(saved["compositor_sha256"], "b" * 64)
        self.assertEqual(Path(saved["plugin"]).stat().st_mode & 0o777, 0o400)
        self.assertIn("synthetic-instance", marker.read_text())
        self.assertNotIn(("plugin", "load", str(self.binary)), self.desktop.commands)

    def test_prepare_does_not_swallow_loaded_module_refusal(self):
        self.save()
        before = self.state_path.read_bytes()
        self.desktop.loaded = True
        with self.assertRaisesRegex(service.Refused, "unload the existing"):
            session.prepare(self.args)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertFalse(Path(self.state["marker"]).exists())

    def test_prepare_uses_random_directories_and_can_repeat_without_timestamp_collision(self):
        first = session.prepare(self.args)
        second = session.prepare(self.args)
        self.assertNotEqual(first["plugin"], second["plugin"])
        self.assertTrue(Path(first["plugin"]).exists())

    def test_failed_state_write_cancels_only_new_owned_marker(self):
        original = session.atomic

        def refuse_state(path, data, mode):
            if path == self.state_path:
                raise OSError("synthetic state write failure")
            return original(path, data, mode)

        with patch.object(session, "atomic", side_effect=refuse_state), self.assertRaisesRegex(OSError, "state write failure"):
            session.prepare(self.args)
        self.assertEqual(Path(self.state["marker"]).read_text().splitlines()[3:], ["cancelled", "0"])

    def test_foreign_mapping_refuses_every_mode_and_unload(self):
        self.desktop.loaded = True
        self.desktop.mapping_matches = False
        for action in ("status", "black", "omit", "unload"):
            with self.subTest(action=action), self.assertRaisesRegex(service.Refused, "exact configured release"):
                session.execute(self.state, action)
        self.assertFalse(any(args[0] == "hyprveil" or args[:2] == ("plugin", "unload") for args in self.desktop.commands))

    def test_unload_masks_then_unloads_and_cleans_exact_marker(self):
        self.desktop.loaded = True
        self.desktop.mode = "omit"
        marker = self.marker()
        result = session.execute(self.state, "unload")
        self.assertFalse(result["loaded"])
        self.assertEqual(result["mode"], "native")
        self.assertLess(self.desktop.commands.index(("hyprveil", "black")), self.desktop.commands.index(("plugin", "unload", str(self.binary))))
        self.assertEqual(marker.read_text().splitlines()[3:], ["cancelled", "0"])
        self.assertFalse(session.execute(self.state, "unload")["loaded"])

    def test_unload_never_removes_unknown_marker_or_arbitrary_state_path(self):
        marker = self.marker("foreign-signature")
        contents = marker.read_text()
        with self.assertRaisesRegex(service.Refused, "selected session"):
            session.execute(self.state, "unload")
        self.assertEqual(marker.read_text(), contents)
        outside = self.root / "do-not-delete"
        outside.write_text("keep")
        with self.assertRaisesRegex(service.Refused, "marker does not match"):
            session.execute(dict(self.state, marker=str(outside)), "unload")
        self.assertEqual(outside.read_text(), "keep")

    def test_native_error_json_is_failure_and_requests_black(self):
        self.desktop.loaded = True
        self.desktop.invalid_native = True
        with self.assertRaisesRegex(service.Refused, "native refusal"):
            session.execute(self.state, "omit")
        self.assertEqual(self.desktop.mode, "black")

    def test_reused_pid_starttime_change_refuses_controls(self):
        self.desktop.loaded = True
        self.desktop.started = "different-start"
        with self.assertRaisesRegex(service.Refused, "process identity changed"):
            session.execute(self.state, "black")
        self.assertFalse(any(args[0] == "hyprveil" for args in self.desktop.commands))

    def test_new_load_requires_elf_pin_but_legacy_trial_can_be_unloaded(self):
        old = dict(self.state)
        old.pop("compositor_sha256")
        with self.assertRaisesRegex(service.Refused, "no compositor ELF pin"):
            session.execute(old, "load")
        self.assertFalse(self.desktop.loaded)
        self.desktop.loaded = True
        self.assertFalse(session.execute(old, "unload")["loaded"])

    def test_load_verifies_black_and_exact_release(self):
        result = session.execute(self.state, "load")
        self.assertEqual(result["mode"], "black")
        self.assertTrue(self.desktop.loaded)
        self.assertIn(("plugin", "load", str(self.binary)), self.desktop.commands)
        self.assertFalse(any(args[0] == "eval" for args in self.desktop.commands))

    def test_trial_load_applies_saved_appearance_without_changing_black_mode(self):
        wanted = dict(service.DEFAULT_APPEARANCE, variant="signal", color="#123456", eye=False, speed=0)
        result = session.execute(dict(self.state, appearance=wanted), "load")
        self.assertEqual(result["appearance"], wanted)
        self.assertEqual(result["mode"], "black")
        self.assertIn(("hyprveil", "appearance", "signal", "#123456", "50", "0", "50", "0", "80", "eye", "75"), self.desktop.commands)

    def test_trial_partial_configure_rereads_latest_settings_and_preserves_mode(self):
        self.save()
        self.desktop.loaded = True
        self.desktop.mode = "spoiler"
        first = session.execute(self.state, "configure", appearance={"color": "#AABBCC", "grain": 0})
        second = session.execute(self.state, "configure", appearance={"speed": 200, "eye": False})
        wanted = dict(service.DEFAULT_APPEARANCE, color="#aabbcc", grain=0, speed=200, eye=False)
        self.assertEqual(first["mode"], "spoiler")
        self.assertEqual(second["appearance"], wanted)
        self.assertEqual(session.read_state()["appearance"], wanted)
        self.assertEqual(second["mode"], "spoiler")

    def test_trial_configure_state_failure_requests_black_and_keeps_saved_values(self):
        self.save()
        self.desktop.loaded = True
        self.desktop.mode = "spoiler"
        before = self.state_path.read_bytes()
        with patch.object(session, "atomic", side_effect=OSError("synthetic disk full")), self.assertRaises(OSError):
            session.execute(self.state, "configure", appearance={"darkness": 0})
        self.assertEqual(self.desktop.mode, "black")
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_trial_configure_refuses_replaced_identity_before_native_command(self):
        self.save(dict(self.state, plugin_sha256="a" * 64))
        self.desktop.loaded = True
        with self.assertRaisesRegex(service.Refused, "trial changed"):
            session.execute(self.state, "configure", appearance={"grain": 0})
        self.assertEqual(self.desktop.commands, [])

    def test_async_load_timeout_keeps_cancelled_tombstone_for_delayed_admission(self):
        marker = self.marker()
        original = self.desktop.raw

        def timeout(*args):
            if args[:2] == ("plugin", "load"):
                raise service.Refused("synthetic permission prompt IPC timed out")
            return original(*args)

        with patch.object(self.desktop, "raw", side_effect=timeout), self.assertRaisesRegex(service.Refused, "timed out"):
            session.execute(self.state, "load")
        self.assertEqual(marker.read_text().splitlines()[3:], ["cancelled", "0"])
        self.assertFalse(self.desktop.loaded)

    def test_upgrade_refuses_load_without_preventing_existing_module_unload(self):
        original = service.digest_file
        with patch.object(service, "digest_file", side_effect=lambda path, *args, **kwargs:
                          "c" * 64 if path == Path("/usr/bin/Hyprland") else original(path, *args, **kwargs)):
            with self.assertRaisesRegex(service.Refused, "compositor ELF changed"):
                session.execute(self.state, "load")
            self.desktop.loaded = True
            self.assertFalse(session.execute(self.state, "unload")["loaded"])


if __name__ == "__main__":
    unittest.main()
