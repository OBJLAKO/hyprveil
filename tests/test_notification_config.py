import unittest
import test_service as fixtures

service = fixtures.service


class NotificationConfiguration(unittest.TestCase):
    def values(self):
        return dict(mode="spoiler", image_path="", **service.DEFAULT_APPEARANCE,
                    **service.DEFAULT_RENDER, **{"notification_" + key: value
                                                for key, value in service.NOTIFICATION_DEFAULTS.items()})

    def test_independent_lua_roundtrip(self):
        values = self.values()
        values.update(variant="error404", notification_enabled=True,
                      notification_variant="matrix", notification_mode="spoiler")
        parsed = service.parse_lua_settings(service.lua_settings_block(values).encode())[3]
        self.assertEqual(parsed, values)
        self.assertEqual(parsed["notification_rounding"], -1)

    def test_partial_profiles_do_not_reset_each_other(self):
        values = self.values()
        values.update(notification_color="#123456", variant="error404")
        status = dict(mode=values["mode"], image_path="", appearance=service.DEFAULT_APPEARANCE,
                      **service.DEFAULT_RENDER, notifications=dict(api=1, **{
                          key.removeprefix("notification_"): values[key] for key in service.NOTIFICATION_FIELDS}))
        patch = service.normalize_config_patch({"notification_mode": "color"}, status)
        self.assertEqual(patch, {"notification_mode": "color"})
        changed = service.config_values(dict(values, **patch))
        self.assertEqual(changed["variant"], "error404")
        self.assertEqual(changed["notification_color"], "#123456")

    def test_old_core_refuses_new_settings(self):
        status = dict(mode="black", image_path="", appearance=service.DEFAULT_APPEARANCE,
                      **service.DEFAULT_RENDER)
        with self.assertRaises(service.Refused):
            service.normalize_config_patch({"notification_enabled": True}, status)
        self.assertEqual(len(service.native_values(status)), len(service.CONFIG_FIELDS))

    def test_strict_notification_metadata(self):
        for patch in ({"notification_enabled": 1}, {"notification_rounding": True},
                      {"notification_rounding": -2}, {"notification_namespace": ".*"},
                      {"notification_namespace": ""}, {"notification_namespace": "x" * 129},
                      {"notification_image_fit": "unknown"}, {"notification_color": "red"}):
            with self.subTest(patch=patch), self.assertRaises(service.Refused):
                service.normalize_config_patch(patch)

    def test_partial_saved_profile_is_refused(self):
        values = self.values()
        del values["notification_namespace"]
        with self.assertRaises(service.Refused):
            service.config_values(values)

    def test_unrelated_negative_literal_is_refused(self):
        block = service.lua_settings_block(self.values()).replace("grain = 50", "grain = -1")
        with self.assertRaises(service.Refused):
            service.parse_lua_settings(block.encode())
