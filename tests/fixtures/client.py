#!/usr/bin/env python3
"""Deterministic native Wayland windows, only for an explicit Hyprveil lab.

The lab launcher owns .hyprveil-lab, a JSON marker with ``runtime_dir`` and
``wayland_display``. This program never contacts hyprctl or changes window rules.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys


ROLES = {
    "background": ("org.hyprveil.fixture.background", "2484c4"),
    "protected": ("org.hyprveil.fixture.protected", "e84090"),
    "foreground": ("org.hyprveil.fixture.foreground", "f2cf52"),
}


def lab_environment(lab_dir: Path, wayland_display: str) -> dict:
    """Reject accidental execution against an inherited desktop display."""
    lab_dir = lab_dir.resolve(strict=True)
    marker = lab_dir / ".hyprveil-lab"
    data = json.loads(marker.read_text())
    runtime = Path(data["runtime_dir"]).resolve(strict=True)
    if runtime != lab_dir:
        raise ValueError("runtime_dir must be the explicit isolated lab directory")
    if data["wayland_display"] != wayland_display:
        raise ValueError("explicit display does not match the lab marker")
    if not wayland_display or "/" in wayland_display:
        raise ValueError("the lab display must be a socket name, not a path")
    if os.environ.get("WAYLAND_DISPLAY") != wayland_display:
        raise ValueError("WAYLAND_DISPLAY does not match the explicit lab display")
    if Path(os.environ.get("XDG_RUNTIME_DIR", "/nonexistent")).resolve() != runtime:
        raise ValueError("XDG_RUNTIME_DIR does not match the lab marker")
    if os.environ.get("DISPLAY"):
        raise ValueError("unset DISPLAY before running a native Wayland fixture")
    if not (runtime / wayland_display).is_socket():
        raise ValueError("the marked Wayland socket does not exist")
    os.environ["GDK_BACKEND"] = "wayland"
    return data


def parse_color(value: str) -> tuple[float, float, float]:
    value = value.removeprefix("#")
    if len(value) != 6:
        raise argparse.ArgumentTypeError("use an RGB hex color, e.g. e84090")
    try:
        return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError as error:
        raise argparse.ArgumentTypeError("invalid RGB hex color") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-dir", required=True, type=Path)
    parser.add_argument("--wayland-display", required=True)
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--color", type=parse_color)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--quit-after", type=int, default=0,
                        help="quit after this many seconds; zero keeps the fixture open")
    args = parser.parse_args()
    if args.width < 32 or args.height < 32 or args.quit_after < 0:
        parser.error("dimensions must be at least 32 and quit-after must be nonnegative")
    try:
        lab_environment(args.lab_dir, args.wayland_display)
        lab_dir = args.lab_dir.resolve(strict=True)
        if args.ready_file and not args.ready_file.resolve().is_relative_to(lab_dir):
            raise ValueError("ready-file must live inside the explicit lab directory")
    except (OSError, ValueError, KeyError) as error:
        parser.error(f"refusing to run outside the marked lab: {error}")

    # Import the GUI only after the environment guard has forced native Wayland.
    import gi
    gi.require_foreign("cairo")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkWayland", "4.0")
    from gi.repository import Gdk, GdkWayland, Gio, GLib, Gtk

    app_id, default_color = ROLES[args.role]
    color = args.color or parse_color(default_color)
    title = f"Hyprveil fixture: {args.role}"
    # Without a session bus GTK's default Wayland app-id falls back to the
    # Python executable name, even when Gtk.Application has application_id.
    GLib.set_prgname(app_id)
    GLib.set_application_name(title)

    class Fixture(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=app_id, flags=Gio.ApplicationFlags.NON_UNIQUE)
            self.ready = False
            self.window = None

        def do_activate(self):
            display = Gdk.Display.get_default()
            if display is None or display.get_name() != args.wayland_display:
                raise RuntimeError("GTK did not connect to the marked Wayland display")
            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title(title)
            self.window.set_decorated(False)
            self.window.set_default_size(args.width, args.height)
            self.window.connect("realize", self.identify_surface)
            drawing = Gtk.DrawingArea()
            drawing.set_draw_func(self.draw)
            self.window.set_child(drawing)
            self.window.present()
            if args.quit_after:
                GLib.timeout_add_seconds(args.quit_after, self.stop)

        def identify_surface(self, window):
            # Set xdg_toplevel.app_id before mapping, so initialClass and class
            # both match the deterministic test rules in the isolated lab.
            surface = window.get_surface()
            if not isinstance(surface, GdkWayland.WaylandToplevel):
                raise RuntimeError("fixture did not realize a native Wayland toplevel")
            surface.set_application_id(app_id)

        def stop(self, *unused):
            self.quit()
            return GLib.SOURCE_REMOVE

        def draw(self, area, context, width, height):
            context.set_source_rgb(*color)
            context.paint()
            if not self.ready:
                self.ready = True
                report = {
                    "app_id": app_id, "title": title, "role": args.role,
                    "pid": os.getpid(), "width": width, "height": height,
                    "color": [round(component * 255) for component in color],
                    "wayland_display": args.wayland_display,
                }
                text = json.dumps(report, sort_keys=True) + "\n"
                if args.ready_file:
                    temporary = args.ready_file.with_suffix(args.ready_file.suffix + ".tmp")
                    temporary.write_text(text)
                    temporary.replace(args.ready_file)
                print(text, end="", flush=True)

    app = Fixture()
    for signum in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, app.stop)
    return app.run([])


if __name__ == "__main__":
    sys.exit(main())
