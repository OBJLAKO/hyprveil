#!/usr/bin/env python3
"""Check native zoom, scene stacking and rounding in an owned disposable lab.

Only explicitly selected parent Wayland hosting is shared. Fixtures, configuration,
IPC and captures belong to the marked, physical-seat-disabled child compositor.
"""
import argparse
import array
import json
import hashlib
from pathlib import Path
import shutil
import signal
import time

import cairo

from lab import PROJECT
from stress import Stress

import sys
sys.path.insert(0, str(PROJECT / "tests"))
from verify_capture import Image, absence_report, color_fraction, opacity_fraction

PRIVATE = (232, 64, 144)
BACKGROUND = (36, 132, 196)
PUBLIC = (242, 207, 82)


def bounds(path, color):
    surface = cairo.ImageSurface.create_from_png(str(path))
    pixels = array.array("I", bytes(surface.get_data()))
    stride = surface.get_stride() // 4
    target = (color[0] << 16) | (color[1] << 8) | color[2]
    matches = [(i % stride, i // stride) for i, value in enumerate(pixels)
               if value & 0xFFFFFF == target and i % stride < surface.get_width()]
    if not matches:
        return None
    xs, ys = zip(*matches)
    return [min(xs), min(ys), max(xs)-min(xs)+1, max(ys)-min(ys)+1]


class GeometrySmoke(Stress):
    def __init__(self, args):
        args.suite = "render-geometry"
        super().__init__(args)
        self.report["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def monitor(self):
        monitors = json.loads(self.ctl("-j", "monitors"))
        return next(m for m in monitors if m["name"].startswith("WAYLAND-") and m["width"] > 0)

    def capture(self, path):
        self.command(["grim", "-o", self.monitor()["name"], str(path)])

    def zoom_region(self, name, local, region=(300, 200, 500, 400)):
        x, y, w, h = region
        path = self.artifacts / (name + "-region.png")
        self.command(["grim", "-g", f"{x},{y} {w}x{h}", str(path)])
        source = bounds(local, PUBLIC)
        expected = None
        if source:
            sx, sy, sw, sh = source
            left, top, right, bottom = max(sx,x), max(sy,y), min(sx+sw,x+w), min(sy+sh,y+h)
            if right > left and bottom > top:
                expected = [left-x, top-y, right-left, bottom-top]
        actual = bounds(path, PUBLIC)
        image = Image(path)
        self.record(name + ": region crops the final native camera image", actual is not None and expected is not None and
                    all(abs(a-b) <= 2 for a,b in zip(actual, expected)) and absence_report(image, PRIVATE, 8)["ok"] and
                    opacity_fraction(image, (0,0,image.width,image.height)) == 1,
                    region=region, exported_public_bounds=actual, local_crop_public_bounds=expected)

    def pair(self, name):
        before = self.geometry()
        self.ctl("hyprveil", "dump-local")
        path = self.artifacts / (name + ".png")
        self.capture(path)
        local = self.artifacts / (name + "-local.png")
        shutil.copy2(self.runtime / "hyprveil-local.png", local)
        image = Image(path)
        whole = (0, 0, image.width, image.height)
        self.record(name + ": private pixels absent, export opaque, geometry retained",
                    absence_report(image, PRIVATE, 8)["ok"] and opacity_fraction(image, whole) == 1 and
                    self.geometry() == before and self.privacy(), geometry=before,
                    capture_size=[image.width, image.height])
        return path, local

    def tests(self):
        # This is the child compositor's own nested output, not the parent.
        # Aquamarine's headless output currently reports 0x0 on this machine.
        output = self.monitor()["name"]
        self.fixture("background")
        self.fixture("protected")
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        self.load()
        self.ctl("hyprveil", "black")
        self.report["compositor"] = json.loads(self.ctl("-j", "version"))
        self.report["scope"] = "native guarded lab; no third-party plugin installed or loaded"

        config = self.runtime / "hyprland.lua"
        base = config.read_text()
        config.write_text(base + '\nhl.monitor({output=' + json.dumps(output) + ',mode="1024x768@60",position="0x0",scale=1})\n'
                          'hl.config({decoration={rounding=32,rounding_power=2}})\n')
        self.ctl("reload")
        time.sleep(.4)
        self.report["monitors_before_capture"] = json.loads(self.ctl("-j", "monitors"))
        self.report["config_errors"] = self.ctl("configerrors")
        self.save()
        if self.report["config_errors"]:
            raise RuntimeError(self.report["config_errors"])
        private = self.protected()
        x, y = private["at"]
        corner = (x+1, y+1, 6, 6)
        center = (x+64, y+64, 32, 32)
        for mode in ("black", "spoiler"):
            self.ctl("hyprveil", mode)
            path, local = self.pair("rounded-" + mode)
            local_corner = color_fraction(Image(local), corner, BACKGROUND, 3)
            capture_corner = color_fraction(Image(path), corner, BACKGROUND, 3)
            self.record(mode + ": replacement inherits native rounded corner", local_corner > .99 and capture_corner > .99,
                        local_background_fraction=local_corner, export_background_fraction=capture_corner, corner=corner)
            self.record(mode + ": interior remains replaced", color_fraction(Image(path), center, PRIVATE, 8) == 0)

        self.fixture("foreground")
        self.ctl("eval", 'hl.config({cursor={zoom_factor=2,zoom_rigid=false,zoom_detached_camera=false}})')
        self.dispatch('hl.dsp.cursor.move({x=512,y=384})')
        time.sleep(.7)
        self.ctl("hyprveil", "black")
        path, local = self.pair("native-zoom-2")
        actual, expected = bounds(path, PUBLIC), bounds(local, PUBLIC)
        self.record("native zoom: public geometry matches local camera", actual is not None and expected is not None and
                    all(abs(a-b) <= 2 for a, b in zip(actual, expected)), exported_public_bounds=actual, local_public_bounds=expected,
                    native_zoom_option=self.ctl("getoption", "cursor:zoom_factor"))
        self.record("native zoom positive control enlarges public fixture", expected is not None and expected[2] >= 300 and expected[3] >= 220,
                    local_public_bounds=expected)
        self.zoom_region("native-zoom-2", local)
        for name, rigid, detached, point in (("rigid", True, False, (480, 360)), ("detached", False, True, (510, 380))):
            self.ctl("eval", 'hl.config({cursor={zoom_factor=1.5,zoom_rigid=' + str(rigid).lower() +
                     ',zoom_detached_camera=' + str(detached).lower() + '}})')
            self.dispatch('hl.dsp.cursor.move({x=' + str(point[0]) + ',y=' + str(point[1]) + '})')
            time.sleep(.7)
            path, local = self.pair("native-zoom-" + name)
            actual, expected = bounds(path, PUBLIC), bounds(local, PUBLIC)
            self.record(name + ": native zoom camera geometry matches local", actual is not None and expected is not None and
                        all(abs(a-b) <= 2 for a,b in zip(actual, expected)),
                        exported_public_bounds=actual, local_public_bounds=expected)
            self.zoom_region("native-zoom-" + name, local)

        self.ctl("eval", 'hl.config({cursor={zoom_factor=1}})')
        time.sleep(.5)
        foreground = next(c for c in self.clients() if c["class"] == "org.hyprveil.fixture.foreground")
        self.dispatch('hl.dsp.window.fullscreen({window=' + json.dumps("address:" + foreground["address"]) +
                      ',action="set",mode="fullscreen",layout_aware=false})')
        time.sleep(.4)
        path, local = self.pair("public-fullscreen-over-private")
        # Sample the private content's center, which the fullscreen public
        # window must cover. The former bottom strip extended beyond a resized
        # nested output; this region also detects the old bug more directly.
        roi = (x + 80, y + 60, 160, 120)
        expected = color_fraction(Image(local), roi, PUBLIC, 3)
        actual = color_fraction(Image(path), roi, PUBLIC, 3)
        self.record("public fullscreen occludes private replacement", expected > .99 and actual > .99,
                    local_public_fraction=expected, exported_public_fraction=actual)

        self.dispatch('hl.dsp.window.fullscreen({window=' + json.dumps("address:" + foreground["address"]) +
                      ',action="unset",mode="fullscreen",layout_aware=false})')
        # The native renderWindow early-out also applies to transparent views.
        # This isolates the visibility check from fullscreen policy heuristics.
        config.write_text(config.read_text() + '\nhl.window_rule({match={class="^org[.]hyprveil[.]fixture[.]protected$"},opacity="0 0"})\n')
        self.ctl("reload")
        time.sleep(.4)
        path, local = self.pair("zero-alpha-private")
        roi = (280, 216, 80, 64)
        self.record("fully transparent native window adds no replacement", color_fraction(Image(local), roi, BACKGROUND, 3) > .99 and
                    color_fraction(Image(path), roi, BACKGROUND, 3) > .99,
                    local_background_fraction=color_fraction(Image(local), roi, BACKGROUND, 3),
                    exported_background_fraction=color_fraction(Image(path), roi, BACKGROUND, 3))
        self.report["plugin_status"] = json.loads(self.ctl("hyprveil", "status"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    args = parser.parse_args()
    run = GeometrySmoke(args)
    def interrupted(signum, frame):
        raise KeyboardInterrupt("geometry smoke interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        run.start()
        run.tests()
        run.report["ok"] = bool(run.checks) and all(check["ok"] for check in run.checks)
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
    print(json.dumps({"ok":run.report["ok"],"report":str(run.artifacts / "report.json") if run.artifacts else None,
                      "lab_stopped":run.report["lab_stopped"]}), flush=True)
    return 0 if run.report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
