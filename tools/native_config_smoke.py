#!/usr/bin/env python3
"""Validate native Hyprland settings and Lua callbacks in a synthetic lab.

The parent Wayland socket only hosts the owned nested compositor. Every test
control call and capture reattests its private marker and owning process.
No personal compositor, user settings or personal screen contents are read.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys

import cairo

from lab import PROJECT
from service import APPEARANCE_ALIASES, APPEARANCE_VARIANTS, DEFAULT_APPEARANCE, validate_appearance
from spoiler_smoke import PRIVATE, SpoilerSmoke

sys.path.insert(0, str(PROJECT / "tests"))
from verify_capture import absence_report, color_fraction, differing_fraction, opacity_fraction

LEGACY_APPEARANCE = ("variant", "color", "grain", "speed", "darkness", "eye", "eye_size")
FIELDS = ("mode", "image_path", *LEGACY_APPEARANCE, "icon", "icon_opacity")
DEFAULTS = dict(mode="black", image_path="", **DEFAULT_APPEARANCE)
CALLBACKS = ("configure", "status", "mode", "cycle", "active_privacy", "set_hidden", "toggle", "reset_sharing")


def lua_literal(value):
    """Render only exact test values without allowing Lua source injection."""
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, str):
        return '"' + "".join(chr(byte) if 32 <= byte < 127 and byte not in (34, 92)
                             else "\\%03d" % byte for byte in value.encode("utf-8")) + '"'
    if isinstance(value, dict):
        return "{" + ",".join("[" + lua_literal(key) + "]=" + lua_literal(item)
                                for key, item in value.items()) + "}"
    raise TypeError("unsupported test Lua literal")


def flatten_status(value):
    if not isinstance(value, dict) or value.get("config_api") != 1 or not isinstance(value.get("appearance"), dict):
        raise RuntimeError("native status does not expose config_api 1 and appearance")
    if value["appearance"].keys() != set(LEGACY_APPEARANCE):
        raise RuntimeError("native appearance breaks the original seven-field reader schema")
    appearance = dict(value["appearance"], icon=value.get("icon"), icon_opacity=value.get("icon_opacity"))
    result = {"mode": value.get("mode"), "image_path": value.get("image_path"),
              **validate_appearance(appearance, canonical=True)}
    if result.keys() != set(FIELDS):
        raise RuntimeError("native settings schema differs from the exact eleven fields")
    return result


class NativeConfigSmoke(SpoilerSmoke):
    def __init__(self, args):
        args.force_shader_failure = False
        super().__init__(args)
        args.suite = "native-config"
        self.report["native_settings_scope"] = "eleven typed config values, atomic Lua patch, commands, dispatchers and callback teardown"
        self.base_config = None

    def native(self):
        return json.loads(self.ctl("hyprveil", "status"))

    def lua(self, expression):
        return self.ctl("repl", expression)

    def lua_settings(self, getter):
        if getter == "status":
            source = "local v=hl.plugin.hyprveil.status(); local a=v.appearance; local count=0; "
            source += "for k in pairs(a) do count=count+1 end; if count~=7 then error('legacy appearance shape changed') end; "
            values = ["v.mode", "v.image_path"] + ["a." + key for key in LEGACY_APPEARANCE] + ["v.icon", "v.icon_opacity"]
        else:
            source = ""
            values = ["hl.get_config(" + lua_literal("plugin.hyprveil." + key) + ")" for key in FIELDS]
        output = self.lua(source + "return table.concat({" + ",".join("tostring(" + value + ")" for value in values) + '},"\\t")')
        tokens = output.split("\t")
        if len(tokens) != len(FIELDS):
            raise RuntimeError("Lua settings query returned an unexpected field count: " + output)
        result = dict(zip(FIELDS, tokens))
        for key in ("grain", "speed", "darkness", "eye_size", "icon_opacity"):
            result[key] = int(result[key])
        if result["eye"] not in ("true", "false"):
            raise RuntimeError("Lua eye setting is not boolean")
        result["eye"] = result["eye"] == "true"
        return result

    def option_settings(self):
        result = {}
        for key in FIELDS:
            value = json.loads(self.ctl("-j", "getoption", "plugin:hyprveil:" + key))
            if key in ("mode", "image_path", "variant", "color", "icon"):
                result[key] = value["str"]
            elif key == "eye":
                if "bool" in value:
                    result[key] = value["bool"]
                else:
                    if value.get("int") not in (0, 1):
                        raise RuntimeError("getoption returned a nonboolean eye")
                    result[key] = bool(value["int"])
            else:
                result[key] = value["int"]
        return result

    def settings_agree(self, label, expected):
        native = flatten_status(self.native())
        lua = self.lua_settings("status")
        configured = self.lua_settings("get_config")
        options = self.option_settings()
        self.assert_check(label + ": every native setting route agrees", native == lua == configured == options == expected,
                          native=native, lua=lua, get_config=configured, getoption=options, expected=expected)

    def patch(self, value):
        output = self.lua("local v,e=hl.plugin.hyprveil.configure(" + lua_literal(value) + "); "
                          'if not v then error(e) end; return v.mode')
        if output != self.native()["mode"]:
            raise RuntimeError("Lua configure acknowledgement differs from actual native mode")

    def plugin_dispatch(self, name, *arguments):
        # Installed Lua Hyprland dispatches a Lua function. The plugin bridge
        # directly calls its registered native dispatcher without eval/IPC.
        expression = "function() local v,e=hl.plugin.hyprveil." + name + "(" + ",".join(lua_literal(value) for value in arguments) + "); if not v then error(e) end end"
        return self.ctl("dispatch", expression)

    def write_config(self, value):
        config = self.runtime / "hyprland.lua"
        config.write_text(self.base_config + "\nhl.config({plugin={hyprveil=" + lua_literal(value) + "}})\n")
        self.ctl("reload")

    def private_frame(self, label, before, black=False):
        image = self.capture_image(label)
        self.assert_check(label + ": opaque private capture leaves local geometry and flag unchanged",
                          absence_report(image, PRIVATE, 8)["ok"] and
                          opacity_fraction(image, (0, 0, image.width, image.height)) == 1 and
                          self.privacy() and self.geometry() == before and
                          (not black or color_fraction(image, self.roi(), (0, 0, 0), 0) == 1))
        return image

    def rejection(self, index, argument):
        before = self.native()
        geometry = self.geometry()
        output = self.lua("local v,e=hl.plugin.hyprveil.configure(" + argument + "); "
                          'return tostring(v==nil and type(e)=="string" and #e>0)')
        after = self.native()
        self.assert_check("invalid Lua patch %02d rejects without any partial change" % index,
                          output == "true" and flatten_status(after) == flatten_status(before) and
                          before["policy_generation"] == after["policy_generation"] and
                          self.geometry() == geometry and self.privacy(), argument=argument[:160], argument_length=len(argument))

    def icon_tests(self, before, restore):
        # A frozen opaque black material isolates the content-free icon pixels.
        # Export checks still inspect the entire frame for private fixture RGB.
        selected = dict(DEFAULTS, mode="spoiler", variant="matte", color="#ffffff",
                        grain=0, speed=0, darkness=100, eye=True, eye_size=128,
                        icon="none", icon_opacity=100)
        self.patch(selected)
        self.settings_agree("native icon none has independent typed settings", selected)
        off = self.private_frame("icon-none-black", before, black=True)
        roi = self.roi()
        icons = {}
        attempts = self.native()["spoiler_shader_attempts"]
        for shape in ("eye", "lock", "shield"):
            self.patch({"icon": shape})
            selected["icon"] = shape
            self.settings_agree("native " + shape + " shape routes agree", selected)
            image = self.private_frame("icon-" + shape + "-opacity100", before)
            changed = round(differing_fraction(off, roi, image, roi, 2) * roi[2] * roi[3])
            self.assert_check(shape + " draws visible content-free pixels on opaque black", changed > 50,
                              changed_pixels=changed)
            icons[shape] = image
        for first, second in (("eye", "lock"), ("eye", "shield"), ("lock", "shield")):
            changed = round(differing_fraction(icons[first], roi, icons[second], roi, 2) * roi[2] * roi[3])
            self.assert_check(first + " and " + second + " have distinguishable native vector geometry", changed > 50,
                              changed_pixels=changed)
        bright = sum(sum(pixel) for pixel in icons["shield"].pixels(roi))
        self.patch({"icon_opacity": 25})
        faint = self.private_frame("icon-shield-opacity25", before)
        faint_energy = sum(sum(pixel) for pixel in faint.pixels(roi))
        self.assert_check("icon opacity changes displayed brightness without transparency or shader recompilation",
                          0 < faint_energy < bright * .65 and self.native()["spoiler_shader_attempts"] == attempts,
                          full_energy=bright, quarter_energy=faint_energy)
        for label, patch in (("opacity-zero", {"icon_opacity": 0}),
                             ("legacy-eye-false", {"icon_opacity": 100, "eye": False}),
                             ("none-selected", {"eye": True, "icon": "none"})):
            self.patch(patch)
            image = self.private_frame("icon-" + label, before, black=True)
            self.assert_check(label + " hides the chosen vector exactly", differing_fraction(off, roi, image, roi, 0) == 0)
        self.patch({"icon": "eye", "eye_size": 40})
        small = self.private_frame("icon-eye-size40", before)
        small_pixels = round(differing_fraction(off, roi, small, roi, 2) * roi[2] * roi[3])
        large_pixels = round(differing_fraction(off, roi, icons["eye"], roi, 2) * roi[2] * roi[3])
        self.assert_check("generic icon size preserves visible geometry at both bounds",
                          small_pixels > 10 and large_pixels > small_pixels * 2,
                          small_pixels=small_pixels, large_pixels=large_pixels)

        # Existing seven-field clients must not reset a newly chosen shape.
        self.patch({"icon": "shield", "icon_opacity": 31})
        original = ["telegram", "#abcdef", "17", "0", "100", "1", "80"]
        self.ctl("hyprveil", "appearance", *original)
        expected = dict(DEFAULTS, mode="spoiler", variant="signal", color="#abcdef", grain=17,
                        speed=0, darkness=100, eye=True, eye_size=80, icon="shield", icon_opacity=31)
        self.settings_agree("legacy seven-field command canonicalizes alias and preserves icon choices", expected)
        self.ctl("hyprveil", "appearance", *original, "lock", "64")
        expected.update(icon="lock", icon_opacity=64)
        self.settings_agree("extended nine-field command selects icon and opacity atomically", expected)
        self.patch(restore)
        self.settings_agree("icon checks restore full caller configuration", restore)

    def variant_tests(self, before, restore):
        variants = ("prism", "signal", "aurora", "contour", "radar", "matte",
                    "error404", "matrix", "anonymous", "glass")
        self.assert_check("public native material enum preserves the original six and appends all four new styles",
                          tuple(APPEARANCE_VARIANTS) == variants)
        aliases = {"satin": "prism", "telegram": "signal", "grid": "radar", "404": "error404",
                   "cmatrix": "matrix", "anon": "anonymous", "liquid-glass": "glass", "liquidglass": "glass"}
        self.assert_check("material aliases remain explicit and bounded", APPEARANCE_ALIASES == aliases)
        selected = dict(restore, mode="spoiler", color="#ffffff", speed=0, darkness=35, eye=False)
        self.patch(selected)
        for variant in variants:
            # Updating one enum field must preserve the other ten native fields
            # in both typed settings and rendered capture behavior.
            self.patch({"variant": variant})
            selected["variant"] = variant
            self.settings_agree("native " + variant + " single-field selection preserves all eleven values", selected)
            self.private_frame("enum-" + variant, before)
        for alias, canonical in APPEARANCE_ALIASES.items():
            self.patch({"variant": alias})
            selected["variant"] = canonical
            self.settings_agree("legacy " + alias + " alias canonicalizes in every native route", selected)
            self.private_frame("enum-alias-" + alias, before)
        self.patch(restore)
        self.settings_agree("material enum checks restore full caller configuration", restore)

    def focused_privacy(self):
        return json.loads(self.ctl("hyprveil", "active-privacy"))

    def focus_window(self, window):
        self.dispatch('hl.dsp.focus({window=' + lua_literal("address:" + window["address"]) + '})')

    def window_identity(self, window):
        return window["address"], str(int(window["stableId"], 16))

    def tag_window(self, window, enabled):
        self.dispatch('hl.dsp.window.tag({window=' + lua_literal("address:" + window["address"]) +
                      ',tag=' + lua_literal(("+" if enabled else "-") + "hyprveil-private-test") + '})')

    def native_private(self, window):
        return self.ctl("getprop", "address:" + window["address"], "no_screen_share") == "true"

    def helper(self, name, *arguments):
        expression = "local v,e=hl.plugin.hyprveil." + name + "(" + ",".join(lua_literal(value) for value in arguments) + "); if not v then error(e) end; "
        expression += 'local allowed={state=true,address=true,stable_id=true,native_private=true,inherited=true}; local count=0; for k in pairs(v) do if not allowed[k] then error("unexpected privacy metadata") end; count=count+1 end; if count~=5 then error("incomplete privacy snapshot") end; '
        expression += 'return table.concat({v.state,v.address,v.stable_id,tostring(v.native_private),tostring(v.inherited)},"\\t")'
        result = self.lua(expression).split("\t")
        if len(result) != 5 or result[3] not in ("true", "false") or result[4] not in ("true", "false"):
            raise RuntimeError("window helper response differs from exact five-field privacy schema")
        value = dict(zip(("state", "address", "stable_id", "native_private", "inherited"), result))
        value["native_private"] = value["native_private"] == "true"
        value["inherited"] = value["inherited"] == "true"
        if value != self.focused_privacy():
            raise RuntimeError("window helper acknowledgement differs from actual atomic privacy status")
        return value

    def all_privacy(self):
        state = json.loads(self.ctl("hyprveil", "fixture-privacy"))
        return {item["address"]: {key: item[key] for key in ("native_private", "effective_private", "retained_private")}
                for item in state["windows"]}

    def helper_rejection(self, label, name, argument):
        before, geometry, policy = self.all_privacy(), self.geometry(), self.native()["policy_generation"]
        result = self.lua("local v,e=hl.plugin.hyprveil." + name + "(" + argument + "); "
                          'return tostring(v==nil and type(e)=="string" and #e>0)')
        self.assert_check(label, result == "true" and before == self.all_privacy() and geometry == self.geometry() and
                          policy == self.native()["policy_generation"], argument=argument[:160], argument_length=len(argument))

    def shared_frame(self, label, before):
        image = self.capture_image(label)
        self.assert_check(label + ": explicit synthetic sharing reveals only the chosen fixture without geometry changes",
                          color_fraction(image, self.roi(), PRIVATE, 3) >= 0.99 and
                          opacity_fraction(image, (0, 0, image.width, image.height)) == 1 and
                          not self.privacy() and self.geometry() == before)

    def per_window_tests(self, before):
        parent = self.protected()
        public = next(item for item in self.clients() if item["class"] == "org.hyprveil.fixture.background")
        address, stable = self.window_identity(parent)
        self.focus_window(parent)
        actual = self.helper("active_privacy")
        self.assert_check("native per-window query exposes exact focused protected identity",
                          actual == {"state": "hidden", "address": address, "stable_id": stable,
                                     "native_private": True, "inherited": False})
        values = [lua_literal(address), lua_literal(stable), "false"]
        for label, arguments in (("wrong address", [lua_literal("0x1"), *values[1:]]),
                                 ("wrong stable identity", [values[0], lua_literal(str(int(stable) + 1)), "false"]),
                                 ("numeric identity", [values[0], stable, "false"]),
                                 ("noncanonical identity", [values[0], lua_literal("0" + stable), "false"]),
                                 ("nonboolean state", [*values[:2], "0"]),
                                 ("extra argument", [*values, "true"]),
                                 ("metatable argument", [*values[:2], "setmetatable({},{})"]),
                                 ("metatable target", ["setmetatable({},{})", *values[1:]])):
            self.helper_rejection("per-window " + label + " rejects atomically", "set_hidden", ",".join(arguments))
        for name in ("active_privacy", "toggle", "reset_sharing"):
            self.helper_rejection(name + " rejects extra arguments atomically", name, "true")
        self.focus_window(public)
        self.helper_rejection("focus drift rejects pinned old-window sharing before mutation", "set_hidden", ",".join(values))
        self.focus_window(parent)

        # First share has no prior manual override: reset must unset its own
        # false slot so subsequent rule changes are again authoritative.
        shown = self.helper("set_hidden", address, stable, False)
        self.assert_check("set_hidden false temporarily shares an auto-protected focused fixture", shown["state"] == "visible" and not shown["native_private"])
        self.shared_frame("shared-auto-private", before)
        self.ctl("reload")
        self.assert_check("temporary native sharing survives configuration reload", self.helper("active_privacy")["state"] == "visible" and not self.native_private(parent))
        self.shared_frame("shared-after-reload", before)
        reset = self.helper("reset_sharing")
        self.assert_check("reset sharing restores automatic privacy", reset["state"] == "hidden" and reset["native_private"])
        self.private_frame("reset-auto-private", before)
        self.tag_window(parent, False)
        self.assert_check("reset removes its false override and permits later privacy rule changes", not self.native_private(parent))
        self.shared_frame("auto-rule-removed-after-reset", before)

        # Explicit hide is durable. A later temporary share/reset restores that
        # exact true manual override rather than losing the user's choice.
        hidden = self.helper("set_hidden", address, stable, True)
        self.assert_check("native explicit hide protects a public fixture", hidden["state"] == "hidden" and hidden["native_private"])
        self.ctl("reload")
        self.assert_check("native manual hide survives configuration reload", self.helper("active_privacy")["state"] == "hidden" and self.native_private(parent))
        self.private_frame("manual-hide-after-reload", before)
        self.helper("set_hidden", address, stable, False)
        self.shared_frame("shared-manual-private", before)
        reset = self.helper("reset_sharing")
        self.assert_check("reset sharing restores a prior manual true override", reset["state"] == "hidden" and reset["native_private"])
        self.assert_check("native focused toggle temporarily shares a hidden fixture", self.helper("toggle")["state"] == "visible")
        self.shared_frame("toggle-shared", before)
        self.assert_check("native focused toggle hides the same fixture again", self.helper("toggle")["state"] == "hidden")
        self.tag_window(parent, True)

        self.helper("set_hidden", address, stable, False)
        self.focus_window(public)
        reset = self.helper("reset_sharing")
        self.assert_check("global reset restores private targets while focused public window remains public",
                          reset["state"] == "visible" and not reset["native_private"] and self.native_private(parent))
        public_address, public_id = self.window_identity(public)
        self.helper("set_hidden", public_address, public_id, False)
        self.helper("reset_sharing")
        self.tag_window(public, True)
        self.assert_check("sharing an already public window creates no override against a future private rule",
                          self.helper("active_privacy")["state"] == "hidden" and self.native_private(public))
        self.tag_window(public, False)
        self.assert_check("public fixture is public again after removing its test privacy rule", self.helper("active_privacy")["state"] == "visible")

        self.focus_window(parent)
        self.helper("set_hidden", address, stable, False)
        self.dispatch('hl.dsp.window.set_prop({window=' + lua_literal("address:" + address) +
                      ',prop="no_screen_share",value="false"})')
        reset = self.helper("reset_sharing")
        self.assert_check("later external false setter revokes ownership and survives our reset",
                          reset["state"] == "visible" and not self.native_private(parent))
        self.shared_frame("external-false-survives-reset", before)
        self.helper("set_hidden", address, stable, True)

        self.dispatch('hl.dsp.focus({workspace="99"})')
        none = {"state": "none", "address": "", "stable_id": "", "native_private": False, "inherited": False}
        self.assert_check("native helpers handle no focused window without choosing another target",
                          self.helper("active_privacy") == self.helper("toggle") == self.helper("reset_sharing") == none and
                          self.native_private(parent) and not self.native_private(public))
        self.dispatch('hl.dsp.focus({workspace="1"})')
        self.focus_window(parent)

        self.popup_control("dialog-show", lambda value: value.get("ready") and value["dialog"]["mapped"])
        dialog = self.wait_until(lambda: next((item for item in self.clients() if item.get("title") == "Hyprveil fixture: transient dialog"), None), "inherited helper fixture")
        self.focus_window(dialog)
        snapshot = self.helper("active_privacy")
        self.assert_check("native helper query identifies inherited privacy without setting child flags", snapshot["state"] == "hidden" and snapshot["inherited"] and not snapshot["native_private"])
        dialog_args = ",".join(lua_literal(value) for value in (*self.window_identity(dialog), False))
        self.helper_rejection("inherited-only privacy cannot be partially shared", "set_hidden", dialog_args)
        self.helper("set_hidden", *self.window_identity(dialog), True)
        self.helper_rejection("own true child flag cannot bypass a private ancestor for sharing", "set_hidden", dialog_args)
        self.popup_control("dialog-hide", lambda value: value.get("ready") and not value["dialog"]["mapped"])
        self.wait_until(lambda: not any(item["address"] == dialog["address"] for item in self.clients()), "inherited helper fixture unmapping")

        # Destroy a temporarily shared window, then create another fixture. A
        # stale weak entry and stale identity must never mutate its successor.
        self.fixture("foreground")
        foreground = next(item for item in self.clients() if item["class"] == "org.hyprveil.fixture.foreground")
        self.focus_window(foreground)
        self.helper("set_hidden", *self.window_identity(foreground), True)
        self.helper("set_hidden", *self.window_identity(foreground), False)
        closing = self.fixtures.pop()
        closing.terminate()
        closing.wait(timeout=3)
        self.wait_until(lambda: not any(item["address"] == foreground["address"] for item in self.clients()), "shared fixture closing")
        self.fixture("foreground")
        replacement = next(item for item in self.clients() if item["class"] == "org.hyprveil.fixture.foreground")
        self.focus_window(replacement)
        self.helper_rejection("closed shared identity cannot target a replacement window", "set_hidden",
                              ",".join(lua_literal(value) for value in (*self.window_identity(foreground), False)))
        result = self.helper("reset_sharing")
        self.assert_check("reset after closed weak entry leaves replacement public and protected parent private",
                          result["state"] == "visible" and not self.native_private(replacement) and self.native_private(parent) and
                          self.window_identity(foreground)[1] != self.window_identity(replacement)[1])
        closing = self.fixtures.pop()
        closing.terminate()
        closing.wait(timeout=3)
        self.wait_until(lambda: not any(item["address"] == replacement["address"] for item in self.clients()), "replacement fixture closing")
        self.focus_window(parent)
        self.private_frame("helpers-restored-private-scene", before)
        # Leave an owned temporary share active deliberately. Unload below must
        # revoke it and restore the parent's prior native/manual privacy.
        self.helper("set_hidden", address, stable, False)
        self.shared_frame("share-pending-unload", before)

    def tests(self):
        self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        self.base_config = (self.runtime / "hyprland.lua").read_text()
        self.fixture("background")
        self.popup_fixture()
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        admitted = self.ctl("plugin", "load", str(self.plugin))
        if admitted != "ok":
            raise RuntimeError("native-config candidate loading refused: " + admitted)
        self.loaded = True
        self.ctl("dismissnotify", "-1")
        self.report["compositor"] = json.loads(self.ctl("-j", "version"))
        plugins = json.loads(self.ctl("-j", "plugin", "list"))
        self.assert_check("loaded module is exactly native-config release 0.5.0",
                          any(item["name"] == "hyprveil" and item["version"] == "0.5.0" for item in plugins))
        before = self.geometry()
        self.settings_agree("initial safe native defaults", DEFAULTS)
        self.private_frame("initial-black", before, black=True)

        configured = dict(DEFAULTS, mode="spoiler", variant="signal", color="#aabbcc", grain=17,
                          speed=0, darkness=31, eye=False, eye_size=128)
        self.patch(configured)
        self.settings_agree("atomic Lua full table", configured)
        self.private_frame("lua-telegram", before)
        self.patch({"color": "#ABCDEF", "grain": 100})
        configured.update(color="#abcdef", grain=100)
        self.settings_agree("atomic Lua partial table canonicalizes color and preserves remaining fields", configured)
        self.icon_tests(before, configured)
        self.variant_tests(before, configured)

        self.ctl("hyprveil", "black")
        configured["mode"] = "black"
        self.settings_agree("legacy command synchronizes native config", configured)
        self.plugin_dispatch("mode", "spoiler")
        configured["mode"] = "spoiler"
        self.settings_agree("typed mode dispatcher synchronizes native config", configured)
        for dispatcher, arguments in (("mode", ("blur",)), ("cycle", ("extra",)),
                                      ("mode", (True,)), ("mode", ()), ("mode", ("black", "extra"))):
            status = self.native()
            try:
                self.plugin_dispatch(dispatcher, *arguments)
            except RuntimeError:
                rejected = True
            else:
                rejected = False
            after = self.native()
            self.assert_check("native " + dispatcher + " bridge rejects invalid arguments without state changes",
                              rejected and flatten_status(after) == flatten_status(status) and
                              after["policy_generation"] == status["policy_generation"] and self.privacy())
        for mode in ("omit", "black", "spoiler", "omit", "black"):
            self.plugin_dispatch("cycle")
            configured["mode"] = mode
            self.settings_agree("cycle selects " + mode, configured)
            self.private_frame("cycle-" + str(len(self.checks)) + "-" + mode, before, black=mode == "black")

        color = (0.12, 0.48, 0.2)
        replacement = self.runtime / "native-replacement.png"
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 64, 64)
        context = cairo.Context(surface)
        context.set_source_rgb(*color)
        context.paint()
        surface.write_to_png(str(replacement))
        replacement.chmod(0o600)
        self.patch({"image_path": str(replacement)})
        configured["image_path"] = str(replacement)
        self.settings_agree("image path preconfiguration preserves black mode", configured)
        self.plugin_dispatch("mode", "image")
        configured["mode"] = "image"
        self.settings_agree("image dispatcher uses configured path", configured)
        image = self.private_frame("configured-image", before)
        self.assert_check("configured image actually replaces protected content",
                          color_fraction(image, self.roi(), (31, 122, 51), 2) >= 0.99)
        self.plugin_dispatch("cycle")
        configured["mode"] = "black"
        self.settings_agree("cycle from image returns secure black", configured)

        invalid = ["nil", "true", '"table"', "{unknown=true}", "{[1]=true}",
                   "setmetatable({grain=20},{})", "{grain=true}", '{grain="20"}',
                   "{grain=1.5}", "{grain=0/0}", "{speed=math.huge}", "{grain=-1}",
                   "{grain=101}", "{speed=201}", "{darkness=101}", "{eye=1}",
                   "{eye_size=39}", "{eye_size=129}", '{variant="blur"}', '{mode="off"}',
                   '{color="#fff"}', '{icon="unknown"}', '{icon=true}', '{icon_opacity=-1}',
                   '{icon_opacity=101}', '{icon_opacity=1.5}', '{icon_opacity="50"}', '{mode="image",image_path=""}',
                   lua_literal({"mode": "image", "image_path": str(self.runtime / "missing.png")}),
                   lua_literal({"image_path": "relative.png"}), lua_literal({"image_path": "bad\x00name"}),
                   lua_literal({"image_path": "/tmp/bad\nname"}), lua_literal({"image_path": "/" + "x" * 4096}),
                   '{mode="spoiler",grain=20,eye="false"}', '{} , {}']
        link = self.runtime / "image-link.png"
        link.symlink_to(replacement)
        invalid.append(lua_literal({"mode": "image", "image_path": str(link)}))
        for index, argument in enumerate(invalid):
            self.rejection(index, argument)
        self.private_frame("after-all-invalid-patches", before, black=True)

        selected = dict(DEFAULTS, mode="spoiler", variant="signal", grain=37, speed=0,
                        color="#4488aa", darkness=12, eye=True, eye_size=40)
        self.write_config(selected)
        self.assert_check("typed native file reload has no compositor config errors", not self.ctl("configerrors"))
        self.settings_agree("native file reload selects all eleven fields", selected)
        self.private_frame("native-file-telegram", before)
        local = self.local_image("native-file-local")
        self.assert_check("native settings leave original local window fully visible",
                          color_fraction(local, self.roi(), PRIVATE, 3) >= 0.99)
        for label, invalid_field in (("grain-range", {"grain": 101}), ("grain-boolean", {"grain": True}),
                                     ("eye-integer", {"eye": 1}), ("icon-name", {"icon": "bad"}),
                                     ("icon-opacity-range", {"icon_opacity": 101})):
            # The file explicitly requests spoiler. Validation failure must
            # override that request in the rendered AND registered mode.
            self.write_config(dict(selected, **invalid_field))
            self.assert_check("invalid native file " + label + " reports compositor config error", bool(self.ctl("configerrors")))
            failed = dict(flatten_status(self.native()), mode="black")
            self.settings_agree("invalid native file " + label + " forces registered and rendered black", failed)
            self.private_frame("invalid-native-file-" + label, before, black=True)
        self.write_config(selected)
        self.assert_check("corrected native file reload clears config errors", not self.ctl("configerrors"))
        self.settings_agree("corrected typed file reload restores selected settings", selected)
        self.per_window_tests(before)

        # Preserve actual plugin-owned callback objects before unloading. Their
        # registered-ID trampoline must reject calls after DSO removal.
        self.lua(";".join("_G.hyprveil_stale_" + name + "=hl.plugin.hyprveil." + name for name in CALLBACKS) + '; return "saved"')
        (self.runtime / "hyprland.lua").write_text(self.base_config)
        self.unload()
        for name in CALLBACKS:
            argument = "{}" if name == "configure" else '"black"' if name == "mode" else ",".join(lua_literal(value) for value in (*self.window_identity(self.protected()), False)) if name == "set_hidden" else ""
            output = self.lua("local ok=pcall(_G.hyprveil_stale_" + name + ("," + argument if argument else "") + "); return tostring(ok)")
            self.assert_check("detached Lua " + name + " callback rejects after unload without crash", output == "false")
        self.ctl("reload")
        self.assert_check("plugin config and Lua callbacks are removed on unload/reload",
                          not self.ctl("configerrors") and all(self.lua('return type(hl.plugin.hyprveil and hl.plugin.hyprveil.' + name + ')') == "nil"
                          for name in CALLBACKS))
        image = self.capture_image("unloaded-native-black")
        self.assert_check("unload leaves native privacy and opaque black capture protection intact",
                          self.privacy() and self.geometry() == before and absence_report(image, PRIVATE, 8)["ok"] and
                          color_fraction(image, self.roi(), (0, 0, 0), 0) == 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    run = NativeConfigSmoke(parser.parse_args())
    def interrupted(signum, frame):
        raise KeyboardInterrupt("native config smoke interrupted")
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        run.start()
        run.tests()
        run.report["ok"] = bool(run.checks) and all(item["ok"] for item in run.checks)
    except (Exception, KeyboardInterrupt) as error:
        run.report["error"] = str(error)
        print(json.dumps({"event": "error", "error": str(error)}), flush=True)
    finally:
        run.cleanup()
    print(json.dumps({"ok": run.report["ok"], "report": str(run.artifacts / "report.json") if run.artifacts else None,
                      "lab_stopped": run.report["lab_stopped"]}), flush=True)
    return 0 if run.report["ok"] and run.report["lab_stopped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
