#!/usr/bin/env python3
"""Test standard plugin admission and the standalone CLI in a guarded lab.

This tests the build artifact and public native API, without running hyprpm or
modifying its privileged cache. Only HYPRVEIL_LAB_RUNTIME is omitted from the
child compositor; the physical-seat guard and marked /tmp runtime remain.
Every control call and synthetic capture reattests that disposable compositor.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time
from unittest.mock import patch

import cli
import service
from lab import PROJECT, load_lab, proc_env
from stress import Stress

sys.path.insert(0, str(PROJECT / "tests"))
from verify_capture import Image, absence_report, color_fraction, opacity_fraction

PRIVATE = (232, 64, 144)
BACKGROUND = (36, 132, 196)


def marked_lab_cli(runtime: Path, arguments):
    """Adapt only CLI target selection to an already attested disposable lab.

    Production Controller deliberately refuses physical-seat-disabled labs.
    Keep that refusal intact: the adapter lives exclusively in this test runner,
    and still checks the marked process, instance list and IPC socket peer.
    """
    runtime, state = load_lab(runtime)
    environment = proc_env(state["pid"])
    if (not state.get("standard_plugin_admission") or b"HYPRVEIL_LAB_RUNTIME" in environment or
            os.environ.get("HYPRVEIL_LAB_RUNTIME") != str(runtime) or
            os.environ.get("XDG_RUNTIME_DIR") != str(runtime) or
            os.environ.get("HOME") != str(runtime / "home")):
        raise RuntimeError("CLI adapter requires the exact marked standard-admission lab environment")

    class MarkedLabController(cli.Controller):
        def select_target(self):
            verified, current = load_lab(runtime)
            if verified != self.runtime or self.signature != current["signature"]:
                raise service.Refused("test CLI target differs from the attested disposable lab")
            pid = current["pid"]
            selected = [item for item in self.query("-j", "instances")
                        if item.get("instance") == self.signature]
            if len(selected) != 1 or selected[0].get("pid") != pid:
                raise service.Refused("test CLI instance does not match the marked process")
            endpoint = runtime / "hypr" / self.signature / ".socket.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(1)
                connection.connect(str(endpoint))
                peer_pid, peer_uid, _ = struct.unpack("3i", connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if (peer_pid, peer_uid) != (pid, os.getuid()):
                raise service.Refused("test CLI socket peer differs from the marked compositor")
            started = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
            return service.Target(pid, self.signature, started)

    with patch.object(service, "standard_runtime", return_value=runtime), patch.object(cli, "Controller", MarkedLabController):
        return cli.main(["--signature", state["signature"], *arguments])


class HyprpmSmoke(Stress):
    def __init__(self, args):
        args.suite = "hyprpm-standard-admission"
        args.standard_plugin_admission = True
        super().__init__(args)
        self.report.update(
            scope="standard native admission/build artifact and standalone CLI; no hyprpm manager/cache operations",
            admission="no live marker; plugin sees live; compositor remains physically seat-disabled in marked /tmp lab",
            local_diagnostics="refusal tested; never requested for pixel readback",
            cli_target_adapter="test-only marked lab, native instance identity and SO_PEERCRED checks")

    def check(self, name, ok, **details):
        self.record(name, ok, **details)
        if not ok:
            raise RuntimeError("assertion failed: " + name)

    def status(self):
        return json.loads(self.ctl("hyprveil", "status"))

    def cli_command(self, *arguments, succeeds=True):
        load_lab(self.runtime)
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--lab-cli", str(self.runtime),
                                 "--", *arguments], env=self.env, capture_output=True, text=True, timeout=15)
        if (result.returncode == 0) != succeeds:
            raise RuntimeError(f"standalone CLI exit {result.returncode}: {result.stdout} {result.stderr}")
        return json.loads(result.stdout if succeeds else result.stderr)

    def load(self):
        result = self.ctl("plugin", "load", str(self.plugin))
        if result != "ok":
            raise RuntimeError("standard plugin admission failed: " + result)
        self.loaded = True
        # Native plugin load schedules an ordinary config reload.
        time.sleep(0.3)
        value = self.status()
        self.check("standard plugin load starts in native black without trial marker",
                   value.get("session") == "live" and value.get("mode") == "black" and
                   value.get("local_dump") == "disabled-in-live" and
                   not list(self.runtime.glob(".hyprveil-live-*")), status=value)
        self.sample_resources("standard-plugin-loaded")

    def private_frame(self, name, mode):
        before = self.geometry()
        self.dispatch('hl.dsp.cursor.move({x=4,y=4})')
        time.sleep(0.15)
        path = self.artifacts / (name + ".png")
        self.capture(path)
        image = Image(path)
        absent = absence_report(image, PRIVATE, 8)
        opaque = opacity_fraction(image, (0, 0, image.width, image.height))
        roi = self.roi()
        black = color_fraction(image, roi, (0, 0, 0), 0)
        background = color_fraction(image, roi, BACKGROUND, 3)
        correct = black >= 0.99 if mode == "black" else background >= 0.99 if mode == "omit" else black < 0.99 and background < 0.99
        self.check(name, absent["ok"] and opaque == 1 and correct and self.privacy() and self.geometry() == before,
                   whole_frame_privacy=absent, opaque_fraction=opaque, mode=mode,
                   black_roi_fraction=black, background_roi_fraction=background,
                   capture_geometry_unchanged=self.geometry() == before)

    def receipts_absent(self):
        return (not (self.runtime / "home/.config/hyprveil/config.json").exists() and
                not (self.runtime / ".hyprveil-controller.json").exists() and
                not list(self.runtime.glob(".hyprveil-live-*")))

    def settings_second_pass(self):
        # Model the real ordering: a settings file is sourced before async
        # plugin loading, then Hyprland's plugin load schedules a second parse.
        # This is a disposable native regression, not a cold-login claim.
        load_lab(self.runtime)
        directory = self.runtime / "home/.config/hypr"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        settings_path = directory / "hyprveil-settings.lua"
        appearance = dict(service.DEFAULT_APPEARANCE, variant="prism", grain=17, speed=0)
        settings = dict(desired_mode="spoiler", image_path="", appearance=appearance)
        suffix = b"\n-- Preserve this unrelated user Lua suffix.\nlocal hyprveil_lab_unrelated = 17\n"
        original = service.lua_settings_source(settings) + suffix
        settings_path.write_bytes(original)
        settings_path.chmod(0o600)
        config = self.runtime / "hyprland.lua"
        passes = self.runtime / "settings-config-passes.txt"
        # Count real configuration evaluations, including automatic watcher
        # reloads. Observing only controller argv would miss those reloads.
        counter = ('\ndo local f=assert(io.open(' + service.lua_string(str(passes)) + ',"a")); '
                   'f:write("parse\\n"); f:close() end\n')
        config.write_bytes(config.read_bytes() + ("\ndofile(" + service.lua_string(str(settings_path)) + ")\n" + counter).encode())
        self.unload()
        self.ctl("reload")
        errors = self.ctl("configerrors")
        absent = self.cli_command("status")
        self.check("managed settings safely skip the first config pass before plugin loading",
                   not errors and absent.get("loaded") is False and absent.get("persistence_supported") is True and
                   settings_path.read_bytes() == original and self.receipts_absent(),
                   config_errors=errors, status=absent)
        self.private_frame("settings-first-pass-native-fallback", "black")

        result = self.ctl("plugin", "load", str(self.plugin))
        if result != "ok":
            raise RuntimeError("standard plugin admission with pre-sourced settings failed: " + result)
        self.loaded = True
        deadline = time.monotonic() + 5
        observed = self.status()
        while time.monotonic() < deadline and (observed.get("mode") != "spoiler" or service.native_values(observed) != dict(mode="spoiler", image_path="", **appearance)):
            time.sleep(0.05)
            observed = self.status()
        self.check("native scheduled second config pass applies pre-sourced managed settings",
                   observed.get("session") == "live" and observed.get("mode") == "spoiler" and
                   service.native_values(observed) == dict(mode="spoiler", image_path="", **appearance) and not self.ctl("configerrors") and
                   settings_path.read_bytes() == original and self.receipts_absent(),
                   status=observed, manual_reload_after_load=False, true_cold_login_tested=False)
        self.private_frame("settings-second-pass-spoiler", "spoiler")

        time.sleep(0.6)
        before_passes = passes.read_text()

        persisted = self.cli_command("configure", "--grain", "23")
        time.sleep(0.6)
        saved = settings_path.read_bytes()
        old_text, old_start, old_finish, _ = service.parse_lua_settings(original)
        new_text, new_start, new_finish, values = service.parse_lua_settings(saved)
        wanted = dict(appearance, grain=23)
        self.check("standalone partial update persists native settings and preserves unrelated Lua",
                   persisted.get("persisted") is True and persisted.get("mode") == "spoiler" and
                   persisted.get("appearance") == wanted and values == dict(mode="spoiler", image_path="", **wanted) and
                   (old_text[:old_start], old_text[old_finish:]) == (new_text[:new_start], new_text[new_finish:]) and
                   saved.endswith(suffix) and not self.ctl("configerrors") and self.receipts_absent(),
                   status=persisted, saved_settings=values, unrelated_suffix_preserved=saved.endswith(suffix))
        self.check("persistent partial appearance update does not reparse Hyprland or trigger its watcher",
                   passes.read_text() == before_passes)
        self.private_frame("settings-cli-persisted-spoiler", "spoiler")
        icons = self.cli_command("configure", "--variant", "telegram", "--color", "#ABCDEF",
                                 "--icon", "shield", "--icon-opacity", "37")
        time.sleep(0.6)
        wanted = dict(wanted, variant="signal", color="#abcdef", icon="shield", icon_opacity=37)
        _, _, _, icon_values = service.parse_lua_settings(settings_path.read_bytes())
        self.check("standalone alias, uppercase tint and icon update persist canonical native values",
                   icons.get("persisted") is True and icons.get("appearance") == wanted and
                   icon_values == dict(mode="spoiler", image_path="", **wanted) and
                   settings_path.read_bytes().endswith(suffix) and not self.ctl("configerrors") and self.receipts_absent(),
                   status=icons, saved_settings=icon_values)
        self.check("persistent preset and icon changes do not reparse Hyprland",
                   passes.read_text() == before_passes)
        self.private_frame("settings-cli-configurable-icon", "spoiler")
        same_stat = settings_path.stat()
        same = self.cli_command("configure", "--icon", "shield", "--icon-opacity", "37")
        time.sleep(0.6)
        self.check("saving unchanged settings keeps the file inode and avoids compositor reload",
                   same.get("persisted") is True and settings_path.stat().st_ino == same_stat.st_ino and
                   settings_path.stat().st_mtime_ns == same_stat.st_mtime_ns and passes.read_text() == before_passes)
        mode = self.cli_command("black")
        time.sleep(0.6)
        self.check("persistent mode change updates native state without reparsing Hyprland",
                   mode.get("mode") == "black" and mode.get("persisted") is True and
                   service.parse_lua_settings(settings_path.read_bytes())[-1]["mode"] == "black" and
                   passes.read_text() == before_passes)
        self.private_frame("settings-cli-persisted-black", "black")
        self.cli_command("spoiler")
        restored = self.cli_command("reload-config")
        self.check("explicit Reload Lua still reparses configuration and preserves saved appearance",
                   passes.read_text() != before_passes and restored.get("mode") == "spoiler" and
                   restored.get("appearance") == wanted and not self.ctl("configerrors"))
        self.report["settings_startup_scope"] = "native availability guard before load and automatic second config pass; true cold login untested"

    def tests(self):
        actual = proc_env(self.state["pid"])
        self.check("standard admission preserves isolation while omitting only plugin lab env",
                   self.state.get("standard_plugin_admission") is True and
                   actual.get(b"LIBSEAT_BACKEND") == b"hyprveil-disabled" and
                   actual.get(b"XDG_RUNTIME_DIR", b"").decode() == str(self.runtime) and
                   b"HYPRVEIL_LAB_RUNTIME" not in actual and
                   self.env.get("HYPRVEIL_LAB_RUNTIME") == str(self.runtime))
        if not any(item["name"] == "HV-TEST" for item in json.loads(self.ctl("-j", "monitors"))):
            self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        self.fixture("background")
        self.fixture("protected")
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        self.check("synthetic private fixture uses the native rule", self.privacy())
        self.report["compositor"] = json.loads(self.ctl("-j", "version"))
        unloaded = self.cli_command("status")
        self.check("standalone CLI reports unloaded without installation receipts",
                   unloaded.get("loaded") is False and self.receipts_absent(), status=unloaded)
        refused = self.cli_command("spoiler", "--runtime", succeeds=False)
        self.check("unloaded CLI mutation explains hyprpm activation without loading",
                   "hyprpm enable hyprveil" in refused.get("error", "") and self.receipts_absent(), result=refused)
        self.private_frame("native-fallback-before-load", "black")
        self.load()
        for diagnostic in ("dump-local", "fixture-privacy"):
            value = json.loads(self.ctl("hyprveil", diagnostic))
            self.check("live admission refuses " + diagnostic, bool(value.get("error")), result=value)
        self.check("diagnostic refusal produces no local mirror PNG", not (self.runtime / "hyprveil-local.png").exists())
        for mode in ("black", "omit", "spoiler"):
            selected = self.cli_command(mode, "--runtime")
            self.check("standalone CLI selects " + mode + " without receipts or persistence",
                       selected.get("mode") == mode and selected.get("persisted") is False and self.receipts_absent(), status=selected)
            self.private_frame("standard-" + mode, mode)
        selected = self.cli_command("configure", "--runtime", "--grain", "0", "--speed", "0", "--eye", "off")
        self.check("standalone partial configuration keeps mode and native settings",
                   selected["mode"] == "spoiler" and selected["appearance"]["grain"] == 0 and
                   selected["appearance"]["speed"] == 0 and selected["appearance"]["eye"] is False and
                   selected.get("persisted") is False and self.receipts_absent(), status=selected)
        self.private_frame("standard-configured-spoiler", "spoiler")
        self.dispatch('hl.dsp.focus({window=' + json.dumps(self.address()) + '})')
        shared = self.cli_command("show")
        self.check("standalone explicit show changes only the focused native privacy", shared.get("state") == "visible" and not self.privacy())
        revoked = self.cli_command("reset-sharing")
        self.check("standalone reset-sharing restores native privacy", revoked.get("state") == "hidden" and self.privacy())
        self.private_frame("standard-reset-sharing", "spoiler")
        refreshed = self.cli_command("reload-config")
        self.check("standalone native reload resets to configured default without a journal",
                   refreshed.get("mode") == "black" and self.receipts_absent(), status=refreshed)
        self.unload()
        self.private_frame("native-fallback-after-unload", "black")
        self.load()
        self.private_frame("standard-reloaded-default", "black")
        self.check("standard API commands never create legacy admission markers or receipts", self.receipts_absent())
        self.settings_second_pass()
        self.report["plugin_status"] = self.status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime")
    parser.add_argument("--parent-display")
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    parser.add_argument("--lab-cli", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("cli_arguments", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.lab_cli is not None:
        arguments = args.cli_arguments[1:] if args.cli_arguments[:1] == ["--"] else args.cli_arguments
        return marked_lab_cli(args.lab_cli, arguments)
    if not args.parent_runtime or not args.parent_display or args.cli_arguments:
        parser.error("provide --parent-runtime and --parent-display for the owned nested test")
    run = HyprpmSmoke(args)
    def interrupted(signum, frame):
        raise KeyboardInterrupt("standard admission suite interrupted")
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
