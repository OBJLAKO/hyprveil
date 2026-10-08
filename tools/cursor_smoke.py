#!/usr/bin/env python3
"""Check requested embedded cursors in a synthetic software-cursor lab."""
from __future__ import annotations

import argparse
import array
import json
from pathlib import Path
import signal
import time

import cairo

from lab import PROJECT
from stress import Stress, BACKGROUND_RGB, image_stats


class CursorSmoke(Stress):
    def __init__(self, args):
        args.suite = "cursor"
        super().__init__(args)

    def cursor_pixels(self, path, origin=(128, 128)):
        image = cairo.ImageSurface.create_from_png(str(path))
        pixels = array.array("I", bytes(image.get_data()))
        stride = image.get_stride() // 4
        # Cursor origin (128,128); include its hotspot margin and full theme
        # image. All surrounding source pixels are solid public blue.
        count = sum((pixels[y * stride + x] & 0xFFFFFF) != BACKGROUND_RGB
                    for y in range(origin[1] - 12, origin[1] + 52)
                    for x in range(origin[0] - 12, origin[0] + 52))
        return count

    def capture_cursor(self, name, requested):
        path = self.artifacts / (name + ".png")
        argv = ["grim", "-o", "HV-TEST"]
        if requested:
            argv.append("-c")
        self.command([*argv, str(path)])
        return path

    def tests(self):
        self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        self.fixture("background")
        self.fixture("protected")
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        self.ctl("dismissnotify", "-1")
        self.dispatch('hl.dsp.cursor.move({x=128,y=128})')
        time.sleep(0.3)
        before = self.geometry()
        native = self.capture_cursor("native-black-with-cursor", True)
        native_count = self.cursor_pixels(native)
        self.record("native software cursor is visible in requested export", native_count > 0,
                    cursor_pixels=native_count)
        if not native_count:
            raise RuntimeError("lab has no visible cursor texture; comparison is inconclusive")

        self.load()
        self.ctl("dismissnotify", "-1")
        self.dispatch('hl.dsp.cursor.move({x=128,y=128})')
        time.sleep(0.3)
        without = self.capture_cursor("omit-without-cursor", False)
        with_cursor = self.capture_cursor("omit-with-cursor", True)
        without_count = self.cursor_pixels(without)
        with_count = self.cursor_pixels(with_cursor)
        self.record("omission respects a cursor-free capture request", without_count == 0,
                    cursor_pixels=without_count)
        self.record("omission includes a requested software cursor", with_count > 0,
                    cursor_pixels=with_count, native_cursor_pixels=native_count)
        self.record("cursor capture preserves privacy and window geometry",
                    image_stats(without)["private_fraction"] == 0 and
                    image_stats(with_cursor)["private_fraction"] == 0 and before == self.geometry())
        cropped = self.artifacts / "omit-cropped-with-cursor.png"
        self.command(["grim", "-g", "80,80 256x256", "-c", str(cropped)])
        cropped_count = self.cursor_pixels(cropped, (48, 48))
        self.record("cropped capture places its requested cursor correctly",
                    cropped_count > 0 and cropped_count == with_count and
                    image_stats(cropped)["private_fraction"] == 0,
                    cursor_pixels=cropped_count, expected_cursor_origin=[48, 48])
        self.dispatch('hl.dsp.cursor.move({x=320,y=256})')
        time.sleep(0.2)
        over_private = self.capture_cursor("omit-cursor-over-private-window", True)
        private_cursor_count = self.cursor_pixels(over_private, (320, 256))
        self.record("cursor remains visible over an omitted private window",
                    private_cursor_count > 0 and image_stats(over_private)["private_fraction"] == 0 and
                    before == self.geometry(), cursor_pixels=private_cursor_count)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    args = parser.parse_args()
    run = CursorSmoke(args)

    def interrupted(signum, frame):
        raise KeyboardInterrupt("cursor smoke interrupted")

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        run.start()
        run.tests()
        run.report["ok"] = bool(run.checks) and all(check["ok"] for check in run.checks)
    except (Exception, KeyboardInterrupt) as error:
        run.report["error"] = str(error)
        print(json.dumps({"event": "error", "error": str(error)}), flush=True)
    finally:
        run.cleanup()
    print(json.dumps({"ok": run.report["ok"], "report": str(run.artifacts / "report.json"),
                      "lab_stopped": run.report["lab_stopped"]}), flush=True)
    return 0 if run.report["ok"] and run.report["lab_stopped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
