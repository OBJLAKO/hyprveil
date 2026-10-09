#!/usr/bin/python3
"""Render and verify real GPU materials in an explicit synthetic lab."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import signal
import sys
import time

import cairo

from lab import PROJECT
from service import APPEARANCE_VARIANTS, DEFAULT_APPEARANCE
from stress import Stress, image_stats

sys.path.insert(0, str(PROJECT / "tests"))
from verify_capture import Image, absence_report, color_fraction, differing_fraction, opacity_fraction

PRIVATE = (232, 64, 144)
CAPTIONS = {"prism": "Shifting planes of iridescent light", "signal": "Warm phosphor, rolling scanlines",
            "aurora": "Flowing teal and violet ribbons", "contour": "Living terracotta topography",
            "radar": "A rotating sonar sweep", "matte": "Still, soft, understated",
            "error404": "Signal lost. Privacy found.", "matrix": "Falling glyphs, phosphor tails",
            "anonymous": "A familiar grin, an original mask", "glass": "Floating lenses, invented reflections"}
DISPLAY_NAMES = {"error404": "404", "matrix": "Matrix", "anonymous": "Anonymous", "glass": "Liquid Glass"}
PLAYFUL = ("error404", "matrix", "anonymous", "glass")
MOTION_FRAMES, MOTION_FPS = 54, 6


def gallery(frames, output, compact=False):
    # The card artwork is the actual native screenshot. Only typography and
    # layout are composited here; the masks are never recreated in Python.
    columns = 5 if compact and len(frames) > 4 else 2
    rows = (len(frames) + columns - 1) // columns
    if compact:
        width, card_width, card_height, row_height = (1192, 216, 138, 195) if columns == 5 else (864, 400, 256, 310)
        height, margin, gap = 65 + rows * row_height, 24, 16
    else:
        width, card_width, card_height, row_height = 1144, 520, 333, 405
        height, margin, gap = 149 + rows * row_height, 40, 24
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    paint = cairo.Context(surface)
    paint.set_source_rgb(.913, .929, .934)
    paint.paint()

    def text(x, y, value, size, shade=(.10, .16, .18), bold=False):
        paint.select_font_face("Nimbus Sans", cairo.FONT_SLANT_NORMAL,
                               cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
        paint.set_font_size(size)
        paint.set_source_rgb(*shade)
        paint.move_to(x, y)
        paint.show_text(value)

    text(margin, 42 if compact else 59, "Hyprveil", 25 if compact else 30, bold=True)
    if not compact:
        text(margin, 87, "Your windows stay yours.", 15, (.35, .42, .44))
    for index, variant in enumerate(frames):
        column, row = index % columns, index // columns
        x, y = margin + column * (card_width + gap), (65 if compact else 120) + row * row_height
        image = cairo.ImageSurface.create_from_png(str(frames[variant]))
        paint.save()
        paint.rectangle(x, y, card_width, card_height)
        paint.clip()
        paint.translate(x, y)
        # Crop the software pointer in the outer corner; retain original PNGs.
        paint.scale(card_width / (image.get_width() - 24), card_height / (image.get_height() - 24))
        paint.set_source_surface(image, -12, -12)
        paint.paint()
        paint.restore()
        text(x, y + card_height + (23 if compact else 29), DISPLAY_NAMES.get(variant, variant.title()), 16 if compact else 18, bold=True)
        if not compact:
            text(x, y + card_height + 51, CAPTIONS[variant], 13, (.35, .42, .44))
    output.parent.mkdir(parents=True, exist_ok=True)
    surface.write_to_png(str(output))


def icon_gallery(frames, output):
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 800, 260)
    paint = cairo.Context(surface)
    paint.set_source_rgb(.913, .929, .934)
    paint.paint()
    for index, (shape, path) in enumerate(frames.items()):
        x, y = 24 + index * 194, 24
        native = cairo.ImageSurface.create_from_png(str(path))
        paint.save()
        paint.rectangle(x, y, 170, 170)
        paint.clip()
        paint.translate(x, y)
        paint.set_source_surface(native, -(native.get_width() - 170) / 2, -(native.get_height() - 170) / 2)
        paint.paint()
        paint.restore()
        paint.select_font_face("Nimbus Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        paint.set_font_size(17)
        paint.set_source_rgb(.10, .16, .18)
        paint.move_to(x, 226)
        paint.show_text(shape.title())
    surface.write_to_png(str(output))


class DesignGallery(Stress):
    def check(self, name, ok, **details):
        self.record(name, ok, **details)
        if not ok:
            raise RuntimeError("assertion failed: " + name)

    def configure(self, **updates):
        values = dict(DEFAULT_APPEARANCE, darkness=35)
        values.update(updates)
        result = json.loads(self.ctl("hyprveil", "appearance", values["variant"], values["color"],
            str(values["grain"]), str(values["speed"]), str(values["darkness"]),
            "1" if values["eye"] else "0", str(values["eye_size"]), values["icon"], str(values["icon_opacity"])))
        actual = dict(result.get("appearance", {}), icon=result.get("icon"), icon_opacity=result.get("icon_opacity"))
        self.check(values["variant"] + " native settings acknowledged", actual == values and result.get("mode") == "spoiler")
        return result

    def motion(self, geometry):
        paths = {}
        for variant in APPEARANCE_VARIANTS:
            self.configure(variant=variant, eye=False, speed=125, grain=0)
            paths[variant] = []
            started = time.monotonic()
            for index in range(MOTION_FRAMES):
                delay = started + index / MOTION_FPS - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                path = self.artifacts / ("motion-" + variant + f"-{index:02d}.png")
                self.capture(path)
                image = Image(path)
                self.check(variant + f" motion frame {index} stays opaque and private",
                           image_stats(path)["private_fraction"] == 0 and
                           opacity_fraction(image, (0, 0, image.width, image.height)) == 1)
                paths[variant].append(path)
            self.check(variant + " motion keeps window policy and placement", self.geometry() == geometry and self.privacy())
            first, last = Image(paths[variant][0]), Image(paths[variant][-1])
            changed = differing_fraction(first, (12, 12, 976, 616), last, (12, 12, 976, 616), 3)
            self.check(variant + " has geometric motion beyond fine grain" if variant != "matte" else "matte remains still in the motion gallery",
                       changed > .02 if variant != "matte" else changed == 0, changed_fraction=changed)
        for index in range(MOTION_FRAMES):
            gallery({variant: paths[variant][index] for variant in APPEARANCE_VARIANTS},
                    self.artifacts / f"gallery-motion-{index:02d}.png", compact=True)
            gallery({variant: paths[variant][index] for variant in PLAYFUL},
                    self.artifacts / f"playful-motion-{index:02d}.png", compact=True)
        for prefix, widths in (("gallery", (1192, 1040, 900)), ("playful", (864, 760, 660))):
            output = self.args.output.with_name("motion.gif" if prefix == "gallery" else "playful.gif")
            for width in widths:
                graph = f"scale={width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3"
                self.command(["/usr/bin/ffmpeg", "-y", "-loglevel", "error", "-framerate", str(MOTION_FPS), "-i",
                              str(self.artifacts / (prefix + "-motion-%02d.png")), "-filter_complex", graph,
                              "-loop", "0", str(output)], timeout=60)
                if output.stat().st_size <= 4 * 1024 * 1024:
                    break
        return self.args.output.with_name("motion.gif")

    def frame(self, name, geometry):
        path = self.artifacts / (name + ".png")
        self.capture(path)
        image = Image(path)
        absence = absence_report(image, PRIVATE, 8)
        self.check(name + " opaque with no synthetic private pixels",
                   absence["ok"] and opacity_fraction(image, (0, 0, image.width, image.height)) == 1
                   and self.geometry() == geometry and self.privacy(),
                   forbidden_fraction=absence["forbidden_fraction"])
        return path, image

    def typography(self, geometry):
        # Test the exported pixels as readable 4/0/4, including the dark
        # counters. A colored rectangle or missing glyph must fail this check.
        self.configure(variant="error404", grain=0, speed=0, eye=False)
        _, image = self.frame("error404-typography", geometry)
        digits = ("00010 00110 01010 10010 11111 00010 00010",
                  "01110 10001 10011 10101 11001 10001 01110",
                  "00010 00110 01010 10010 11111 00010 00010")
        mismatches = 0
        scale = min(1, image.width / image.height / 1.35)
        for index, digit in enumerate(digits):
            for row, bits in enumerate(digit.split()):
                for column, bit in enumerate(bits):
                    x = round(image.width / 2 + (-.51 + (index + (column + .5) / 5 * .84) * .34) * scale * image.height)
                    y = round(image.height / 2 + (-.22 + (row + .5) / 7 * .39) * scale * image.height)
                    color = next(image.pixels((x, y, 1, 1)))
                    mismatches += (max(color) > 90) != (bit == "1")
        self.check("404 exported bitmap typography and dark counters are legible", mismatches == 0, mismatched_cells=mismatches)

    def tests(self):
        self.check("gallery uses the explicit reviewed binary", self.report["plugin_sha256"] == self.args.plugin_sha256)
        self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        config = self.runtime / "hyprland.lua"
        width, height = (1920, 1080) if self.args.preview else (1000, 640)
        with config.open("a") as handle:
            handle.write(f'\nhl.monitor({{output="HV-TEST",mode="{width}x{height}@60",position="0x0",scale=1}})\n')
        self.ctl("reload")
        self.check("synthetic gallery configuration has no errors", not self.ctl("configerrors"))
        self.fixture("protected")
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        self.dispatch('hl.dsp.window.fullscreen({window=' + json.dumps(self.address()) + ',action="set",mode="fullscreen",layout_aware=false})')
        self.dispatch('hl.dsp.cursor.move({x=0,y=0})')
        self.ctl("dismissnotify", "-1")
        self.load()
        time.sleep(.3)
        self.ctl("hyprveil", "spoiler")
        geometry = self.geometry()
        rect = (12, 12, width - 24, height - 24)
        self.ctl("hyprveil", "dump-local")
        self.capture(self.artifacts / "local-trigger.png")
        shutil.copyfile(self.runtime / "hyprveil-local.png", self.artifacts / "local.png")
        local = Image(self.artifacts / "local.png")
        self.check("synthetic private fixture stays visible locally", color_fraction(local, rect, PRIVATE, 3) > .99)
        frames = {}
        for variant in PLAYFUL if self.args.preview else APPEARANCE_VARIANTS:
            self.configure(variant=variant, eye=False)
            path, first = self.frame(variant, geometry)
            time.sleep(.35)
            _, second = self.frame(variant + "-later", geometry)
            difference = differing_fraction(first, rect, second, rect, 0)
            status = json.loads(self.ctl("hyprveil", "status"))
            self.check(variant + (" remains still without a timer" if variant == "matte" else " animates deliberately"),
                       difference == 0 and not status["spoiler_animation_armed"] if variant == "matte" else difference > .01,
                       changed_fraction=difference)
            frames[variant] = path
            if self.args.preview:
                continue
            self.configure(variant=variant, speed=0, eye=False)
            _, frozen = self.frame(variant + "-frozen", geometry)
            time.sleep(.15)
            _, repeat = self.frame(variant + "-frozen-repeat", geometry)
            status = json.loads(self.ctl("hyprveil", "status"))
            self.check(variant + " speed zero freezes and disarms timer",
                       differing_fraction(frozen, rect, repeat, rect, 0) == 0 and not status["spoiler_animation_armed"])
            for boundary, updates in (("darkness", {"darkness": 100}), ("black-tint", {"color": "#000000"})):
                self.configure(variant=variant, eye=False, **updates)
                _, black = self.frame(variant + "-" + boundary, geometry)
                self.check(variant + " " + boundary + " stays opaque black", color_fraction(black, rect, (0, 0, 0), 0) == 1)
        gallery(frames, self.args.output)
        if self.args.preview:
            self.report.update(gallery=str(self.args.output), frames={key: str(value) for key, value in frames.items()}, ok=True)
            return
        gallery({variant: frames[variant] for variant in PLAYFUL}, self.args.output.with_name("playful.png"))
        self.typography(geometry)
        motion = self.motion(geometry)
        icons = {}
        for shape in ("eye", "lock", "shield", "none"):
            self.configure(variant="matte", darkness=100, eye=True, eye_size=128, icon=shape, icon_opacity=75)
            icons[shape], _ = self.frame("icon-" + shape, geometry)
        icon_gallery(icons, self.args.output.with_name("icons.png"))
        provenance = {"version": 1, "renderer": "Hyprveil native GLES shader; marked synthetic Hyprland lab",
                      "plugin_sha256": self.report["plugin_sha256"], "variants": list(APPEARANCE_VARIANTS),
                      "settings": dict(DEFAULT_APPEARANCE, darkness=35, eye=False), "native_size": [1000, 640],
                      "motion": {"file": motion.name, "frames": MOTION_FRAMES, "fps": MOTION_FPS, "native_capture": True,
                                 "settings": dict(DEFAULT_APPEARANCE, darkness=35, eye=False, speed=125, grain=0),
                                 "sha256": hashlib.sha256(motion.read_bytes()).hexdigest()},
                      "playful": {"file": "playful.gif", "variants": list(PLAYFUL), "frames": MOTION_FRAMES,
                                  "fps": MOTION_FPS, "sha256": hashlib.sha256(self.args.output.with_name("playful.gif").read_bytes()).hexdigest(),
                                  "still": "playful.png", "still_sha256": hashlib.sha256(self.args.output.with_name("playful.png").read_bytes()).hexdigest()},
                      "icons": {"file": "icons.png", "shapes": ["eye", "lock", "shield", "none"],
                                "sha256": hashlib.sha256(self.args.output.with_name("icons.png").read_bytes()).hexdigest()},
                      "gallery_sha256": hashlib.sha256(self.args.output.read_bytes()).hexdigest(),
                      "checks": len(self.checks), "all_checks_passed": all(item["ok"] for item in self.checks)}
        self.args.output.with_suffix(".json").write_text(json.dumps(provenance, indent=2) + "\n")
        self.report.update(gallery=str(self.args.output), frames={key: str(value) for key, value in frames.items()}, ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    parser.add_argument("--plugin-sha256", required=True)
    parser.add_argument("--output", type=Path, default=PROJECT / "assets/styles/gallery.png")
    parser.add_argument("--preview", action="store_true", help="Render just the four expressive styles at 1920x1080 for visual review")
    args = parser.parse_args()
    args.suite = "design-gallery"
    run = DesignGallery(args)

    def interrupted(signum, frame):
        raise KeyboardInterrupt("design gallery interrupted")

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        run.start()
        run.tests()
    except (Exception, KeyboardInterrupt) as error:
        run.report.update(error=str(error), ok=False)
        print(json.dumps({"event": "error", "error": str(error)}), flush=True)
    finally:
        run.cleanup()
    print(json.dumps({"ok": run.report["ok"], "report": str(run.artifacts / "report.json") if run.artifacts else None,
                      "gallery": run.report.get("gallery"), "lab_stopped": run.report.get("lab_stopped")}), flush=True)
    return 0 if run.report["ok"] and run.report.get("lab_stopped") and not run.report.get("cleanup_errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
