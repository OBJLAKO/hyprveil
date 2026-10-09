#!/usr/bin/env python3
"""Render the illustrated GitHub cover with Pycairo and rsvg-convert.

No desktop or capture APIs are used. Text is embedded as vector glyph paths.
"""
from pathlib import Path
import hashlib
import json
import subprocess

import cairo

ROOT = Path(__file__).resolve().parent
WIDTH, HEIGHT = 1280, 640
INK = "#192A30"
MUTED = "#5C6C74"
ACCENT = "#168477"
BORDER = "#D6DFE3"


def color(context, value):
    context.set_source_rgb(*(int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)))


def rounded(context, x, y, width, height, radius, fill, stroke=None, line=1):
    context.new_sub_path()
    context.arc(x + width - radius, y + radius, radius, -1.570796327, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, 1.570796327)
    context.arc(x + radius, y + height - radius, radius, 1.570796327, 3.141592654)
    context.arc(x + radius, y + radius, radius, 3.141592654, 4.712388980)
    context.close_path()
    color(context, fill)
    context.fill_preserve()
    if stroke:
        color(context, stroke)
        context.set_line_width(line)
        context.stroke()
    else:
        context.new_path()


def text(context, x, baseline, value, size=16, fill=INK, bold=False):
    context.select_font_face("Adwaita Sans", cairo.FONT_SLANT_NORMAL,
                             cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    context.set_font_size(size)
    color(context, fill)
    context.move_to(x, baseline)
    context.show_text(value)


def centered(context, x, baseline, value, size=16, fill=INK, bold=False):
    context.select_font_face("Adwaita Sans", cairo.FONT_SLANT_NORMAL,
                             cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    context.set_font_size(size)
    width = context.text_extents(value).x_advance
    text(context, x - width / 2, baseline, value, size, fill, bold)


def line(context, x1, y1, x2, y2, fill=BORDER, width=1):
    color(context, fill)
    context.set_line_width(width)
    context.set_line_cap(cairo.LINE_CAP_BUTT)
    context.move_to(x1, y1)
    context.line_to(x2, y2)
    context.stroke()


def public_window(context, x, y):
    rounded(context, x, y, 240, 236, 8, "#FFFFFF", BORDER)
    text(context, x + 18, y + 28, "Stream checklist", bold=True)
    line(context, x, y + 44, x + 240, y + 44)
    for index, value in enumerate(("Camera ready", "Slides ready", "Audio checked")):
        top = y + 74 + index * 42
        rounded(context, x + 18, top - 12, 16, 16, 4, "#E4F1EE")
        context.set_line_cap(cairo.LINE_CAP_ROUND)
        context.set_line_join(cairo.LINE_JOIN_ROUND)
        color(context, ACCENT)
        context.set_line_width(1.7)
        context.move_to(x + 22, top - 4)
        context.line_to(x + 25, top - 1)
        context.line_to(x + 30, top - 7)
        context.stroke()
        text(context, x + 46, top + 1, value)


def private_window(context, x, y, shared):
    if not shared:
        rounded(context, x, y, 312, 194, 8, "#FFFFFF", ACCENT, 1.5)
        text(context, x + 20, y + 29, "Private notes", bold=True)
        line(context, x + .75, y + 45, x + 311.25, y + 45)
        text(context, x + 20, y + 82, "Birthday gift ideas")
        text(context, x + 20, y + 118, "Weekend plans")
        text(context, x + 20, y + 154, "Personal reminders", fill=MUTED)
        return
    rounded(context, x, y, 312, 194, 8, INK)
    # A clean lock communicates a protected window without suggesting blur.
    center = x + 156
    context.set_line_width(2.5)
    context.set_line_cap(cairo.LINE_CAP_ROUND)
    color(context, "#ECF5F2")
    context.move_to(center - 12, y + 82)
    context.line_to(center - 12, y + 71)
    context.curve_to(center - 12, y + 54, center + 12, y + 54, center + 12, y + 71)
    context.line_to(center + 12, y + 82)
    context.stroke()
    rounded(context, center - 19, y + 82, 38, 29, 5, INK, "#ECF5F2", 2.5)
    centered(context, center, y + 145, "Hidden from viewers", fill="#ECF5F2")


def desktop(context, x, shared):
    y = 240
    rounded(context, x, y, 560, 330, 12, "#EDF1F3", BORDER)
    public_window(context, x + 24, y + 28)
    private_window(context, x + 224, y + 112, shared)


def draw(surface):
    context = cairo.Context(surface)
    color(context, "#FFFFFF")
    context.paint()
    text(context, 64, 108, "Hyprveil", size=56, bold=True)
    text(context, 66, 153, "Keep private windows out of screen shares.", size=23, fill=MUTED)
    text(context, 64, 218, "Your desktop", bold=True)
    text(context, 656, 218, "Screen share", bold=True)
    desktop(context, 64, False)
    desktop(context, 656, True)


def main():
    svg = ROOT / "hero.svg"
    surface = cairo.SVGSurface(str(svg), WIDTH, HEIGHT)
    surface.set_document_unit(cairo.SVG_UNIT_PX)
    draw(surface)
    surface.finish()
    # Cairo embeds glyph paths: the artwork does not depend on browser fonts.
    value = svg.read_text()
    value = value.replace('viewBox="0 0 1280 640"', 'viewBox="0 0 1280 640" role="img" aria-labelledby="title desc"')
    value = value.replace('<defs>', '<title id="title">Hyprveil — hide private windows from screen shares</title>\n'
                          '<desc id="desc">Illustration: the same public checklist and private notes appear on a desktop and a screen share. '
                          'The notes are visible only on the desktop; the screen share shows an opaque protected window.</desc>\n<defs>', 1)
    svg.write_text(value)
    png = ROOT / "social-preview.png"
    subprocess.run(["rsvg-convert", "-o", str(png), str(svg)], check=True)
    # Both scenes are identical outside the one deliberately changed window.
    # Include two pixels around its boundary to cover antialiasing of strokes.
    raster = cairo.ImageSurface.create_from_png(str(png))
    pixels, stride = bytes(raster.get_data()), raster.get_stride()
    differences, maximum_delta = 0, 0
    for y in range(330):
        for x in range(560):
            if 222 <= x <= 538 and 110 <= y <= 308:
                continue
            left = (240 + y) * stride + (64 + x) * 4
            right = (240 + y) * stride + (656 + x) * 4
            delta = max(abs(pixels[left + channel] - pixels[right + channel]) for channel in range(4))
            maximum_delta = max(maximum_delta, delta)
            differences += delta > 2  # Subpixel stroke quantization can differ by 1–2 RGB levels.
    assert differences == 0, "the public window and desktop background must match"
    provenance = {
        "version": 1,
        "kind": "illustration",
        "renderer": "Pycairo vector artwork, rasterized from the SVG with librsvg",
        "description": "Generic fictional notes are visible locally and covered by an opaque mask in the screen share.",
        "dimensions": [WIDTH, HEIGHT],
        "desktop_capture": False,
        "checks": {"scene_pixels_different_outside_private_window_above_tolerance": differences,
                   "rgb_tolerance": 2, "maximum_antialias_delta": maximum_delta,
                   "same_private_window_geometry": [224, 112, 312, 194],
                   "comparison_boundary_padding": 2},
        "sha256": {file.name: hashlib.sha256(file.read_bytes()).hexdigest()
                   for file in (svg, png, Path(__file__))},
    }
    (ROOT / "cover-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")


if __name__ == "__main__":
    main()
