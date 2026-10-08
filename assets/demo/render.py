#!/usr/bin/env python3
"""Frame real synthetic captures for the README; generate no mask pixels.

Usage: python3 assets/demo/render.py PATH/TO/readme-demo/report.json
The capture report must confirm its marked lab was stopped successfully.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import cairo

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "assets"


def ink(ctx, value):
    ctx.set_source_rgb(*(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)))


def text(ctx, x, y, value, size, color="b9cbc2", bold=False):
    ink(ctx, color)
    ctx.select_font_face("Liberation Sans", cairo.FONT_SLANT_NORMAL,
                         cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(size)
    ctx.move_to(x, y)
    ctx.show_text(value)


def roundrect(ctx, x, y, width, height, radius):
    from math import pi
    ctx.new_sub_path()
    for cx, cy, a in ((x + width - radius, y + radius, -pi / 2),
                      (x + width - radius, y + height - radius, 0),
                      (x + radius, y + height - radius, pi / 2),
                      (x + radius, y + radius, pi)):
        ctx.arc(cx, cy, radius, a, a + pi / 2)
    ctx.close_path()


def picture(ctx, path, x, y, width, height):
    image = cairo.ImageSurface.create_from_png(str(path))
    ctx.save()
    roundrect(ctx, x, y, width, height, 12)
    ctx.clip()
    ctx.translate(x, y)
    ctx.scale(width / image.get_width(), height / image.get_height())
    ctx.set_source_surface(image)
    ctx.get_source().set_filter(cairo.FILTER_BEST)
    ctx.paint()
    ctx.restore()
    roundrect(ctx, x + .5, y + .5, width - 1, height - 1, 11.5)
    ink(ctx, "34483f")
    ctx.set_line_width(1)
    ctx.stroke()


def canvas(width, height):
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    ctx = cairo.Context(surface)
    gradient = cairo.LinearGradient(0, 0, width, height)
    gradient.add_color_stop_rgb(0, .07, .12, .11)
    gradient.add_color_stop_rgb(1, .04, .07, .08)
    ctx.set_source(gradient)
    ctx.paint()
    return surface, ctx


def frame(local, masked, variant, path):
    surface, ctx = canvas(960, 472)
    text(ctx, 32, 50, "Your screen.", 26, "eceee5", True)
    text(ctx, 494, 50, "Shared capture.", 26, "eceee5", True)
    text(ctx, 32, 77, "Your window stays visible", 14)
    text(ctx, 494, 77, "Protected with Hyprveil", 14)
    roundrect(ctx, 839, 26, 89, 29, 14.5)
    ink(ctx, "263f35")
    ctx.fill()
    text(ctx, 854 if variant == "Satin" else 848, 45, variant.upper(), 11, "c0e0ce", True)
    picture(ctx, local, 32, 110, 434, 271.25)
    picture(ctx, masked, 494, 110, 434, 271.25)
    ink(ctx, "82a994")
    ctx.set_line_width(1.2)
    ctx.move_to(474, 246)
    ctx.line_to(486, 246)
    ctx.line_to(482, 242)
    ctx.move_to(486, 246)
    ctx.line_to(482, 250)
    ctx.stroke()
    text(ctx, 32, 428, "Same window. Two views.", 18, "d9e4d9", True)
    text(ctx, 32, 452, "Real GPU frames · synthetic notes only", 12, "8cab99")
    text(ctx, 750, 445, "hyprveil / " + variant.lower(), 13, "91baa3")
    surface.write_to_png(str(path))


def social(local, masked, path):
    surface, ctx = canvas(1280, 640)
    text(ctx, 60, 93, "Hyprveil", 62, "f0f0e7", True)
    text(ctx, 62, 140, "Privacy, with a little atmosphere.", 26, "b6cbbd")
    text(ctx, 951, 91, "NATIVE HYPRLAND", 13, "a4c3b0", True)
    text(ctx, 60, 205, "Your screen.", 22, "dce9dd", True)
    text(ctx, 660, 205, "Shared capture.", 22, "dce9dd", True)
    text(ctx, 1107, 203, "TELEGRAM", 12, "a9d2ba", True)
    picture(ctx, local, 60, 230, 560, 350)
    picture(ctx, masked, 660, 230, 560, 350)
    text(ctx, 60, 616, "Your window stays visible. The capture gets a veil.", 17, "bad0bf")
    text(ctx, 974, 615, "ACTUAL SYNTHETIC CAPTURE", 10, "8eae99", True)
    surface.write_to_png(str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report_path = args.report.resolve(strict=True)
    source = report_path.parent
    if (not report_path.is_relative_to((ROOT / "artifacts").resolve(strict=True)) or
            source.name != "readme-demo" or not source.parent.name.startswith("hv-")):
        raise RuntimeError("render only an owned README capture report inside this repository's synthetic artifacts")
    report = json.loads(report_path.read_text())
    if not report.get("ok") or not report.get("lab_stopped") or report.get("cleanup_errors"):
        raise RuntimeError("refusing unsuccessful or running lab media")
    expected = {variant: [variant + "-%03d.png" % index for index in range(24)]
                for variant in ("satin", "telegram")}
    if (report.get("local_frame") != "local.png" or report.get("demo_frames") != expected or
            report.get("native_frame_size") != [800, 500] or
            len(report.get("checks", [])) != 3 or not all(check.get("ok") for check in report["checks"])):
        raise RuntimeError("refusing foreign or incomplete synthetic capture schema")
    local = source / report["local_frame"]
    with tempfile.TemporaryDirectory(prefix="hyprveil-readme-") as directory:
        frames = Path(directory)
        index = 0
        for variant, names in report["demo_frames"].items():
            for name in names:
                frame(local, source / name, variant.title(), frames / ("%03d.png" % index))
                index += 1
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", "10", "-i", str(frames / "%03d.png"),
                   "-filter_complex", "split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
                   "-loop", "0", str(ASSETS / "demo.gif")]
        subprocess.run(command, check=True)
    frame(local, source / report["demo_frames"]["telegram"][0], "Telegram", ASSETS / "demo/poster.png")
    social(local, source / report["demo_frames"]["telegram"][0], ASSETS / "social-preview.png")
    outputs = ("hero.svg", "demo.gif", "social-preview.png", "demo/poster.png")
    provenance = {"version": 1, "native_sha256": report["plugin_sha256"],
                  "source": "real Hyprland GPU captures in a fresh marked isolated compositor",
                  "content": "GTK notes fixture with explicitly synthetic text; no personal desktop or files",
                  "frames": index, "display_fps": 10, "native_frame_size": report["native_frame_size"],
                  "variants": ["satin", "telegram"], "appearance": report["demo_settings"],
                  "local_document_marker_visible": True, "marker_absent_from_every_protected_frame": True,
                  "lab_stopped": True,
                  "processing": "crop-free scaling, rounded framing, labels, GIF palette quantization; no synthesized mask pixels or interpolated motion",
                  "outputs": {name: {"bytes": (ASSETS / name).stat().st_size,
                                     "sha256": hashlib.sha256((ASSETS / name).read_bytes()).hexdigest()} for name in outputs}}
    (ASSETS / "demo/provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(provenance, indent=2))


if __name__ == "__main__":
    main()
