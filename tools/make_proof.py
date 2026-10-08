#!/usr/bin/env python3
"""Arrange synthetic lab captures into a comparison; never captures a desktop."""
import argparse
import json
from pathlib import Path
import cairo


def evidence_caption(root):
    report = json.loads((root / "report.json").read_text())
    checks = report.get("checks", [])
    if not report.get("ok") or not checks or any(check.get("ok") is not True for check in checks):
        raise ValueError("comparison requires a completed successful smoke report")
    version = report.get("compositor", {}).get("version", "unknown")
    capture_tool = report.get("capture_tool", "unknown")
    # A client name alone does not establish which Wayland protocol it used.
    return f"{len(checks)} проверок пройдены • Hyprland {version} • {capture_tool} • синтетические окна"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_dir", type=Path)
    args = parser.parse_args()
    root = args.artifact_dir
    caption = evidence_caption(root)
    panels = [
        ("Локально: окно остаётся видно", "local.png"),
        ("Штатный захват: чёрная маска", "native-black.png"),
        ("Hyprveil: окно исключено", "omitted.png"),
        ("Hyprveil: содержимое заменено PNG", "image-mask.png"),
    ]
    canvas = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1088, 984)
    context = cairo.Context(canvas)
    context.set_source_rgb(0.055, 0.075, 0.10)
    context.paint()
    context.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    context.set_source_rgb(0.95, 0.96, 0.98)
    context.set_font_size(30)
    context.move_to(24, 44)
    context.show_text("Hyprveil — реальные кадры тестового Hyprland")
    context.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    context.set_font_size(18)
    context.set_source_rgb(0.65, 0.72, 0.78)
    context.move_to(24, 76)
    context.show_text("Одно окно, тот же размер и положение. Отдельная сцена для захвата.")
    for index, (label, filename) in enumerate(panels):
        column, row = index % 2, index // 2
        x, y = 24 + column * 536, 110 + row * 430
        context.set_source_rgb(0.95, 0.96, 0.98)
        context.set_font_size(20)
        context.move_to(x, y + 22)
        context.show_text(label)
        image = cairo.ImageSurface.create_from_png(str(root / filename))
        if (image.get_width(), image.get_height()) != (1024, 768):
            raise ValueError("comparison expects the 1024×768 synthetic lab output")
        context.save()
        context.translate(x, y + 36)
        context.scale(0.5, 0.5)
        context.set_source_surface(image, 0, 0)
        context.paint()
        context.restore()
    context.set_source_rgb(0.65, 0.72, 0.78)
    context.set_font_size(16)
    context.move_to(24, 976)
    context.show_text(caption)
    destination = root / "comparison.png"
    canvas.write_to_png(str(destination))
    print(destination)


if __name__ == "__main__":
    main()
