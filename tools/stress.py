#!/usr/bin/env python3
"""Exercise the prototype only in a newly created, guarded synthetic lab.

The explicit parent socket is used solely to host nested Hyprland. All control,
fixtures, captures and plugin loading address that lab's marked private runtime.
No inherited desktop service, user configuration or main compositor is used.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time

import cairo

from lab import PROJECT, base_env, lab_env, load_lab, write_json

PRIVATE_CLASS = "org.hyprveil.fixture.protected"
GEOMETRY_FIELDS = ("address", "at", "size", "workspace", "monitor", "floating", "fullscreen")
PRIVATE_RGB = 0xE84090
BACKGROUND_RGB = 0x2484C4


def image_stats(path, roi=None):
    surface = cairo.ImageSurface.create_from_png(str(path))
    width, height = surface.get_width(), surface.get_height()
    if surface.get_format() not in (cairo.FORMAT_RGB24, cairo.FORMAT_ARGB32):
        raise RuntimeError("capture must be an RGB/RGBA PNG")
    data = bytes(surface.get_data())
    pixels = array.array("I", data)
    stride = surface.get_stride() // 4

    def count(color, rect):
        x, y, w, h = rect
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height:
            raise RuntimeError(f"invalid capture ROI {rect} in {width}x{height}")
        total = 0
        for row in range(y, y + h):
            values = pixels[row * stride + x:row * stride + x + w]
            total += values.count(color) + values.count(color | 0xFF000000)
        return total / (w * h)

    result = {"width": width, "height": height,
              "private_fraction": count(PRIVATE_RGB, (0, 0, width, height))}
    if roi:
        result.update(roi=list(roi), private_roi_fraction=count(PRIVATE_RGB, roi),
                      background_roi_fraction=count(BACKGROUND_RGB, roi),
                      black_roi_fraction=count(0, roi))
    return result


class Stress:
    def __init__(self, args):
        self.args = args
        self.launcher = None
        self.runtime = self.state = self.env = self.artifacts = None
        self.plugin = None
        self.loaded = False
        self.fixtures = []
        self.logs = []
        self.checks = []
        self.iterations = []
        self.resource_samples = []
        self.started_at = time.monotonic()
        self.report = {"ok": False, "checks": self.checks, "iterations": self.iterations,
                       "capture_tool": "grim", "pixel_matching": "exact opaque synthetic RGB",
                       "quality_observations": [],
                       "resource_samples": self.resource_samples,
                       "resource_scope": "marked lab native compositor RSS and open FDs; observations without a pass threshold",
                       "concurrency_evidence": "overlapping client processes, not protocol-session overlap"}

    def start(self):
        launch = [sys.executable, str(getattr(self.args, "lab_entry", PROJECT / "tools/lab.py")),
                  "run", "--parent-runtime", self.args.parent_runtime, "--parent-display", self.args.parent_display]
        if getattr(self.args, "standard_plugin_admission", False):
            launch.append("--standard-plugin-admission")
        self.launcher = subprocess.Popen(
            launch, env=base_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        selector = selectors.DefaultSelector()
        selector.register(self.launcher.stdout, selectors.EVENT_READ)
        if not selector.select(30):
            raise RuntimeError("timed out starting isolated lab")
        ready = json.loads(self.launcher.stdout.readline())
        selector.close()
        if not ready.get("ready"):
            raise RuntimeError(f"lab did not become ready: {ready}")
        self.runtime, self.state = load_lab(ready["runtime_dir"])
        self.env = lab_env(self.runtime, self.state)
        self.artifacts = Path(self.state["artifacts"]) / getattr(self.args, "suite", "stress")
        self.artifacts.mkdir()
        self.plugin = self.artifacts / "hyprveil.so"
        shutil.copy2(getattr(self.args, "plugin", PROJECT / "build/hyprveil.so"), self.plugin)
        self.plugin.chmod(0o500)
        self.report.update(lab=self.state, artifacts=str(self.artifacts),
                           plugin_sha256=hashlib.sha256(self.plugin.read_bytes()).hexdigest())
        self.sample_resources("lab-ready-before-fixtures")
        print(json.dumps({"event": "lab-ready", "runtime": str(self.runtime), "artifacts": str(self.artifacts)}), flush=True)

    def command(self, argv, timeout=15):
        # Check the marker and actual owning process before every control call.
        load_lab(self.runtime)
        result = subprocess.run(argv, env=self.env, text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"{argv[0]} exit {result.returncode}: {result.stderr.strip()} {result.stdout.strip()}")
        return result.stdout.strip()

    def sample_resources(self, phase):
        sample = {"phase": phase, "elapsed_seconds": round(time.monotonic() - self.started_at, 3)}
        try:
            # Never inspect an inherited compositor. Attest the private marker
            # and owning process immediately before reading its proc entries.
            _, owner = load_lab(self.runtime)
            pid = int(owner["pid"])
            status = {}
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                key, separator, value = line.partition(":")
                if separator and key in {"VmRSS", "VmHWM", "RssAnon", "RssFile", "VmSize", "Threads"}:
                    status[key] = int(value.split()[0])
            sample.update(pid=pid, rss_kib=status.get("VmRSS"), peak_rss_kib=status.get("VmHWM"),
                          anonymous_rss_kib=status.get("RssAnon"), file_rss_kib=status.get("RssFile"),
                          virtual_size_kib=status.get("VmSize"), threads=status.get("Threads"),
                          open_fds=sum(1 for _ in Path(f"/proc/{pid}/fd").iterdir()))
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            sample["error"] = str(error)
        self.resource_samples.append(sample)
        return sample

    def ctl(self, *argv):
        output = self.command(["hyprctl", "-i", self.state["signature"], *argv])
        if output.startswith(("err", "Invalid", "unknown", "Couldn")):
            raise RuntimeError(output)
        return output

    def dispatch(self, expression):
        return self.ctl("eval", "hl.dispatch(" + expression + ")")

    def clients(self):
        return json.loads(self.ctl("-j", "clients"))

    def geometry(self):
        return {client["class"]: {field: client[field] for field in GEOMETRY_FIELDS}
                for client in self.clients() if client["class"].startswith("org.hyprveil.fixture.")}

    def protected(self):
        matches = [client for client in self.clients() if client["class"] == PRIVATE_CLASS]
        if len(matches) != 1:
            raise RuntimeError("expected exactly one synthetic protected window")
        return matches[0]

    def address(self):
        return "address:" + self.protected()["address"]

    def privacy(self):
        return self.ctl("getprop", self.address(), "no_screen_share") == "true"

    def record(self, name, ok, **details):
        value = {"name": name, "ok": bool(ok), **details}
        self.checks.append(value)
        self.save()
        print(json.dumps({"event": "check", **value}), flush=True)
        return value

    def save(self):
        if self.artifacts:
            write_json(self.artifacts / "report.json", self.report)

    def fixture(self, role):
        log = (self.artifacts / (role + ".log")).open("w")
        self.logs.append(log)
        client = subprocess.Popen([sys.executable, str(PROJECT / "tests/fixtures/client.py"),
                                   "--lab-dir", str(self.runtime), "--wayland-display", self.state["wayland_display"],
                                   "--role", role, "--ready-file", str(self.runtime / (role + ".json"))],
                                  env=self.env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        self.fixtures.append(client)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if client.poll() is not None:
                raise RuntimeError(f"{role} fixture exited {client.returncode}")
            matches = [window for window in self.clients() if window["class"] == "org.hyprveil.fixture." + role]
            if len(matches) == 1 and min(matches[0]["size"]) > 0:
                time.sleep(0.3)
                return
            time.sleep(0.1)
        raise RuntimeError(f"{role} fixture did not map")

    def monitor(self):
        monitors = json.loads(self.ctl("-j", "monitors"))
        return next(monitor for monitor in monitors if monitor["name"] == "HV-TEST")

    def roi(self):
        window = self.protected()
        monitor = self.monitor()
        scale = monitor["scale"]
        x = round((window["at"][0] - monitor["x"]) * scale) + 16
        y = round((window["at"][1] - monitor["y"]) * scale) + 16
        w = round(window["size"][0] * scale) - 32
        h = round(window["size"][1] * scale) - 32
        w, h = min(w, monitor["width"] - x - 1), min(h, monitor["height"] - y - 1)
        if window["fullscreen"]:
            # Disposable nested sessions have startup notices in the upper
            # right. Use the lower half to prove the local fullscreen surface.
            y += h // 2
            h -= h // 2
        return x, y, w, h

    def capture(self, path):
        self.command(["grim", "-o", "HV-TEST", str(path)])

    def scene(self, name, expected=None, reveal=True, local=True, native_black=False):
        self.dispatch('hl.dsp.cursor.move({x=4,y=4})')
        time.sleep(0.2)
        before = self.geometry()
        protected = self.protected()
        geometry_expected = not expected or all(protected[key] == value for key, value in expected.items())
        private = self.privacy()
        roi = self.roi()
        if local:
            self.ctl("hyprveil", "dump-local")
        image = self.artifacts / (name + ".png")
        self.capture(image)
        pixels = image_stats(image, roi)
        after = self.geometry()
        local_stats = None
        if local:
            original = self.artifacts / (name + "-local.png")
            shutil.copy2(self.runtime / "hyprveil-local.png", original)
            local_stats = image_stats(original, roi)
        ok = private and geometry_expected and before == after and pixels["private_fraction"] == 0
        if reveal:
            ok = ok and pixels["background_roi_fraction"] >= 0.99
        if local:
            ok = ok and local_stats["private_roi_fraction"] >= 0.99
        if native_black:
            ok = ok and pixels["black_roi_fraction"] >= 0.99
        return self.record(name, ok, pixels=pixels, local=local_stats, privacy_retained=private,
                           expected_geometry=geometry_expected, capture_geometry_unchanged=before == after,
                           geometry=before, reveal_background_required=reveal,
                           native_black_required=native_black)

    def transform(self, x, y, width, height):
        address = json.dumps(self.address())
        self.dispatch(f"hl.dsp.window.resize({{window={address},x={width},y={height},relative=false}})")
        self.dispatch(f"hl.dsp.window.move({{window={address},x={x},y={y},relative=false}})")
        time.sleep(0.3)
        return {"at": [x, y], "size": [width, height]}

    def load(self):
        result = self.ctl("plugin", "load", str(self.plugin))
        if result != "ok":
            raise RuntimeError(f"plugin load: {result}")
        self.loaded = True
        self.ctl("hyprveil", "omit")
        time.sleep(0.2)
        self.sample_resources("plugin-loaded")

    def unload(self):
        result = self.ctl("plugin", "unload", str(self.plugin))
        if result != "ok":
            raise RuntimeError(f"plugin unload: {result}")
        self.loaded = False
        time.sleep(0.2)
        self.sample_resources("plugin-unloaded")

    def tests(self):
        monitors = json.loads(self.ctl("-j", "monitors"))
        if not any(monitor["name"] == "HV-TEST" for monitor in monitors):
            self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        # Remove only this disposable lab's fixed protected geometry rule. The
        # persistent tag privacy rule remains active across all reloads.
        config = self.runtime / "hyprland.lua"
        fixed = ('hl.window_rule({ match = { class = "^org[.]hyprveil[.]fixture[.]protected$" },\n'
                 '  size = { 320, 240 }, move = { 256, 192 } })\n')
        contents = config.read_text()
        if contents.count(fixed) != 1:
            raise RuntimeError("expected disposable protected geometry rule")
        config.write_text(contents.replace(fixed, ""))
        self.ctl("reload")
        if self.ctl("configerrors"):
            raise RuntimeError("isolated lab configuration contains errors")
        self.fixture("background")
        self.fixture("protected")
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        expected = self.transform(256, 192, 320, 240)
        self.load()
        self.report["compositor"] = json.loads(self.ctl("-j", "version"))
        self.scene("baseline", expected)
        for index, geometry in enumerate(((64, 80, 400, 280), (576, 360, 320, 240), (120, 120, 640, 400))):
            expected = self.transform(*geometry)
            self.scene("move-resize-" + str(index + 1), expected)

        self.dispatch('hl.dsp.window.fullscreen({window=' + json.dumps(self.address()) +
                      ',action="set",mode="fullscreen",layout_aware=false})')
        time.sleep(0.3)
        fullscreen = self.scene("fullscreen-privacy", {"fullscreen": 2}, reveal=False)
        self.report["quality_observations"].append({
            "name": "fullscreen underlay reconstruction",
            "underlay_reconstructed": fullscreen["pixels"]["background_roi_fraction"] >= 0.99,
            "accepted_behavior": "compositor background may replace fullscreen private window",
            "required_for_privacy": False})
        self.dispatch('hl.dsp.window.fullscreen({window=' + json.dumps(self.address()) +
                      ',action="unset",mode="fullscreen",layout_aware=false})')
        expected = self.transform(256, 192, 320, 240)
        self.scene("fullscreen-restored", expected)

        for index in range(3):
            self.ctl("reload")
            # Runtime mode changes are deliberately not persistence. An empty
            # native config reload returns to black; verify it before selecting
            # omission again for the underlay/geometry stress assertions.
            self.scene("config-reload-black-" + str(index + 1), expected, reveal=False, native_black=True)
            self.ctl("hyprveil", "omit")
            self.scene("config-reload-" + str(index + 1), expected)
            self.sample_resources("config-reload-" + str(index + 1))
        for index in range(3):
            self.unload()
            self.scene("unloaded-native-black-" + str(index + 1), expected, reveal=False, local=False, native_black=True)
            self.load()
            self.scene("plugin-reloaded-" + str(index + 1), expected)

        # Both physical dimensions must divide evenly by 1.25. Hyprland
        # otherwise rounds the requested scale (1024x768 became 1.3333334).
        # Only our disposable headless output changes resolution.
        config.write_text(config.read_text() + '\nhl.monitor({output="HV-TEST",mode="1280x800@60",position="0x0",scale=1.25})\n')
        self.ctl("reload")
        self.record("fractional reload restores native black", json.loads(self.ctl("hyprveil", "status"))["mode"] == "black")
        self.ctl("hyprveil", "omit")
        time.sleep(0.4)
        scale = self.monitor()["scale"]
        self.record("fractional-scale-applied", scale == 1.25, scale=scale,
                    config_errors=self.ctl("configerrors"))
        expected = self.transform(120, 96, 400, 280)
        self.scene("fractional-scale", expected)
        config.write_text(config.read_text() + '\nhl.monitor({output="HV-TEST",mode="1024x768@60",position="0x0",scale=1})\n')
        self.ctl("reload")
        self.record("scale restoration reload restores native black", json.loads(self.ctl("hyprveil", "status"))["mode"] == "black")
        self.ctl("hyprveil", "omit")
        expected = self.transform(256, 192, 320, 240)
        self.scene("scale-restored", expected)
        self.sample_resources("after-load-reload-scale-cycles")

        start = time.monotonic()
        next_move = start + 5
        next_progress = start + 10
        failures = 0
        simultaneous_pairs = 0
        while time.monotonic() - start < self.args.duration:
            now = time.monotonic()
            if now >= next_move:
                index = int((now - start) // 5) % 3
                expected = self.transform(*((256, 192, 320, 240), (64, 64, 400, 280), (576, 360, 320, 240))[index])
                next_move += 5
            self.dispatch('hl.dsp.cursor.move({x=4,y=4})')
            before = self.geometry()
            paths = [self.runtime / ("stress-consumer-" + str(index) + ".png") for index in range(2)]
            load_lab(self.runtime)
            consumers = [subprocess.Popen(["grim", "-o", "HV-TEST", str(path)], env=self.env,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                         for path in paths]
            overlapping = all(consumer.poll() is None for consumer in consumers)
            simultaneous_pairs += int(overlapping)
            try:
                for consumer in consumers:
                    stdout, stderr = consumer.communicate(timeout=15)
                    if consumer.returncode:
                        raise RuntimeError(f"parallel capture failed: {stderr.decode(errors='replace')}")
            finally:
                for consumer in consumers:
                    if consumer.poll() is None:
                        consumer.kill()
                        consumer.wait()
            stats = [image_stats(path, self.roi()) for path in paths]
            after = self.geometry()
            window = self.protected()
            expected_match = all(window[key] == value for key, value in expected.items())
            ok = before == after and expected_match and self.privacy() and all(
                image["private_fraction"] == 0 and image["background_roi_fraction"] >= 0.99 for image in stats)
            sample = {"index": len(self.iterations), "elapsed": round(time.monotonic() - start, 3), "ok": ok,
                      "overlapping_consumers": overlapping, "expected_geometry": expected_match,
                      "capture_geometry_unchanged": before == after,
                      "private_fractions": [image["private_fraction"] for image in stats],
                      "underlay_fractions": [image["background_roi_fraction"] for image in stats]}
            self.iterations.append(sample)
            if not ok or len(self.iterations) == 1:
                for index, path in enumerate(paths):
                    shutil.copy2(path, self.artifacts / (f"duration-{sample['index']}-consumer-{index}.png"))
            if not ok:
                failures += 1
                print(json.dumps({"event": "stress-failure", **sample}), flush=True)
            if now >= next_progress:
                self.sample_resources("duration-" + str(round(now - start)) + "-seconds")
                self.save()
                print(json.dumps({"event": "stress-progress", "elapsed": sample["elapsed"],
                                  "pairs": len(self.iterations), "failures": failures}), flush=True)
                next_progress += 10
            time.sleep(0.08)
        for index, path in enumerate(paths):
            shutil.copy2(path, self.artifacts / (f"duration-last-consumer-{index}.png"))
        self.record("repeated-two-consumer-capture", failures == 0 and simultaneous_pairs > 0,
                    duration_seconds=round(time.monotonic() - start, 3), capture_pairs=len(self.iterations),
                    captures=len(self.iterations) * 2, overlapping_pairs=simultaneous_pairs, failures=failures)
        self.report["plugin_status"] = json.loads(self.ctl("hyprveil", "status"))
        self.scene("duration-final", expected)
        self.sample_resources("duration-completed")

    def cleanup(self):
        errors = []
        if self.loaded:
            try:
                self.unload()
            except Exception as error:
                errors.append(str(error))
        for client in self.fixtures:
            if client.poll() is None:
                client.terminate()
            try:
                client.wait(timeout=3)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait()
        for log in self.logs:
            log.close()
        if self.launcher and self.launcher.poll() is None:
            # The launcher owns exactly the child created above; its SIGTERM
            # handler stops that child and escalates to kill after five seconds.
            self.launcher.terminate()
            try:
                self.launcher.wait(timeout=10)
            except subprocess.TimeoutExpired:
                errors.append("lab launcher did not stop within ten seconds")
        child_stopped = self.state is None or not Path(f"/proc/{self.state['pid']}").exists()
        self.report.update(lab_stopped=child_stopped and self.launcher is not None and self.launcher.poll() is not None,
                           cleanup_errors=errors)
        if errors or not self.report["lab_stopped"]:
            self.report["ok"] = False
        if self.launcher and self.launcher.poll() is not None:
            self.launcher.stdout.close()
            self.launcher.stderr.close()
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    args = parser.parse_args()
    if not 1 <= args.duration <= 300:
        parser.error("duration must be between one and 300 seconds")
    stress = Stress(args)
    def interrupted(signum, frame):
        raise KeyboardInterrupt("safe stress run interrupted")
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        stress.start()
        stress.tests()
        stress.report["ok"] = bool(stress.checks) and all(check["ok"] for check in stress.checks)
    except (Exception, KeyboardInterrupt) as error:
        stress.report["error"] = str(error)
        print(json.dumps({"event": "error", "error": str(error)}), flush=True)
    finally:
        stress.cleanup()
    print(json.dumps({"ok": stress.report["ok"], "report": str(stress.artifacts / "report.json") if stress.artifacts else None,
                      "lab_stopped": stress.report["lab_stopped"]}), flush=True)
    return 0 if stress.report["ok"] and stress.report["lab_stopped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
