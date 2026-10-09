#!/usr/bin/python3
"""Check new GPU materials on small and fractional windows in an owned lab."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import time

import cairo

from lab import PROJECT
from service import DEFAULT_APPEARANCE, validate_appearance
from stress import Stress

sys.path.insert(0, str(PROJECT / "tests"))
from verify_capture import Image, absence_report, color_fraction, differing_fraction, opacity_fraction

PRIVATE = (232, 64, 144)
VARIANTS = ("error404", "matrix", "anonymous", "glass")
SIZES = ((48, 32), (128, 96), (240, 160), (64, 128))


class StyleBoundsSmoke(Stress):
    def __init__(self, args):
        args.suite = "style-bounds"
        super().__init__(args)
        self.report.update(bounds=[], visual_scope="Actual GPU crops; readability requires visual review, not an OCR claim")

    def check(self, name, ok, **details):
        self.record(name, ok, **details)
        if not ok:
            raise RuntimeError("assertion failed: " + name)

    def configure(self, variant):
        wanted = dict(DEFAULT_APPEARANCE, variant=variant, speed=0, grain=0, darkness=25, eye=False, icon="none")
        status = json.loads(self.ctl("hyprveil", "appearance", wanted["variant"], wanted["color"],
                         str(wanted["grain"]), str(wanted["speed"]), str(wanted["darkness"]),
                         "0", str(wanted["eye_size"]), wanted["icon"], str(wanted["icon_opacity"])))
        actual = dict(status.get("appearance", {}), icon=status.get("icon"), icon_opacity=status.get("icon_opacity"))
        self.check(variant + " bounded native settings acknowledged", status.get("mode") == "spoiler" and
                   validate_appearance(actual, canonical=True) == wanted)

    def window_rect(self, inset=0):
        window, monitor = self.protected(), self.monitor()
        scale = monitor["scale"]
        return (round((window["at"][0] - monitor["x"]) * scale) + inset,
                round((window["at"][1] - monitor["y"]) * scale) + inset,
                max(1, round(window["size"][0] * scale) - 2 * inset),
                max(1, round(window["size"][1] * scale) - 2 * inset))

    def frame(self, name, geometry):
        path = self.artifacts / (name + ".png")
        self.capture(path)
        image = Image(path)
        absence = absence_report(image, PRIVATE, 8)
        self.check(name + " stays opaque, private and geometrically unchanged",
                   absence["ok"] and opacity_fraction(image, (0, 0, image.width, image.height)) == 1 and
                   self.geometry() == geometry and self.privacy(), forbidden_fraction=absence["forbidden_fraction"])
        return path, image

    def crop(self, source, rect, name):
        x, y, width, height = rect
        original = cairo.ImageSurface.create_from_png(str(source))
        result = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
        paint = cairo.Context(result)
        paint.set_source_surface(original, -x, -y)
        paint.paint()
        path = self.artifacts / (name + "-crop.png")
        result.write_to_png(str(path))
        return path

    def tests(self):
        self.check("candidate binary matches the explicit requested SHA", self.report["plugin_sha256"] == self.args.plugin_sha256)
        self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        self.fixture("background")
        self.fixture("protected")
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        self.dispatch('hl.dsp.cursor.move({x=8,y=8})')
        self.load()
        config = self.runtime / "hyprland.lua"
        base = config.read_text()
        for scale in (1, 1.25):
            config.write_text(base + '\nhl.monitor({output="HV-TEST",mode="1280x800@60",position="0x0",scale=' + str(scale) + '})\n')
            self.ctl("reload")
            time.sleep(.3)
            status = json.loads(self.ctl("hyprveil", "status"))
            self.check("owned lab applies scale " + str(scale) + " with safe reload defaults",
                       self.monitor()["scale"] == scale and not self.ctl("configerrors") and status["mode"] == "black")
            self.ctl("hyprveil", "spoiler")
            for width, height in SIZES:
                self.transform(80, 80, width, height)
                geometry = self.geometry()
                self.check("requested small window size is applied", self.protected()["size"] == [width, height],
                           requested=[width, height], actual=self.protected()["size"], scale=scale)
                for variant in VARIANTS:
                    self.configure(variant)
                    name = f"bounds-{scale}-{width}x{height}-{variant}"
                    path, image = self.frame(name, geometry)
                    rect = self.window_rect()
                    crop = self.crop(path, rect, name)
                    pixels = list(image.pixels(self.window_rect(inset=2)))
                    luminance = [sum(pixel) / 3 for pixel in pixels]
                    self.report["bounds"].append(dict(variant=variant, scale=scale, logical_size=[width, height],
                        physical_size=list(rect[2:]), capture=str(path), crop=str(crop),
                        luminance_span=round(max(luminance) - min(luminance), 2),
                        bright_pixels=sum(max(pixel) > 80 for pixel in pixels)))
                    if (width, height) == (128, 96):
                        repeat_path, repeat = self.frame(name + "-repeat", geometry)
                        status = json.loads(self.ctl("hyprveil", "status"))
                        self.check(name + " freezes exactly without an animation timer",
                                   differing_fraction(image, rect, repeat, rect, 0) == 0 and not status["spoiler_animation_armed"])
                # Diagnostic readback is admitted only by this marked lab.
                self.ctl("hyprveil", "dump-local")
                self.capture(self.artifacts / (f"local-{scale}-{width}x{height}-trigger.png"))
                local = Image(self.runtime / "hyprveil-local.png")
                self.check("small original fixture remains visible locally", color_fraction(local, self.window_rect(inset=2), PRIVATE, 3) >= .99)
        self.sample_resources("after-small-fractional-materials")
        self.unload()
        self.check("unload preserves native privacy", self.privacy())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    parser.add_argument("--plugin-sha256", required=True)
    run = StyleBoundsSmoke(parser.parse_args())
    def interrupted(signum, frame):
        raise KeyboardInterrupt("style bounds interrupted")
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        run.start()
        run.tests()
        run.report["ok"] = bool(run.checks) and all(item["ok"] for item in run.checks)
    except (Exception, KeyboardInterrupt) as error:
        run.report.update(ok=False, error=str(error))
        print(json.dumps({"event": "error", "error": str(error)}), flush=True)
    finally:
        run.cleanup()
    print(json.dumps({"ok": run.report["ok"], "report": str(run.artifacts / "report.json") if run.artifacts else None,
                      "lab_stopped": run.report["lab_stopped"]}), flush=True)
    return 0 if run.report["ok"] and run.report["lab_stopped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
