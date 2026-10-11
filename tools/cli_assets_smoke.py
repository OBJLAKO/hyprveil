#!/usr/bin/env python3
"""Validate native asset commands and persistence in a guarded standard-admission lab.

Only target selection uses the existing marked-lab adapter; native IPC, file
persistence and capture rendering are real. No Hyprpm or host changes are made."""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import cairo
import service
from hyprpm_smoke import HyprpmSmoke
from lab import proc_env


class NativeCli15(HyprpmSmoke):
    def __init__(self, args):
        super().__init__(args)
        args.suite = "standalone-cli-native15"
        self.report.update(
            scope="actual standalone CLI subprocess using only the existing marked-lab target adapter; real native IPC and persistent Lua; no hyprpm/cache or host changes",
            runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            source_sha256={name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                           for name in ("tools/cli.py", "tools/service.py", "tools/hyprpm_smoke.py", "src/main.cpp")})

    def monitor(self):
        return next(m for m in json.loads(self.ctl("-j", "monitors"))
                    if m["name"].startswith("WAYLAND-") and m["width"] > 0)

    def capture(self, path):
        self.command(["grim", "-o", self.monitor()["name"], str(path)])

    def expect(self, name, result, expected):
        self.check(name, result.get("persisted") is True and service.native_values(result) == expected,
                   expected=expected, actual=service.native_values(result), persisted=result.get("persisted"))

    def tests(self):
        native_env = proc_env(self.state["pid"])
        self.check("standard admission omits plugin lab env while preserving owned seat-disabled compositor",
                   self.state["standard_plugin_admission"] and b"HYPRVEIL_LAB_RUNTIME" not in native_env and
                   native_env.get(b"LIBSEAT_BACKEND") == b"hyprveil-disabled" and
                   native_env.get(b"XDG_RUNTIME_DIR", b"").decode() == str(self.runtime))
        output = self.monitor()["name"]
        directory = self.runtime / "home/.config/hypr"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        settings = directory / "hyprveil-settings.lua"
        suffix = b"\n-- Unrelated CLI regression suffix must survive.\nlocal native_cli15_unrelated = 37\n"
        original = service.lua_settings_source(dict(desired_mode="black", image_path="",
            appearance=dict(service.DEFAULT_APPEARANCE), **service.DEFAULT_RENDER)) + suffix
        settings.write_bytes(original)
        settings.chmod(0o600)
        counter = self.runtime / "cli15-global-config-passes.txt"
        config = self.runtime / "hyprland.lua"
        config.write_text(config.read_text() + "\nhl.monitor({output=" + json.dumps(output) +
                          ',mode="1024x768@60",position="0x0",scale=1})\n' +
                          "dofile(" + service.lua_string(str(settings)) + ")\n" +
                          "do local f=assert(io.open(" + service.lua_string(str(counter)) +
                          ',"a")); f:write("parse\\n"); f:close() end\n')
        self.ctl("reload")
        time.sleep(.4)
        self.fixture("background")
        self.fixture("protected")
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        self.load()
        self.ctl("dismissnotify", "-1")
        time.sleep(.8)
        before_passes = counter.read_text()
        self.report["compositor"] = json.loads(self.ctl("-j", "version"))
        self.report["cli_argv"] = []
        expected = dict(mode="black", image_path="", **service.DEFAULT_APPEARANCE, **service.DEFAULT_RENDER)

        def call(*argv):
            self.report["cli_argv"].append(list(argv))
            return self.cli_command(*argv)

        expected["mask_color"] = "#a1b2c3"
        self.expect("CLI partial mask_color persists and preserves every other native field",
                    call("configure", "--mask-color", "#A1B2C3"), expected)
        expected.update(mode="color", mask_color="#193b5d")
        self.expect("CLI color HEX changes mode and canonical fill atomically",
                    call("color", "#193B5D"), expected)

        image = self.runtime / "cli15-image.png"
        icon = self.runtime / "cli15-icon.png"
        for path, rgb in ((image, (0.1, .7, .3)), (icon, (.9, .8, .2))):
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 32, 16)
            context = cairo.Context(surface)
            context.set_source_rgb(*rgb)
            context.paint()
            surface.write_to_png(str(path))
        shader = self.runtime / "cli15-custom.frag"
        shader.write_text("vec4 hyprveil(vec2 uv) { return vec4(0.15,0.65,0.85,1.0); }\n")
        expected.update(mode="image", image_path=str(image), image_fit="fit")
        self.expect("CLI image PATH --fit fit persists its path and fit without resetting other fields",
                    call("image", str(image), "--fit", "fit"), expected)
        self.private_frame("CLI image actual native capture remains private and opaque", "image")
        expected.update(mode="spoiler", shader_path=str(shader))
        self.expect("CLI spoiler --shader-path persists the trusted GLSL path",
                    call("spoiler", "--shader-path", str(shader)), expected)
        expected.update(icon="custom", icon_path=str(icon), icon_opacity=63, eye=True)
        self.expect("CLI custom PNG icon selection and visibility persist atomically",
                    call("configure", "--icon", "custom", "--icon-path", str(icon),
                         "--icon-opacity", "63", "--eye", "on"), expected)
        self.private_frame("CLI custom shader and PNG icon actual native capture remains private and opaque", "spoiler")
        status = call("status")
        self.check("CLI status reports all fifteen native fields with strict legacy appearance envelope intact",
                   status.get("loaded") is True and status.get("persistence_supported") is True and
                   status.get("desired_mode") == "spoiler" and service.native_values(status["status"]) == expected and
                   set(expected) == set(service.CONFIG_FIELDS) and len(expected) == 15 and
                   set(status["status"]["appearance"]) == set(service.DEFAULT_APPEARANCE) and
                   set(self.status()["appearance"]) == set(service.DEFAULT_APPEARANCE) - service.ICON_FIELDS, status=status)
        old_text, old_start, old_end, _ = service.parse_lua_settings(original)
        new_text, new_start, new_end, saved = service.parse_lua_settings(settings.read_bytes())
        self.check("managed literal file contains exactly all fifteen expected values and preserves unrelated Lua",
                   saved == expected and set(saved) == set(service.CONFIG_FIELDS) and
                   (old_text[:old_start], old_text[old_end:]) == (new_text[:new_start], new_text[new_end:]) and
                   settings.read_bytes().endswith(suffix), saved_settings=saved)
        time.sleep(.8)
        self.check("all new CLI partial and mode commands avoid global config reload and watcher reparsing",
                   counter.read_text() == before_passes, before=before_passes, after=counter.read_text())
        image.unlink()
        shader.unlink()
        icon.unlink()
        expected["mode"] = "black"
        self.expect("CLI black persists safely while retaining missing inactive asset paths",
                    call("black"), expected)
        self.private_frame("CLI black after missing retained assets actual capture remains private and opaque", "black")
        final_saved = service.parse_lua_settings(settings.read_bytes())[-1]
        time.sleep(.8)
        self.check("black fallback retains fifteen-field persistence without reload, receipts or config errors",
                   final_saved == expected and counter.read_text() == before_passes and self.receipts_absent() and
                   not self.ctl("configerrors") and self.status().get("session") == "live" and
                   json.loads(self.ctl("hyprveil", "dump-local")).get("error") == "dump-local is disabled in live sessions",
                   saved_settings=final_saved, config_passes=counter.read_text())
        self.report["final_native_status"] = self.status()
        self.report["managed_settings_sha256"] = hashlib.sha256(settings.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", required=True, type=Path)
    run = NativeCli15(parser.parse_args())
    def interrupted(signum, frame):
        raise KeyboardInterrupt("CLI native15 smoke interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        run.start()
        run.tests()
        run.report["ok"] = bool(run.checks) and all(item["ok"] for item in run.checks)
    except (Exception, KeyboardInterrupt) as error:
        run.report.update(ok=False, error=str(error))
        print(json.dumps({"event":"error", "error":str(error)}), flush=True)
    finally:
        run.cleanup()
        run.report["fixture_processes_stopped"] = all(p.poll() is not None for p in run.fixtures)
        run.report["owned_fixture_pids"] = [p.pid for p in run.fixtures]
        if not run.report["fixture_processes_stopped"]:
            run.report["ok"] = False
        run.save()
    print(json.dumps({"ok":run.report["ok"], "report":str(run.artifacts / "report.json") if run.artifacts else None,
                      "lab_stopped":run.report.get("lab_stopped")}), flush=True)
    return 0 if run.report["ok"] and run.report.get("lab_stopped") else 1

if __name__ == "__main__":
    raise SystemExit(main())
