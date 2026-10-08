#!/usr/bin/env python3
"""A real GTK demo document, admitted only inside an explicit synthetic lab."""
import argparse
import json
from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/fixtures"))
from client import lab_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-dir", required=True, type=Path)
    parser.add_argument("--wayland-display", required=True)
    args = parser.parse_args()
    lab_environment(args.lab_dir, args.wayland_display)

    import gi
    gi.require_foreign("cairo")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkWayland", "4.0")
    from gi.repository import Gdk, GdkWayland, Gio, GLib, Gtk

    app_id = "org.hyprveil.fixture.protected"
    GLib.set_prgname(app_id)
    GLib.set_application_name("Hyprveil synthetic notes")

    class Notes(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=app_id, flags=Gio.ApplicationFlags.NON_UNIQUE)

        def do_activate(self):
            display = Gdk.Display.get_default()
            if display is None or display.get_name() != args.wayland_display:
                raise RuntimeError("demo connected to a different display")
            window = Gtk.ApplicationWindow(application=self)
            window.set_title("Hyprveil demo: synthetic notes")
            window.set_decorated(False)
            window.set_default_size(800, 500)
            window.connect("realize", lambda w: w.get_surface().set_application_id(app_id))
            area = Gtk.DrawingArea()
            area.set_draw_func(self.draw)
            window.set_child(area)
            window.present()

        def draw(self, area, ctx, width, height):
            import cairo
            ctx.scale(width / 800, height / 500)

            def color(value):
                ctx.set_source_rgb(*(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)))

            def rect(x, y, w, h, value):
                color(value)
                ctx.rectangle(x, y, w, h)
                ctx.fill()

            def text(x, y, value, size, ink, bold=False, mono=False):
                color(ink)
                ctx.select_font_face("Liberation Mono" if mono else "Liberation Sans", cairo.FONT_SLANT_NORMAL,
                                     cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
                ctx.set_font_size(size)
                ctx.move_to(x, y)
                ctx.show_text(value)

            rect(0, 0, 800, 500, "f0eee7")
            rect(0, 0, 184, 500, "142622")
            rect(184, 0, 616, 58, "e6e5dc")
            text(24, 44, "studio / notes", 18, "c1dccb", True)
            text(24, 100, "WORKSPACE", 11, "6d9788", True)
            rect(14, 116, 156, 40, "263e35")
            text(26, 142, "release.md", 15, "e8eee5", mono=True)
            text(26, 190, "ideas.md", 15, "789789", mono=True)
            text(26, 231, "archive", 15, "789789", mono=True)
            text(218, 36, "release.md", 15, "4c6156", mono=True)
            text(667, 35, "LOCAL DRAFT", 11, "4e7764", True)
            text(224, 117, "A quieter workspace.", 30, "223c31", True)
            text(226, 152, "A small place for unfinished thoughts.", 17, "6b7a6e")
            rect(224, 181, 512, 1, "cfD5c8")
            text(226, 223, "01", 13, "4e7a61", mono=True)
            text(266, 224, "Write something worth keeping.", 18, "354e3f")
            text(226, 270, "02", 13, "4e7a61", mono=True)
            text(266, 271, "Leave room for the next idea.", 18, "354e3f")
            text(226, 317, "03", 13, "4e7a61", mono=True)
            text(266, 318, "Share only when it is ready.", 18, "354e3f")
            rect(224, 351, 512, 56, "e0e7da")
            text(242, 385, "Everything here is synthetic demo content.", 15, "486a54")
            # This distinctive marker is checked locally and must disappear
            # from every real protected capture, before any media composition.
            rect(24, 422, 24, 24, "e84090")
            text(61, 439, "SYNTHETIC", 11, "acc6b5", True)
            text(226, 463, "No personal documents. No personal desktop.", 13, "6b7a6e")

        def stop(self, *unused):
            self.quit()
            return GLib.SOURCE_REMOVE

    app = Notes()
    for signum in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, app.stop)
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
