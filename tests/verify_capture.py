#!/usr/bin/env python3
"""Pixel and geometry assertions for synthetic, isolated Hyprveil scenes.

Reads only explicitly supplied PNG/JSON files. No capture command, compositor
socket, or inherited desktop display is accessed by this verifier.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import cairo


def rgb(value: str) -> tuple[int, int, int]:
    value = value.removeprefix("#")
    if len(value) != 6:
        raise argparse.ArgumentTypeError("use six RGB hex digits")
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError as error:
        raise argparse.ArgumentTypeError("invalid RGB color") from error


def rectangle(value: str) -> tuple[int, int, int, int]:
    try:
        parts = tuple(int(part) for part in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("use x,y,width,height in PNG pixels") from error
    if len(parts) != 4 or min(parts[:2]) < 0 or min(parts[2:]) <= 0:
        raise argparse.ArgumentTypeError("use nonnegative x/y and positive width/height")
    return parts


class Image:
    def __init__(self, path: Path):
        surface = cairo.ImageSurface.create_from_png(str(path))
        if surface.get_format() not in (cairo.FORMAT_RGB24, cairo.FORMAT_ARGB32):
            raise ValueError("expected an RGB or RGBA PNG")
        self.width = surface.get_width()
        self.height = surface.get_height()
        self.stride = surface.get_stride()
        self.alpha = surface.get_format() == cairo.FORMAT_ARGB32
        self.data = bytes(surface.get_data())

    def pixels(self, rect):
        x, y, width, height = rect
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise ValueError("invalid region")
        if x + width > self.width or y + height > self.height:
            raise ValueError("region extends beyond image bounds")
        for row in range(y, y + height):
            for column in range(x, x + width):
                offset = row * self.stride + column * 4
                word = int.from_bytes(self.data[offset:offset + 4], sys.byteorder)
                color = ((word >> 16) & 255, (word >> 8) & 255, word & 255)
                alpha = (word >> 24) & 255 if self.alpha else 255
                # Cairo stores native-endian, premultiplied ARGB32. The captured
                # scene should be opaque, but retain source RGB for RGBA fixtures.
                yield tuple(round(component * 255 / alpha) for component in color) if 0 < alpha < 255 else color


def close(first, second, tolerance):
    return all(abs(a - b) <= tolerance for a, b in zip(first, second))


def color_fraction(image: Image, rect, color, tolerance: int) -> float:
    return sum(close(pixel, color, tolerance) for pixel in image.pixels(rect)) / (rect[2] * rect[3])


def opacity_fraction(image: Image, rect) -> float:
    """Distinguish opaque denial frames from transparent black pixels."""
    x, y, width, height = rect
    if min(x, y) < 0 or min(width, height) <= 0 or x + width > image.width or y + height > image.height:
        raise ValueError("region extends beyond image bounds")
    if not image.alpha:
        return 1.0
    alpha_offset = 3 if sys.byteorder == "little" else 0
    opaque = sum(image.data[row * image.stride + column * 4 + alpha_offset] == 255
                 for row in range(y, y + height) for column in range(x, x + width))
    return opaque / (width * height)


def differing_fraction(first: Image, first_rect, second: Image, second_rect, tolerance: int) -> float:
    if first_rect[2:] != second_rect[2:]:
        raise ValueError("compared regions must have equal sizes")
    pixels = zip(first.pixels(first_rect), second.pixels(second_rect), strict=True)
    return sum(not close(a, b, tolerance) for a, b in pixels) / (first_rect[2] * first_rect[3])


def absence_report(image: Image, color, tolerance: int = 3, maximum_fraction: float = 0.0) -> dict:
    """Scan the whole exported frame, including pixels outside any mask ROI."""
    fraction = color_fraction(image, (0, 0, image.width, image.height), color, tolerance)
    return {"ok": fraction <= maximum_fraction, "forbidden_fraction": fraction,
            "width": image.width, "height": image.height}


def geometry_report(before_path: Path, after_path: Path, app_id: str) -> dict:
    def target(path):
        clients = json.loads(path.read_text())
        matches = [client for client in clients if client.get("class") == app_id]
        if len(matches) != 1:
            raise ValueError(f"expected exactly one {app_id} in {path}, got {len(matches)}")
        return matches[0]
    before, after = target(before_path), target(after_path)
    required = ("address", "at", "size", "workspace", "monitor", "floating", "fullscreen")
    for field in required:
        if field not in before or field not in after:
            raise ValueError(f"missing geometry field {field}")
    changed = {field: {"before": before[field], "after": after[field]}
               for field in required if before[field] != after[field]}
    return {"ok": not changed, "app_id": app_id, "changed": changed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    scene = subcommands.add_parser("scene", help="prove local content is omitted and underlay is revealed")
    scene.add_argument("--local", required=True, type=Path)
    scene.add_argument("--capture", required=True, type=Path)
    scene.add_argument("--underlay", required=True, type=Path)
    scene.add_argument("--rect", required=True, type=rectangle)
    scene.add_argument("--protected-color", type=rgb, default=rgb("e84090"))
    scene.add_argument("--minimum-local", type=float, default=0.99)
    scene.add_argument("--maximum-leak", type=float, default=0.0)
    scene.add_argument("--maximum-difference", type=float, default=0.001)
    scene.add_argument("--tolerance", type=int, default=3)

    color = subcommands.add_parser("color", help="assert a solid-color mask or test window region")
    color.add_argument("--image", required=True, type=Path)
    color.add_argument("--rect", required=True, type=rectangle)
    color.add_argument("--color", required=True, type=rgb)
    color.add_argument("--minimum-fraction", type=float, default=0.99)
    color.add_argument("--tolerance", type=int, default=3)

    compare = subcommands.add_parser("compare", help="compare captured pixels with an expected image mask")
    compare.add_argument("--image", required=True, type=Path)
    compare.add_argument("--reference", required=True, type=Path)
    compare.add_argument("--rect", required=True, type=rectangle)
    compare.add_argument("--reference-rect", type=rectangle)
    compare.add_argument("--maximum-difference", type=float, default=0.001)
    compare.add_argument("--tolerance", type=int, default=3)

    absence = subcommands.add_parser("absence", help="reject forbidden pixels anywhere in an exported frame")
    absence.add_argument("--image", required=True, type=Path)
    absence.add_argument("--color", type=rgb, default=rgb("e84090"))
    absence.add_argument("--maximum-fraction", type=float, default=0.0)
    absence.add_argument("--tolerance", type=int, default=3)

    geometry = subcommands.add_parser("geometry", help="prove an existing window was not moved or resized")
    geometry.add_argument("--before", required=True, type=Path)
    geometry.add_argument("--after", required=True, type=Path)
    geometry.add_argument("--app-id", default="org.hyprveil.fixture.protected")
    args = parser.parse_args()
    try:
        if args.command == "scene":
            local, capture, underlay = (Image(path) for path in (args.local, args.capture, args.underlay))
            if len({(im.width, im.height) for im in (local, capture, underlay)}) != 1:
                raise ValueError("local, capture and underlay images must use the same pixel coordinates")
            local_fraction = color_fraction(local, args.rect, args.protected_color, args.tolerance)
            capture_rect = (0, 0, capture.width, capture.height)
            leak_fraction = color_fraction(capture, capture_rect, args.protected_color, args.tolerance)
            difference = differing_fraction(capture, args.rect, underlay, args.rect, args.tolerance)
            report = {
                "ok": local_fraction >= args.minimum_local and leak_fraction <= args.maximum_leak
                      and difference <= args.maximum_difference,
                "local_protected_fraction": local_fraction,
                "capture_protected_fraction": leak_fraction,
                "capture_underlay_difference": difference,
                "rect": args.rect,
            }
        elif args.command == "color":
            fraction = color_fraction(Image(args.image), args.rect, args.color, args.tolerance)
            report = {"ok": fraction >= args.minimum_fraction, "matching_fraction": fraction, "rect": args.rect}
        elif args.command == "compare":
            reference_rect = args.reference_rect or (0, 0, args.rect[2], args.rect[3])
            difference = differing_fraction(Image(args.image), args.rect, Image(args.reference), reference_rect, args.tolerance)
            report = {"ok": difference <= args.maximum_difference, "differing_fraction": difference, "rect": args.rect}
        elif args.command == "absence":
            report = absence_report(Image(args.image), args.color, args.tolerance, args.maximum_fraction)
        else:
            report = geometry_report(args.before, args.after, args.app_id)
    except Exception as error:
        report = {"ok": False, "error": str(error)}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
