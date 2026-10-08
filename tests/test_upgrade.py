"""Exercise the secure native-config admission step before weak handoff."""
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import service
import upgrade


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
        active = dict(service.DEFAULT_APPEARANCE, variant="telegram", grain=71)
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
        active = dict(service.DEFAULT_APPEARANCE, variant="telegram", color="#abcdef", speed=0)
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
