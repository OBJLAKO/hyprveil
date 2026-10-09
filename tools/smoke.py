#!/usr/bin/env python3
"""Exercise real capture rendering with synthetic windows in a guarded lab."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import cairo

from lab import PROJECT, load_lab, lab_env

sys.path.insert(0, str(PROJECT / "tests"))
from verify_capture import Image, opacity_fraction


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-dir", required=True)
    args = parser.parse_args()
    runtime, state = load_lab(args.lab_dir)
    env = lab_env(runtime, state)
    artifacts = Path(state["artifacts"])
    processes = []
    checks = []
    loaded = False
    plugin = str(artifacts / "hyprveil.so")
    # Keep the exact binary used for each test, including its debug symbols.
    shutil.copyfile(PROJECT / "build/hyprveil.so", plugin)
    Path(plugin).chmod(0o500)
    plugin_sha256 = hashlib.sha256(Path(plugin).read_bytes()).hexdigest()

    def command(argv):
        load_lab(runtime)
        completed = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=15)
        if completed.returncode:
            raise RuntimeError(f"{argv}: {completed.stdout} {completed.stderr}")
        return completed.stdout.strip()

    def ctl(*argv):
        result = command(["hyprctl", "-i", state["signature"], *argv])
        if result.startswith(("err", "Invalid", "unknown", "Couldn")):
            raise RuntimeError(f"hyprctl {argv}: {result}")
        return result

    def check(name, *argv):
        completed = subprocess.run([sys.executable, str(PROJECT / "tests/verify_capture.py"), *map(str, argv)],
                                   env=env, text=True, capture_output=True, timeout=15)
        result = json.loads(completed.stdout)
        checks.append({"name": name, **result})
        print(json.dumps(checks[-1]), flush=True)
        if completed.returncode or not result.get("ok"):
            raise RuntimeError("assertion failed: " + name)

    def capture(filename, geometry=None):
        path = artifacts / filename
        # Headless outputs can include a software pointer in their native
        # mirror. Keep it outside every privacy/assertion rectangle.
        ctl("eval", 'hl.dispatch(hl.dsp.cursor.move({x=16,y=16}))')
        time.sleep(0.15)
        argv = ["grim", "-g", geometry] if geometry else ["grim", "-o", "HV-TEST"]
        command([*argv, str(path)])
        return path

    def fixture(role):
        log = (artifacts / f"{role}.log").open("w")
        process = subprocess.Popen([sys.executable, str(PROJECT / "tests/fixtures/client.py"),
                                    "--lab-dir", str(runtime), "--wayland-display", state["wayland_display"],
                                    "--role", role, "--ready-file", str(runtime / f"{role}.json")],
                                   env=env, stdout=log, stderr=subprocess.STDOUT)
        processes.append((process, log))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"fixture {role} exited {process.returncode}: {(artifacts / f'{role}.log').read_text()}")
            clients = json.loads(ctl("-j", "clients"))
            targets = [item for item in clients if item.get("class") == "org.hyprveil.fixture." + role]
            if len(targets) == 1 and min(targets[0].get("size", [0, 0])) > 0:
                time.sleep(0.35)
                return targets[0]
            time.sleep(0.1)
        (artifacts / f"{role}.clients.json").write_text(json.dumps(clients, indent=2))
        raise RuntimeError(f"fixture {role} did not map")

    try:
        monitors = json.loads(ctl("-j", "monitors"))
        if not any(item["name"] == "HV-TEST" for item in monitors):
            ctl("output", "create", "headless", "HV-TEST")
        for item in monitors:
            if item["name"].startswith("WAYLAND-"):
                ctl("output", "remove", item["name"])
        errors = ctl("configerrors")
        if errors:
            raise RuntimeError("lab configuration errors: " + errors)
        fixture("background")
        underlay = capture("underlay.png")
        protected = fixture("protected")
        before = artifacts / "clients.before.json"
        before.write_text(ctl("-j", "clients") + "\n")
        x, y = protected["at"]
        w, h = protected["size"]
        rect = f"{x+16},{y+16},{w-32},{h-32}"
        preflag = capture("visible-before-protection.png")
        check("fixture visible before protection", "color", "--image", preflag, "--rect", rect, "--color", "e84090")
        address = "address:" + protected["address"]
        print("protecting synthetic fixture", flush=True)
        ctl("eval", 'hl.dispatch(hl.dsp.window.tag({window=' + json.dumps(address) + ',tag="+hyprveil-private-test"}))')
        if ctl("getprop", address, "no_screen_share") != "true":
            raise RuntimeError("native capture protection not applied")
        black = capture("native-black.png")
        check("native fallback is black", "color", "--image", black, "--rect", rect, "--color", "000000")
        print("loading plugin into isolated compositor", flush=True)
        response = ctl("plugin", "load", plugin)
        if response != "ok":
            raise RuntimeError("plugin load: " + response)
        loaded = True
        initial = json.loads(ctl("hyprveil", "status"))
        print(json.dumps(initial), flush=True)
        if initial.get("mode") != "black":
            raise RuntimeError("plugin did not start with safe native black defaults")
        if ctl("getprop", address, "no_screen_share") != "true":
            raise RuntimeError("native capture protection lost after plugin load/config reload")
        initial_black = capture("plugin-initial-black.png")
        check("plugin starts with opaque black protection", "color", "--image", initial_black,
              "--rect", rect, "--color", "000000")
        check("initial plugin defaults contain no private pixels anywhere", "absence", "--image", initial_black)
        initial_image = Image(initial_black)
        opaque = opacity_fraction(initial_image, (0, 0, initial_image.width, initial_image.height)) == 1
        checks.append({"name": "initial black export is fully opaque", "ok": opaque})
        if not opaque:
            raise RuntimeError("initial black privacy mask produced a transparent export")
        # Typed native configuration starts in black. Select omission before
        # comparing its exported scene with the public underlay.
        ctl("hyprveil", "omit")
        ctl("hyprveil", "dump-local")
        sanitized = capture("omitted.png")
        local = artifacts / "local.png"
        shutil.copyfile(runtime / "hyprveil-local.png", local)
        status = json.loads(ctl("hyprveil", "status"))
        (artifacts / "plugin-status.json").write_text(json.dumps(status, indent=2) + "\n")
        print(json.dumps(status), flush=True)
        if status["scene_frames"] < 1 or status["omitted_window_passes"] < 1:
            raise RuntimeError("capture hooks did not omit a window")
        check("window locally visible and absent from export", "scene", "--local", local,
              "--capture", sanitized, "--underlay", underlay, "--rect", rect)
        after = artifacts / "clients.after.json"
        after.write_text(ctl("-j", "clients") + "\n")
        check("protected window geometry preserved", "geometry", "--before", before, "--after", after)
        check("background geometry preserved", "geometry", "--before", before, "--after", after,
              "--app-id", "org.hyprveil.fixture.background")
        if ctl("getprop", address, "no_screen_share") != "true":
            raise RuntimeError("plugin changed native capture protection")
        mask = artifacts / "test-mask.png"
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        paint = cairo.Context(surface)
        paint.set_source_rgb(0.10, 0.24, 0.32)
        paint.paint()
        paint.set_source_rgb(0.18, 0.75, 0.65)
        paint.rectangle(0, 0, w / 2, h)
        paint.fill()
        paint.set_source_rgb(1, 1, 1)
        paint.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        paint.set_font_size(26)
        paint.move_to(48, h / 2)
        paint.show_text("PRIVATE")
        surface.write_to_png(str(mask))
        ctl("hyprveil", "image", str(mask))
        image_capture = capture("image-mask.png")
        check("custom image replaces protected contents", "compare", "--image", image_capture,
              "--reference", mask, "--rect", rect, "--reference-rect", f"16,16,{w-32},{h-32}")
        check("PNG mode contains no private pixels anywhere", "absence", "--image", image_capture)
        ctl("hyprveil", "omit")
        repeated = capture("omit-after-image.png")
        check("switch back to omission", "scene", "--local", local, "--capture", repeated,
              "--underlay", underlay, "--rect", rect)
        cropped = capture("region-omitted.png", "240,176 480x320")
        check("region export matches sanitized monitor", "compare", "--image", cropped,
              "--reference", repeated, "--rect", "0,0,480,320", "--reference-rect", "240,176,480,320")
        invalid_image = artifacts / "invalid-image.png"
        invalid_image.write_text("This synthetic file is deliberately not a PNG.\n")
        ctl("hyprveil", "image", str(invalid_image))
        invalid_capture = capture("invalid-image-black.png")
        check("invalid PNG retains a black privacy mask", "color", "--image", invalid_capture,
              "--rect", rect, "--color", "000000")
        check("invalid PNG contains no private pixels anywhere", "absence", "--image", invalid_capture)
        ctl("hyprveil", "omit")
        foreground = fixture("foreground")
        fg_before = artifacts / "clients.foreground.before.json"
        fg_before.write_text(ctl("-j", "clients") + "\n")
        fx, fy = foreground["at"]
        fw, fh = foreground["size"]
        fg_rect = f"{fx+8},{fy+8},{fw-16},{fh-16}"
        ctl("hyprveil", "image", str(mask))
        stacked_image = capture("image-with-foreground.png")
        check("image replacement keeps foreground stacking", "color", "--image", stacked_image,
              "--rect", fg_rect, "--color", "f2cf52")
        check("stacked PNG contains no private pixels anywhere", "absence", "--image", stacked_image)
        ctl("hyprveil", "omit")
        stacked_omit = capture("omit-with-foreground.png")
        check("omission keeps public foreground visible", "color", "--image", stacked_omit,
              "--rect", fg_rect, "--color", "f2cf52")
        check("private content omitted beneath overlapping foreground", "scene", "--local", local,
              "--capture", stacked_omit, "--underlay", underlay,
              "--rect", f"{x+16},{y+16},{w-32},{fy-y-32}")
        fg_after = artifacts / "clients.foreground.after.json"
        fg_after.write_text(ctl("-j", "clients") + "\n")
        check("foreground geometry preserved", "geometry", "--before", fg_before, "--after", fg_after,
              "--app-id", "org.hyprveil.fixture.foreground")
        ctl("hyprveil", "black")
        fallback = capture("plugin-black.png")
        check("disabled effect retains native black protection", "color", "--image", fallback,
              "--rect", f"{x+16},{y+16},{w-32},{fy-y-32}", "--color", "000000")
        check("disabled effect contains no private pixels anywhere", "absence", "--image", fallback)
        ctl("plugin", "unload", plugin)
        loaded = False
        fallback = capture("unloaded-black.png")
        check("plugin unload retains native protection", "color", "--image", fallback,
              "--rect", f"{x+16},{y+16},{w-32},{fy-y-32}", "--color", "000000")
        check("unloaded plugin contains no private pixels anywhere", "absence", "--image", fallback)
        report = {"ok": True, "lab": state, "rect": rect, "checks": checks, "plugin_status": status,
                  "compositor": json.loads(ctl("-j", "version")), "capture_tool": "grim",
                  "plugin_sha256": plugin_sha256}
        (artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"ok": True, "report": str(artifacts / "report.json")}), flush=True)
        return 0
    except Exception as error:
        (artifacts / "report.json").write_text(json.dumps({"ok": False, "error": str(error), "checks": checks,
                                                       "plugin_sha256": plugin_sha256}, indent=2) + "\n")
        print(f"smoke failed: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        if loaded:
            try:
                ctl("plugin", "unload", plugin)
            except Exception:
                pass
        for process, log in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
