#!/usr/bin/env python3
"""Synthetic Wayland popup/dialog fixture, restricted to a marked Hyprveil lab.

Run with the lab environment (unset DISPLAY):
  python tests/fixtures/popup.py --lab-dir /tmp/hv-LAB --wayland-display wayland-1

The main app-id is org.hyprveil.fixture.protected. A magenta popup starts on
its right edge, extending outside the main rectangle when the output allows.
The optional transient requests app-id org.hyprveil.fixture.dialog. GTK may
associate it with the parent's app-id; its stable title identifies it in tests.

Send newline-delimited commands to LAB/popup-control.fifo, without keyboard or
pointer simulation: popup-show, popup-hide, dialog-show, dialog-hide,
parent-hide, parent-show, parent-close, status, quit. Parent lifecycle commands
keep an already mapped transient dialog alive; its native privacy flag is not
modified. For example: printf 'popup-hide\\n' > /tmp/hv-LAB/popup-control.fifo
Read LAB/popup-ready.json or stdout for current geometry and readiness.
This fixture neither invokes hyprctl nor creates rules or captures.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client import lab_environment


APP_ID = "org.hyprveil.fixture.protected"
DIALOG_ID = "org.hyprveil.fixture.dialog"
PRIVATE = (232, 64, 144)
COMMANDS = {"popup-show", "popup-hide", "dialog-show", "dialog-hide", "parent-hide",
            "parent-show", "parent-close", "status", "quit"}


def lab_path(lab_dir: Path, supplied: Path | None, default_name: str) -> Path:
    path = supplied if supplied is not None else lab_dir / default_name
    path = path.resolve()
    if not path.is_relative_to(lab_dir.resolve()):
        raise ValueError("fixture files must stay inside the explicit lab directory")
    if not path.parent.is_dir():
        raise ValueError("fixture file parent directory does not exist")
    return path


class ControlFIFO:
    """Own one new lab-local FIFO; never replace or delete somebody else's file."""

    def __init__(self, path: Path):
        self.path = path
        self.fd = None
        self.pending = b""
        os.mkfifo(path, 0o600)  # Existing files, symlinks and live FIFOs are rejected.
        self.inode = path.lstat().st_ino
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        except Exception:
            self.close()
            raise

    def read_commands(self) -> list[str]:
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return []
        self.pending += data
        if len(self.pending) > 16384:
            self.pending = b""
            return ["invalid-command-too-long"]
        lines = self.pending.split(b"\n")
        self.pending = lines.pop()
        return [line.decode("ascii", errors="replace").strip() for line in lines if line.strip()]

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            info = self.path.lstat()
            if info.st_ino == self.inode and stat.S_ISFIFO(info.st_mode):
                self.path.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lab-dir", required=True, type=Path)
    parser.add_argument("--wayland-display", required=True)
    parser.add_argument("--width", type=int, default=400)
    parser.add_argument("--height", type=int, default=300)
    parser.add_argument("--popup-width", type=int, default=220)
    parser.add_argument("--popup-height", type=int, default=160)
    parser.add_argument("--start-popup", choices=("show", "hide"), default="show")
    parser.add_argument("--start-dialog", choices=("show", "hide"), default="hide")
    parser.add_argument("--control-file", type=Path)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--quit-after", type=int, default=0)
    args = parser.parse_args()
    if min(args.width, args.height, args.popup_width, args.popup_height) < 32 or args.quit_after < 0:
        parser.error("dimensions must be at least 32 and quit-after must be nonnegative")
    try:
        lab_environment(args.lab_dir, args.wayland_display)
        lab_dir = args.lab_dir.resolve(strict=True)
        control_path = lab_path(lab_dir, args.control_file, "popup-control.fifo")
        ready_path = lab_path(lab_dir, args.ready_file, "popup-ready.json")
        control = ControlFIFO(control_path)
    except (OSError, ValueError, KeyError) as error:
        parser.error(f"refusing to run outside a fresh marked lab fixture: {error}")

    try:
        import gi
        gi.require_foreign("cairo")
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        gi.require_version("GdkWayland", "4.0")
        from gi.repository import Gdk, GdkWayland, Gio, GLib, Gtk

        GLib.set_prgname(APP_ID)
        GLib.set_application_name("Hyprveil synthetic popup fixture")

        class Fixture(Gtk.Application):
            def __init__(self):
                super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
                self.window = None
                self.body = None
                self.popup = None
                self.dialog = None
                self.main_drawn = False
                self.popup_drawn = False
                self.dialog_drawn = False
                self.popup_requested = args.start_popup == "show"
                self.dialog_requested = args.start_dialog == "show"
                self.anchor = None
                self.parent_destroyed = False

            def do_activate(self):
                display = Gdk.Display.get_default()
                if display is None or display.get_name() != args.wayland_display:
                    raise RuntimeError("GTK did not connect to the marked Wayland display")
                Gtk.Settings.get_for_display(display).set_property("gtk-enable-animations", False)
                provider = Gtk.CssProvider()
                provider.load_from_string(
                    ".hyprveil-private, .hyprveil-private > contents {"
                    "background: #e84090; border: 0; border-radius: 0;"
                    "box-shadow: none; padding: 0; margin: 0; }")
                Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                self.window = Gtk.ApplicationWindow(application=self)
                self.window.set_title("Hyprveil fixture: protected popup parent")
                self.window.set_decorated(False)
                self.window.set_default_size(args.width, args.height)
                self.window.add_css_class("hyprveil-private")
                self.window.connect("realize", lambda window: self.identify(window, APP_ID))
                self.body = self.drawing("main", args.width, args.height)
                self.window.set_child(self.body)
                self.popup = Gtk.Popover()
                self.popup.set_parent(self.body)
                self.popup.set_autohide(False)
                self.popup.set_has_arrow(False)
                self.popup.set_position(Gtk.PositionType.RIGHT)
                self.popup.set_offset(8, 0)
                self.popup.add_css_class("hyprveil-private")
                self.popup.set_child(self.drawing("popup", args.popup_width, args.popup_height))
                self.window.connect("map", lambda unused: GLib.idle_add(self.start_children))
                self.window.present()
                GLib.timeout_add(50, self.poll_commands)
                if args.quit_after:
                    GLib.timeout_add_seconds(args.quit_after, self.stop)

            def identify(self, window, app_id):
                surface = window.get_surface()
                if not isinstance(surface, GdkWayland.WaylandToplevel):
                    raise RuntimeError("fixture did not realize a native Wayland toplevel")
                surface.set_application_id(app_id)

            def drawing(self, kind, width, height):
                area = Gtk.DrawingArea()
                area.set_content_width(width)
                area.set_content_height(height)
                area.set_draw_func(lambda widget, context, w, h: self.paint(kind, context, w, h))
                return area

            def paint(self, kind, context, width, height):
                context.set_source_rgb(*(component / 255 for component in PRIVATE))
                context.paint()
                first = not getattr(self, kind + "_drawn")
                setattr(self, kind + "_drawn", True)
                if kind == "main":
                    anchor = (max(0, width - 2), max(0, height // 2 - 1))
                    if anchor != self.anchor:
                        self.anchor = anchor
                        rectangle = Gdk.Rectangle()
                        rectangle.x, rectangle.y = anchor
                        rectangle.width, rectangle.height = 1, 2
                        self.popup.set_pointing_to(rectangle)
                if first:
                    GLib.idle_add(self.report, kind + "-drawn")

            def start_children(self):
                if self.popup_requested:
                    self.popup.popup()
                if self.dialog_requested:
                    self.show_dialog()
                GLib.idle_add(self.report, "started")
                return GLib.SOURCE_REMOVE

            def show_dialog(self):
                if self.dialog is None:
                    self.dialog = Gtk.ApplicationWindow(application=self)
                    self.dialog.set_title("Hyprveil fixture: transient dialog")
                    self.dialog.set_decorated(False)
                    self.dialog.set_transient_for(self.window)
                    self.dialog.set_destroy_with_parent(False)
                    self.dialog.set_default_size(180, 120)
                    self.dialog.add_css_class("hyprveil-private")
                    self.dialog.connect("realize", lambda window: self.identify(window, DIALOG_ID))
                    self.dialog.set_child(self.drawing("dialog", 180, 120))
                    self.dialog.connect("close-request", self.dialog_close)
                self.dialog.present()

            def dialog_close(self, unused):
                self.dialog_requested = False
                self.dialog.set_visible(False)
                GLib.idle_add(self.report, "dialog-close")
                return True

            def surface_report(self, widget, popup=False):
                surface = widget.get_surface() if widget is not None else None
                if surface is None:
                    return {"mapped": False, "size": [0, 0]}
                report = {"mapped": surface.get_mapped(), "size": [surface.get_width(), surface.get_height()]}
                if popup and isinstance(surface, Gdk.Popup):
                    report["relative_at"] = [surface.get_position_x(), surface.get_position_y()]
                return report

            def report(self, event="status", error=None):
                report = {
                    "event": event, "pid": os.getpid(), "app_id": APP_ID,
                    "dialog_app_id": DIALOG_ID, "wayland_display": args.wayland_display,
                    "color": PRIVATE, "control_file": str(control_path),
                    "main": self.surface_report(self.window),
                    "popup": self.surface_report(self.popup, popup=True),
                    "dialog": self.surface_report(self.dialog),
                    "main_drawn": self.main_drawn, "popup_drawn": self.popup_drawn,
                    "dialog_drawn": self.dialog_drawn,
                    "parent_destroyed": self.parent_destroyed,
                    "dialog_transient_for_parent": self.dialog is not None and self.window is not None and
                    self.dialog.get_transient_for() == self.window,
                    "dialog_destroy_with_parent": self.dialog.get_destroy_with_parent() if self.dialog is not None else None,
                    "popup_requested": self.popup_requested, "dialog_requested": self.dialog_requested,
                }
                report["ready"] = self.main_drawn and (not self.popup_requested or
                    (self.popup_drawn and report["popup"]["mapped"])) and (not self.dialog_requested or
                    (self.dialog_drawn and report["dialog"]["mapped"]))
                if error:
                    report["error"] = error
                text = json.dumps(report, sort_keys=True) + "\n"
                temporary = ready_path.with_suffix(ready_path.suffix + f".{os.getpid()}.tmp")
                temporary.write_text(text)
                temporary.replace(ready_path)
                print(text, end="", flush=True)
                return GLib.SOURCE_REMOVE

            def poll_commands(self):
                for command in control.read_commands():
                    if command not in COMMANDS:
                        self.report("error", "unknown command: " + command)
                        continue
                    if command == "quit":
                        return self.stop()
                    if command in {"parent-hide", "parent-close"}:
                        if self.dialog is None or not self.dialog.get_mapped():
                            self.report("error", "parent lifecycle requires a mapped transient dialog")
                            continue
                        if self.window is None:
                            self.report("error", "parent has already been destroyed")
                            continue
                        self.popup_requested = False
                        self.popup.popdown()
                        if command == "parent-hide":
                            self.window.set_visible(False)
                        else:
                            self.window.destroy()
                            self.window = None
                            self.parent_destroyed = True
                        self.dialog.present()
                    elif command == "parent-show":
                        if self.window is None:
                            self.report("error", "a destroyed parent cannot be presented")
                            continue
                        self.window.present()
                    if command == "popup-show":
                        if self.window is None or not self.window.get_mapped():
                            self.report("error", "popup requires a mapped parent")
                            continue
                        self.popup_requested = True
                        self.popup.popup()
                    elif command == "popup-hide":
                        self.popup_requested = False
                        self.popup.popdown()
                    elif command == "dialog-show":
                        self.dialog_requested = True
                        self.show_dialog()
                    elif command == "dialog-hide":
                        self.dialog_requested = False
                        if self.dialog is not None:
                            self.dialog.set_visible(False)
                    GLib.idle_add(self.report, command)
                return GLib.SOURCE_CONTINUE

            def stop(self, *unused):
                self.quit()
                return GLib.SOURCE_REMOVE

        app = Fixture()
        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, app.stop)
        return app.run([])
    finally:
        control.close()


if __name__ == "__main__":
    sys.exit(main())
