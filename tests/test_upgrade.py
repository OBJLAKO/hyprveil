"""Exercise the secure native-config admission step before weak handoff."""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import service
import upgrade


class UpgradeCandidateTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hyprveil-upgrade-candidates-")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        (self.project / "build").mkdir(mode=0o700)
        self.plugin = self.project / "build/hyprveil.so"
        self.guard = self.project / "build/hyprveil-upgrade-guard.so"
        self.plugin.write_bytes(b"synthetic reviewed main release")
        self.guard.write_bytes(b"synthetic reviewed capture guard")
        self.args = SimpleNamespace(old_sha256=next(iter(upgrade.OLD_PINS)),
                                    plugin_sha256=upgrade.install.sha(self.plugin.read_bytes()),
                                    guard_sha256=upgrade.install.sha(self.guard.read_bytes()))
        location = patch.object(upgrade, "__file__", str(self.project / "tools/upgrade.py"))
        location.start()
        self.addCleanup(location.stop)

    def test_replaced_guard_bytes_refuse_before_selecting_compositor(self):
        original = upgrade.install.read_owned

        def replaced(path, *args, **kwargs):
            if path == self.guard:
                # Model make replacing a guard after an earlier pathname hash.
                self.guard.write_bytes(b"synthetic concurrently rebuilt guard")
            return original(path, *args, **kwargs)

        with patch.object(upgrade.install, "read_owned", side_effect=replaced), \
                patch.object(upgrade.service, "Controller", side_effect=AssertionError("unattested candidate reached compositor selection")) as controller:
            with self.assertRaisesRegex(service.Refused, "explicit tested pins"):
                upgrade.upgrade(self.args)
        controller.assert_not_called()
        self.assertFalse((self.project / "artifacts").exists())

    def test_both_admitted_releases_are_frozen_before_any_compositor_ipc(self):
        plugin_bytes, guard_bytes = self.plugin.read_bytes(), self.guard.read_bytes()
        original = upgrade.install.atomic

        def rebuilding(path, contents, mode):
            # Any later build output is independent of the admitted snapshot.
            self.plugin.write_bytes(b"synthetic next main build")
            self.guard.write_bytes(b"synthetic next guard build")
            return original(path, contents, mode)

        def before_ipc():
            folders = list((self.project / "artifacts").glob("protected-upgrade-*"))
            self.assertEqual(len(folders), 1)
            for name, expected in (("hyprveil.so", plugin_bytes), ("hyprveil-upgrade-guard.so", guard_bytes)):
                frozen = folders[0] / name
                self.assertEqual(frozen.read_bytes(), expected)
                self.assertEqual(frozen.stat().st_mode & 0o777, 0o400)
            raise service.Refused("synthetic stop before IPC")

        with patch.object(upgrade.install, "atomic", side_effect=rebuilding), \
                patch.object(upgrade.service, "Controller", side_effect=before_ipc):
            with self.assertRaisesRegex(service.Refused, "stop before IPC"):
                upgrade.upgrade(self.args)


class NativeAdmissionTests(TestCase):
    def test_native_file_choice_is_overridden_and_black_is_queried(self):
        controller = Mock()
        controller.native.side_effect = [{"mode": "black", "config_api": 1}, {"mode": "black"}]
        result = upgrade.black_before_handoff(controller)
        self.assertEqual(result, {"mode": "black", "config_api": 1})
        self.assertEqual([call.args for call in controller.native.call_args_list], [("black",), ("status",)])

    def test_nonblack_ack_cannot_authorize_adoption(self):
        controller = Mock()
        controller.native.return_value = {"mode": "spoiler"}
        with self.assertRaisesRegex(service.Refused, "black before privacy handoff"):
            upgrade.black_before_handoff(controller)
        controller.native.assert_called_once_with("black")

    def test_intervening_native_change_cannot_authorize_adoption(self):
        controller = Mock()
        controller.native.side_effect = [{"mode": "black"}, {"mode": "omit"}]
        with self.assertRaisesRegex(service.Refused, "black before privacy handoff"):
            upgrade.black_before_handoff(controller)

    def test_failed_black_setter_stops_before_status_or_handoff(self):
        controller = Mock()
        controller.native.side_effect = service.Refused("refused black")
        with self.assertRaisesRegex(service.Refused, "refused black"):
            upgrade.black_before_handoff(controller)
        controller.native.assert_called_once_with("black")

    def test_active_native_appearance_wins_over_stale_manifest(self):
        controller = Mock()
        active = dict(service.DEFAULT_APPEARANCE, variant="signal", grain=71)
        controller.settings = {"appearance": dict(service.DEFAULT_APPEARANCE), "image_path": ""}
        controller.native.return_value = {"mode": "spoiler", "appearance": active}
        self.assertEqual(upgrade.preserved_selection(controller), ("spoiler", "", active))

    def test_predecessor_without_appearance_uses_validated_legacy_defaults(self):
        controller = Mock()
        controller.settings = {"image_path": ""}
        controller.native.return_value = {"mode": "black"}
        self.assertEqual(upgrade.preserved_selection(controller), ("black", "", service.DEFAULT_APPEARANCE))

    def test_native_image_selection_preserves_actual_lua_path_not_stale_json(self):
        controller = Mock()
        active = dict(service.DEFAULT_APPEARANCE, variant="signal", color="#abcdef", speed=0)
        actual = "/tmp/synthetic-current-image.png"
        controller.settings = {"image_path": "/tmp/synthetic-obsolete-image.png", "appearance": service.DEFAULT_APPEARANCE}
        controller.native.return_value = {"config_api": 1, "mode": "image", "image_path": actual, "appearance": active}
        controller.safe_image.side_effect = lambda path: Path(path)
        self.assertEqual(upgrade.preserved_selection(controller), ("image", actual, active))
        controller.safe_image.assert_called_once_with(actual)

    def test_native_image_selection_never_falls_back_to_manifest_when_actual_is_empty(self):
        controller = Mock()
        controller.settings = {"image_path": "/tmp/synthetic-obsolete-image.png", "appearance": service.DEFAULT_APPEARANCE}
        controller.native.return_value = {"config_api": 1, "mode": "image", "image_path": "", "appearance": service.DEFAULT_APPEARANCE}
        controller.safe_image.side_effect = service.Refused("synthetic invalid actual image")
        with self.assertRaisesRegex(service.Refused, "invalid actual image"):
            upgrade.preserved_selection(controller)
        controller.safe_image.assert_called_once_with("")

    def test_native_actual_image_is_admitted_before_any_upgrade_mutation(self):
        controller = Mock()
        actual = "/tmp/synthetic-unreadable-image.png"
        controller.settings = {"image_path": "/tmp/synthetic-obsolete-image.png", "appearance": service.DEFAULT_APPEARANCE}
        controller.native.return_value = {"config_api": 1, "mode": "image", "image_path": actual, "appearance": service.DEFAULT_APPEARANCE}
        controller.safe_image.side_effect = service.Refused("synthetic image admission refusal")
        with self.assertRaisesRegex(service.Refused, "image admission refusal"):
            upgrade.preserved_selection(controller)
        controller.native.assert_called_once_with("status")
        controller.safe_image.assert_called_once_with(actual)

    def test_legacy_image_selection_uses_admitted_manifest_path(self):
        controller = Mock()
        saved = "/tmp/synthetic-legacy-image.png"
        controller.settings = {"image_path": saved, "appearance": service.DEFAULT_APPEARANCE}
        controller.native.return_value = {"mode": "image", "appearance": service.DEFAULT_APPEARANCE}
        controller.safe_image.side_effect = lambda path: Path(path)
        self.assertEqual(upgrade.preserved_selection(controller), ("image", saved, service.DEFAULT_APPEARANCE))
        controller.safe_image.assert_called_once_with(saved)

    def test_native_inactive_image_path_is_preserved_for_every_nonimage_mode(self):
        # A valid configured path can be unavailable until image mode is chosen.
        # Preserve that setting without attempting an unnecessary image decode.
        actual = "/tmp/synthetic-preconfigured-image.png"
        active = dict(service.DEFAULT_APPEARANCE, grain=93)
        for mode in ("black", "spoiler", "omit"):
            with self.subTest(mode=mode):
                controller = Mock()
                controller.settings = {"image_path": "/tmp/synthetic-stale-image.png", "appearance": service.DEFAULT_APPEARANCE}
                controller.native.return_value = {"config_api": 1, "mode": mode, "image_path": actual, "appearance": active}
                self.assertEqual(upgrade.preserved_selection(controller), (mode, actual, active))
                controller.safe_image.assert_not_called()

    def test_native_empty_inactive_path_does_not_restore_stale_manifest_path(self):
        controller = Mock()
        controller.settings = {"image_path": "/tmp/synthetic-stale-image.png", "appearance": service.DEFAULT_APPEARANCE}
        controller.native.return_value = {"config_api": 1, "mode": "spoiler", "image_path": "", "appearance": service.DEFAULT_APPEARANCE}
        self.assertEqual(upgrade.preserved_selection(controller), ("spoiler", "", service.DEFAULT_APPEARANCE))
        controller.safe_image.assert_not_called()
