#!/usr/bin/env python3
"""Real session-lock and unsupported-output capture tests in an owned lab."""
from __future__ import annotations

import argparse
import array
import json
from pathlib import Path
import signal
import subprocess
import time

import cairo

from lab import PROJECT, load_lab
from stress import Stress, image_stats


def solid_fraction(path, rgb):
    image = cairo.ImageSurface.create_from_png(str(path))
    values = array.array("I", bytes(image.get_data()))
    stride = image.get_stride() // 4
    count = sum((values[y * stride + x] & 0xFFFFFF) == rgb
                for y in range(image.get_height()) for x in range(image.get_width()))
    return count / (image.get_width() * image.get_height())


def opaque_fraction(path):
    image = cairo.ImageSurface.create_from_png(str(path))
    if image.get_format() == cairo.FORMAT_RGB24:
        return 1.0  # RGB PNG carries no transparency channel.
    if image.get_format() != cairo.FORMAT_ARGB32:
        raise RuntimeError("unexpected capture pixel format")
    values = array.array("I", bytes(image.get_data()))
    stride = image.get_stride() // 4
    return sum(values[y * stride + x] >> 24 == 255
               for y in range(image.get_height()) for x in range(image.get_width())) / (image.get_width() * image.get_height())


class LockSmoke(Stress):
    def __init__(self, args):
        args.suite = "lock"
        super().__init__(args)
        self.lock_client = None

    def wait(self, predicate, description, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.lock_client and self.lock_client.poll() is not None:
                raise RuntimeError("lock client exited: " + (self.artifacts / "lock-client.log").read_text())
            result = predicate()
            if result:
                return result
            time.sleep(0.03)
        raise RuntimeError("timeout waiting for " + description)

    def lock_state(self):
        path = self.runtime / "lock-state.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def control(self, name, predicate):
        before = (self.runtime / "lock-state.json").stat().st_mtime_ns
        self.lock_client.stdin.write(name + "\n")
        self.lock_client.stdin.flush()
        return self.wait(lambda: (value if (self.runtime / "lock-state.json").stat().st_mtime_ns > before and
                                 predicate(value := self.lock_state()) else None), name)

    def capture_black(self, name, argv=None):
        path = self.artifacts / (name + ".png")
        self.command(["grim", *(argv or ["-o", "HV-TEST"]), str(path)])
        return self.record(name, solid_fraction(path, 0) == 1 and opaque_fraction(path) == 1 and image_stats(path)["private_fraction"] == 0,
                           full_frame_black_fraction=solid_fraction(path, 0),
                           full_frame_opaque_fraction=opaque_fraction(path),
                           private_fraction=image_stats(path)["private_fraction"])

    def compile_client(self):
        xml = "/usr/share/wayland-protocols/staging/ext-session-lock/ext-session-lock-v1.xml"
        header = self.artifacts / "ext-session-lock-client.h"
        source = self.artifacts / "ext-session-lock-protocol.c"
        object_file = self.artifacts / "ext-session-lock-protocol.o"
        binary = self.artifacts / "lock-client"
        # Compile-only commands do not address any display; use our lab env.
        self.command(["wayland-scanner", "client-header", xml, str(header)])
        self.command(["wayland-scanner", "private-code", xml, str(source)])
        self.command(["cc", "-c", str(source), "-o", str(object_file)])
        self.command(["c++", "-std=c++23", "-Wall", "-Wextra", "-I", str(self.artifacts),
                      str(PROJECT / "tests/fixtures/lock_client.cpp"), str(object_file),
                      "-lwayland-client", "-o", str(binary)])
        return binary

    def tests(self):
        self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        self.fixture("background")
        self.fixture("protected")
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        self.load()
        self.ctl("hyprveil", "omit")
        self.ctl("dismissnotify", "-1")
        before = self.geometry()
        self.scene("pre-lock-omission", reveal=True)
        binary = self.compile_client()
        load_lab(self.runtime)
        log = (self.artifacts / "lock-client.log").open("w")
        self.logs.append(log)
        self.lock_client = subprocess.Popen([str(binary), str(self.runtime), self.state_display(), str(self.runtime / "lock-state.json")],
                                            env=self.env, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True)
        self.fixtures.append(self.lock_client)
        self.wait(lambda: (self.runtime / "lock-state.json").exists(), "lock fixture ready")
        self.ctl("hyprveil", "spoiler")
        started = time.monotonic()
        self.control("lock", lambda value: value.get("lock_requested"))
        self.capture_black("lock-before-surface-is-ready")
        self.record("capture does not acknowledge an absent lock surface",
                    not self.lock_state()["locked"] and time.monotonic() - started < 4,
                    fixture_state=self.lock_state(), elapsed=time.monotonic() - started)
        self.control("show", lambda value: value.get("mapped"))
        self.wait(lambda: self.lock_state().get("locked"), "physical lock ACK")
        self.ctl("hyprveil", "dump-local")
        self.capture_black("locked-monitor-all-black")
        local = self.runtime / "hyprveil-local.png"
        local_fraction = solid_fraction(local, 0x28D8B8)
        self.record("real cyan lock surface remains local", local_fraction >= 0.99,
                    local_cyan_fraction=local_fraction, fixture_state=self.lock_state())
        self.capture_black("locked-crop-all-black", ["-g", "80,80 256x256"])
        # Hyprland v0.56.2 serializes stableId and advertises its same hex value
        # through ext_foreign_toplevel_handle_v1.identifier.
        toplevel = self.protected().get("stableId")
        self.report["foreign_identifier"] = toplevel
        if not toplevel:
            raise RuntimeError("missing foreign identifier for direct window capture")
        self.capture_black("locked-direct-window-all-black", ["-T", toplevel])
        background = next(client for client in self.clients() if client["class"] == "org.hyprveil.fixture.background")
        if self.ctl("getprop", "address:" + background["address"], "no_screen_share") != "false":
            raise RuntimeError("expected a public window for locked direct capture")
        self.capture_black("locked-direct-public-window-all-black", ["-T", background["stableId"]])
        self.control("unlock", lambda value: not value.get("lock_requested"))
        self.ctl("hyprveil", "omit")
        self.scene("unlock-restores-omission", reveal=True)
        self.record("lock and capture preserve all fixture geometry", before == self.geometry())
        self.lock_client.stdin.write("quit\n")
        self.lock_client.stdin.flush()
        self.lock_client.wait(timeout=3)

        # Rotated output uses full-frame denial rather than native mirror masks.
        config = self.runtime / "hyprland.lua"
        config.write_text(config.read_text() + '\nhl.monitor({output="HV-TEST",mode="1024x768@60",position="0x0",scale=1,transform=1})\n')
        self.ctl("reload")
        time.sleep(0.25)
        self.record("rotated output is actually applied", self.monitor()["transform"] == 1,
                    transform=self.monitor()["transform"], config_errors=self.ctl("configerrors"))
        self.capture_black("unsupported-rotated-output-all-black")
        config.write_text(config.read_text() + '\nhl.monitor({output="HV-TEST",mode="1024x768@60",position="0x0",scale=1,transform=0})\n')
        self.ctl("reload")
        time.sleep(0.25)
        # Runtime style changes reset to file/default on a standard Lua reload.
        # This scene explicitly tests omission after rotation, not the default.
        self.ctl("hyprveil", "omit")
        self.scene("rotation-restored-omission", reveal=True)
        self.report["plugin_status"] = json.loads(self.ctl("hyprveil", "status"))

    def state_display(self):
        return self.env["WAYLAND_DISPLAY"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    args = parser.parse_args()
    run = LockSmoke(args)
    def interrupted(signum, frame):
        raise KeyboardInterrupt("lock smoke interrupted")
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
        # If a lock client dies/never acknowledges, the owned lab is still
        # stopped by Stress.cleanup; the parent session is never unlocked.
        run.cleanup()
    print(json.dumps({"ok": run.report["ok"], "report": str(run.artifacts / "report.json") if run.artifacts else None,
                      "lab_stopped": run.report["lab_stopped"]}), flush=True)
    return 0 if run.report["ok"] and run.report["lab_stopped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
