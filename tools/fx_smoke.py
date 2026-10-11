#!/usr/bin/env python3
"""Exercise real omarchy-fx transformers in a synthetic, guarded compositor."""
from __future__ import annotations
import argparse
import array
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import time

import cairo
from lab import PROJECT
from stress import Stress, image_stats
import sys
sys.path.insert(0, str(PROJECT / "tests"))
from verify_capture import Image, absence_report, color_fraction, opacity_fraction

PRIVATE = (232, 64, 144)
PUBLIC = (242, 207, 82)
MASK = (26, 191, 102)


def color_shape(path, color, roi, tolerance=5):
    """Read only the synthetic fixture ROI, retaining its physical coordinates."""
    surface = cairo.ImageSurface.create_from_png(str(path))
    pixels = array.array("I", bytes(surface.get_data()))
    stride = surface.get_stride() // 4
    x, y, width, height = roi
    if min(x, y) < 0 or min(width, height) <= 0 or x + width > surface.get_width() or y + height > surface.get_height():
        raise RuntimeError("FX comparison ROI extends outside the child capture")
    red, green, blue = color
    return {row * stride + column for row in range(y, y + height) for column in range(x, x + width)
            if abs(((value := pixels[row * stride + column]) >> 16 & 255) - red) <= tolerance and
            abs((value >> 8 & 255) - green) <= tolerance and abs((value & 255) - blue) <= tolerance}


def shape_metrics(reference, actual, plain):
    intersection = len(reference & actual)
    union = len(reference | actual)
    outside = reference - plain
    return {"reference_pixels": len(reference), "actual_pixels": len(actual),
            "intersection_over_union": intersection / union if union else 0.0,
            "reference_outside_plain": len(outside), "actual_outside_plain": len(actual - plain),
            "outside_inherited_fraction": len(outside & actual) / len(outside) if outside else None}


def crop_png(source, target, rect):
    image = cairo.ImageSurface.create_from_png(str(source))
    x, y, width, height = rect
    if min(x, y) < 0 or min(width, height) <= 0 or x + width > image.get_width() or y + height > image.get_height():
        raise RuntimeError("FX reference crop extends outside the local synthetic mirror")
    crop = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    context = cairo.Context(crop)
    context.set_source_surface(image, -x, -y)
    context.paint()
    crop.write_to_png(str(target))


class FxSmoke(Stress):
    def __init__(self, args):
        args.suite = "fx"
        super().__init__(args)
        self.report["pixel_matching"] = "whole-frame secret-color absence with RGB tolerance 8; exact local synthetic RGB"
        self.report["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def monitor(self):
        return next(monitor for monitor in json.loads(self.ctl("-j", "monitors"))
                    if monitor["name"].startswith("WAYLAND-") and monitor["width"] > 0)

    def capture(self, path):
        # Aquamarine's synthetic headless backend can advertise a 0x0 output.
        # Use the child compositor's usable nested output, never its parent.
        self.command(["grim", "-o", self.monitor()["name"], str(path)])

    def fixture_shape(self, role, margin=96, dimensions=None):
        window = next(window for window in self.clients() if window["class"] == "org.hyprveil.fixture." + role)
        monitor = self.monitor()
        scale = monitor["scale"]
        x, y = (round((window["at"][index] - monitor[("x", "y")[index]]) * scale) for index in range(2))
        width, height = (round(value * scale) for value in window["size"])
        frame_width, frame_height = dimensions or (monitor["width"], monitor["height"])
        stride = frame_width
        plain = {row * stride + column for row in range(max(0, y), min(frame_height, y + height))
                 for column in range(max(0, x), min(frame_width, x + width))}
        left, top = max(0, x - margin), max(0, y - margin)
        right, bottom = min(frame_width, x + width + margin), min(frame_height, y + height + margin)
        return (left, top, right - left, bottom - top), plain

    def visual_inheritance(self, config_home, before, mask):
        # A long, undamped pulse makes the actual reviewed effect observable
        # throughout each bounded burst. Its state is advanced only by normal
        # child render preChecks; the test never pokes OmaFX private memory.
        (config_home / "omarchy-fx.conf").write_text(
            "wobbly_enabled=true\nelastic_enabled=true\npulse_enabled=true\n"
            "pulse_on_switch=true\npulse_amount=40\npulse_period=1800\npulse_damping=0\n"
            "shake_enabled=false\n")
        self.ctl("reload")  # This reload addresses only the marked child.
        shader = self.runtime / "fx-solid.frag"
        shader.write_text("vec4 hyprveil(vec2 uv) { return vec4(26.0/255.0,191.0/255.0,102.0/255.0,1.0); }\n")
        for mode, target in [(mode, "protected") for mode in ("black", "color", "image", "spoiler")] + [("color", "foreground")]:
            values = {"mode": mode, "mask_color": "#1abf66", "eye": False, "speed": 0,
                      "shader_path": str(shader) if mode == "spoiler" else "", "image_path": str(mask)}
            self.ctl("repl", "local v,e=hl.plugin.hyprveil.configure(" +
                     "{" + ",".join(key + "=" + (str(value).lower() if isinstance(value, bool) else
                        str(value) if isinstance(value, int) else json.dumps(value)) for key, value in values.items()) +
                     "}); if not v then error(e) end; return true")
            self.focus("background")
            self.focus(target)
            time.sleep(.32)
            observations = []
            for frame in range(3):
                name = "inherit-" + mode + "-" + target + "-" + str(frame)
                observed = json.loads(self.ctl("hyprveil-fx-probe"))
                if observed["private_transformers" if target == "protected" else "public_transformers"] <= 0:
                    raise RuntimeError("FX visual comparison has no active real transformer")
                self.ctl("hyprveil", "dump-local")
                path, local = self.artifacts / (name + ".png"), self.artifacts / (name + "-local.png")
                self.capture(path)
                shutil.copyfile(self.runtime / "hyprveil-local.png", local)
                image = Image(path)
                local_image = Image(local)
                if (local_image.width, local_image.height) != (image.width, image.height):
                    raise RuntimeError("child output resized between local mirror and capture")
                dimensions = (image.width, image.height)
                private_roi, private_plain = self.fixture_shape("protected", dimensions=dimensions)
                public_roi, public_plain = self.fixture_shape("foreground", dimensions=dimensions)
                forbidden = absence_report(image, PRIVATE, 8)["forbidden_fraction"]
                opacity = opacity_fraction(image, (0, 0, image.width, image.height))
                private = shape_metrics(color_shape(local, PRIVATE, private_roi),
                                        color_shape(path, (0, 0, 0) if mode == "black" else MASK, private_roi), private_plain)
                public = shape_metrics(color_shape(local, PUBLIC, public_roi), color_shape(path, PUBLIC, public_roi), public_plain)
                inherited = private["intersection_over_union"] >= .92
                if private["reference_outside_plain"] >= 150:
                    inherited = inherited and private["outside_inherited_fraction"] >= .8
                self.record(name + ": opaque synthetic replacement follows the native mesh, public scene retained",
                            forbidden == 0 and opacity == 1 and
                            inherited and public["intersection_over_union"] >= .94 and
                            self.geometry() == before and self.privacy(),
                            private_shape=private, public_shape=public, transformer_observation=observed,
                            forbidden_fraction=forbidden, opaque_fraction=opacity, frame_dimensions=dimensions)
                observations.append(private if target == "protected" else public)
                time.sleep(.08)
            self.record("inherit-" + mode + "-" + target + ": positive control has visibly deformed edges",
                        any(item["reference_outside_plain"] >= 150 and item["actual_outside_plain"] >= 120 for item in observations),
                        shapes=observations)

    def region_zoom_inheritance(self, before):
        self.ctl("hyprveil", "color")
        self.focus("background")
        self.focus("protected")
        for zoom in (1, 2):
            self.ctl("eval", "hl.config({cursor={zoom_factor=" + str(zoom) +
                     ",zoom_rigid=false,zoom_detached_camera=false}})")
            self.dispatch("hl.dsp.cursor.move({x=512,y=384})")
            time.sleep(.35)
            for region in (None, (180, 130, 520, 420)):
                if region:
                    monitor = self.monitor()
                    x, y, width, height = region
                    region = (x, y, min(width, monitor["width"] - x), min(height, monitor["height"] - y))
                    if min(region[2:]) <= 0:
                        raise RuntimeError("child output too small for nonzero-origin FX region")
                name = "inherit-zoom-" + str(zoom) + ("-region" if region else "-monitor")
                self.ctl("hyprveil", "dump-local")
                path = self.artifacts / (name + ".png")
                if region:
                    x, y, width, height = region
                    self.command(["grim", "-g", f"{x},{y} {width}x{height}", str(path)])
                else:
                    self.capture(path)
                local = self.artifacts / (name + "-local.png")
                shutil.copyfile(self.runtime / "hyprveil-local.png", local)
                if region:
                    cropped = self.artifacts / (name + "-local-crop.png")
                    crop_png(local, cropped, region)
                    local = cropped
                image = Image(path)
                whole = (0, 0, image.width, image.height)
                private = shape_metrics(color_shape(local, PRIVATE, whole), color_shape(path, MASK, whole), set())
                public = shape_metrics(color_shape(local, PUBLIC, whole), color_shape(path, PUBLIC, whole), set())
                observed = json.loads(self.ctl("hyprveil-fx-probe"))
                forbidden = absence_report(image, PRIVATE, 8)["forbidden_fraction"]
                opacity = opacity_fraction(image, whole)
                self.record(name + ": FX mask and public view match the final local camera/crop",
                            private["intersection_over_union"] >= .92 and public["intersection_over_union"] >= .94 and
                            forbidden == 0 and opacity == 1 and observed["private_transformers"] > 0 and
                            self.geometry() == before and self.privacy(),
                            private_shape=private, public_shape=public, transformer_observation=observed,
                            forbidden_fraction=forbidden, opaque_fraction=opacity, zoom=zoom, capture_region=region)
        self.ctl("eval", "hl.config({cursor={zoom_factor=1}})")
        time.sleep(.3)

    def unsupported_transformer(self, before):
        attached = json.loads(self.ctl("hyprveil-fx-probe", "attach-unknown"))
        if not attached["unknown_attached"]:
            raise RuntimeError("unsupported transformer fixture did not attach")
        try:
            self.ctl("hyprveil", "color")
            self.focus("background")
            self.focus("protected")
            time.sleep(.35)
            self.ctl("hyprveil", "dump-local")
            path = self.artifacts / "unsupported-private-chain.png"
            self.capture(path)
            image = Image(path)
            roi, plain = self.fixture_shape("protected", dimensions=(image.width, image.height))
            shape = color_shape(path, MASK, roi)
            observed = json.loads(self.ctl("hyprveil-fx-probe"))
            private = self.protected()
            center = (private["at"][0] + 64, private["at"][1] + 64, 32, 32)
            self.record("mixed unknown/private FX chain uses plain synthetic mask without calling the unknown export transformer",
                        observed["private_transformers"] >= 2 and observed["unknown_total_calls"] > 0 and
                        observed["unknown_capture_calls"] == 0 and color_fraction(image, center, MASK, 1) == 1 and
                        len(shape - plain) == 0 and absence_report(image, PRIVATE, 8)["ok"] and
                        opacity_fraction(image, (0, 0, image.width, image.height)) == 1 and
                        self.geometry() == before and self.privacy(),
                        transformer_observation=observed, synthetic_pixels_outside_plain=len(shape - plain))
        finally:
            detached = json.loads(self.ctl("hyprveil-fx-probe", "detach-unknown"))
            if detached["unknown_attached"]:
                raise RuntimeError("unsupported transformer fixture did not detach")

    def mirrored_pool_retention(self):
        (self.runtime / "home/.config/omarchy/omarchy-fx.conf").write_text(
            "wobbly_enabled=true\nelastic_enabled=true\npulse_enabled=true\n"
            "pulse_on_switch=true\npulse_amount=40\npulse_period=1800\npulse_damping=0\n"
            "shake_enabled=false\n")
        self.ctl("reload")
        self.focus("background")
        self.focus("protected")
        # Public native transformers legitimately reuse their pool entries.
        # Remove only our two owned public clients, leaving one private view
        # so this sentinel isolates the sanitized private transformer path.
        public_pids = {window["pid"] for window in self.clients()
                       if window["class"] in ("org.hyprveil.fixture.background", "org.hyprveil.fixture.foreground")}
        for client in self.fixtures:
            if client.pid in public_pids:
                client.terminate()
                client.wait(timeout=3)
        deadline = time.monotonic() + 3
        while any(window["pid"] in public_pids for window in self.clients()):
            if time.monotonic() >= deadline:
                raise RuntimeError("owned public fixture did not close before synthetic mirror regression")
            time.sleep(.05)
        self.ctl("repl", 'assert(hl.plugin.hyprveil.configure({mode="color",mask_color="#1abf66",eye=false,speed=0,shader_path=""}))')
        self.focus("protected")
        time.sleep(.32)
        if json.loads(self.ctl("hyprveil-fx-probe"))["private_transformers"] <= 0:
            raise RuntimeError("synthetic mirror regression has no active reviewed private transformer")
        armed = json.loads(self.ctl("hyprveil-fx-probe", "arm-mirror"))
        if not armed["mirror_armed"]:
            raise RuntimeError("synthetic mirror sentinel did not arm")
        path = self.artifacts / "synthetic-mirror-pool.png"
        self.capture(path)
        observed = json.loads(self.ctl("hyprveil-fx-probe"))
        image = Image(path)
        forbidden = absence_report(image, PRIVATE, 8)["forbidden_fraction"]
        opacity = opacity_fraction(image, (0, 0, image.width, image.height))
        self.record("private FX excludes spare mirror-attached pool framebuffer without modifying its synthetic sentinel",
                    observed["mirror_seeded"] and observed["mirror_checked"] and observed["mirror_unchanged"] and
                    not observed["mirror_error"] and observed["private_transformers"] > 0 and
                    forbidden == 0 and opacity == 1 and self.privacy(),
                    transformer_observation=observed, forbidden_fraction=forbidden, opaque_fraction=opacity,
                    readback_scope="new fixture-owned solid sentinel texture only; no native mirror read")

    def load_extra(self, path):
        if self.ctl("plugin", "load", str(path)) != "ok":
            raise RuntimeError("test plugin load failed")

    def focus(self, role):
        matches = [c for c in self.clients() if c["class"] == "org.hyprveil.fixture." + role]
        if len(matches) != 1:
            raise RuntimeError("ambiguous focus fixture")
        self.dispatch('hl.dsp.focus({window=' + json.dumps("address:" + matches[0]["address"]) + '})')

    def tests(self):
        self.report["scope"] = "marked seat-disabled child compositor, copied existing reviewed OmaFX binary; no host installation"
        output = self.monitor()["name"]
        config = self.runtime / "hyprland.lua"
        config.write_text(config.read_text() + '\nhl.monitor({output=' + json.dumps(output) +
                          ',mode="1024x768@60",position="0x0",scale=1})\n')
        self.ctl("reload")
        time.sleep(.3)
        config_home = self.runtime / "home/.config/omarchy"
        (config_home / "plugins/omarchy-fx").mkdir(parents=True)
        (config_home / "shell.json").write_text('{"bar":{"right":[{"id":"omarchy-fx"}]}}\n')
        (config_home / "omarchy-fx.conf").write_text(
            "wobbly_enabled=true\nelastic_enabled=true\npulse_enabled=true\n"
            "pulse_on_switch=true\npulse_strength=4\nshake_enabled=false\n")
        fx = self.artifacts / "omarchy-fx.so"
        shutil.copyfile(self.args.fx_plugin, fx)
        fx.chmod(0o500)
        self.report["fx_sha256"] = hashlib.sha256(fx.read_bytes()).hexdigest()
        flags = shlex.split(subprocess.check_output(["pkg-config", "--cflags", "--libs", "hyprland"], text=True))
        probe = self.artifacts / "fx-probe.so"
        subprocess.run(["c++", "-std=c++23", "-shared", "-fPIC", "-fno-gnu-unique", "-O1",
                        str(PROJECT / "tests/fixtures/fx_probe.cpp"), "-o", str(probe), *flags],
                       check=True, timeout=60, capture_output=True, text=True)
        self.load_extra(fx)
        self.load_extra(probe)
        if self.ctl("configerrors"):
            raise RuntimeError("FX lab config errors")
        self.fixture("background")
        self.fixture("protected")
        self.fixture("foreground")
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        self.ctl("dismissnotify", "-1")
        self.focus("background")
        self.focus("protected")
        native_before = self.artifacts / "native-fx-before.png"
        native_observation = json.loads(self.ctl("hyprveil-fx-probe"))
        self.capture(native_before)
        self.report["native_fx_baseline"] = {
            "transformers": native_observation,
            "private_fraction": absence_report(Image(native_before), (232, 64, 144), tolerance=8)["forbidden_fraction"]}
        self.load()
        self.ctl("dismissnotify", "-1")
        self.dispatch('hl.dsp.cursor.move({x=16,y=16})')
        if self.args.mirror_only:
            self.mirrored_pool_retention()
            return
        before = self.geometry()
        mask = self.artifacts / "mask.png"
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 240)
        paint = cairo.Context(surface)
        paint.set_source_rgb(*(component / 255 for component in MASK))
        paint.paint()
        surface.write_to_png(str(mask))
        for mode in ("black", "omit", "image", "spoiler"):
            self.ctl("hyprveil", mode, *([str(mask)] if mode == "image" else []))
            for target, key in (("protected", "private_transformers"), ("foreground", "public_transformers")):
                self.focus("background")
                self.focus(target)
                observed = json.loads(self.ctl("hyprveil-fx-probe"))
                if observed[key] <= 0:
                    raise RuntimeError("actual transformer not active; test inconclusive: " + str(observed))
                self.ctl("hyprveil", "dump-local")
                capture = self.artifacts / f"{mode}-{target}.png"
                self.capture(capture)
                local = self.artifacts / f"{mode}-{target}-local.png"
                shutil.copyfile(self.runtime / "hyprveil-local.png", local)
                image = Image(capture)
                absence = absence_report(image, (232, 64, 144), tolerance=8)
                public = color_fraction(image, (432, 328, 128, 88), (242, 207, 82), 8)
                self.record(f"{mode} capture with active {target} transformer",
                            absence["ok"] and public >= 0.99 and before == self.geometry() and
                            image_stats(local)["private_fraction"] > 0 and self.privacy(),
                            transformer_observation=observed, forbidden_fraction=absence["forbidden_fraction"],
                            public_roi_fraction=public, local_private_fraction=image_stats(local)["private_fraction"])
        self.visual_inheritance(config_home, before, mask)
        self.region_zoom_inheritance(before)
        self.unsupported_transformer(before)
        self.unload()
        self.focus("background")
        self.focus("protected")
        native = self.artifacts / "after-unload.png"
        self.capture(native)
        self.report["native_fx_after_unload"] = {
            "transformers": json.loads(self.ctl("hyprveil-fx-probe")),
            "private_fraction": absence_report(Image(native), (232, 64, 144), tolerance=8)["forbidden_fraction"]}
        self.report["native_fallback_scope"] = (
            "Unloading retains the native privacy property and OmaFX, but does not guarantee pixel privacy: "
            "the pinned native fallback blacks the original window rectangle, so deformed edges can remain visible. "
            "The after-unload check verifies metadata/lifetime only; private_fraction quantifies that native limitation.")
        self.record("Hyprveil unload preserves FX and window privacy property",
                    self.privacy() and any(p["name"] == "omarchy-fx" for p in json.loads(self.ctl("-j", "plugin", "list"))))
        self.load()
        self.mirrored_pool_retention()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    parser.add_argument("--fx-plugin", type=Path, required=True)
    parser.add_argument("--mirror-only", action="store_true", help="run only the lab-owned mirror pool sentinel regression")
    run = FxSmoke(parser.parse_args())
    def interrupted(signum, frame):
        raise KeyboardInterrupt("FX smoke interrupted")
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        run.start()
        run.tests()
        run.report["ok"] = bool(run.checks) and all(c["ok"] for c in run.checks)
    except (Exception, KeyboardInterrupt) as error:
        run.report["error"] = str(error)
        print(json.dumps({"event":"error", "error":str(error)}), flush=True)
    finally:
        run.cleanup()
    print(json.dumps({"ok":run.report["ok"], "report":str(run.artifacts / "report.json") if run.artifacts else None,
                      "lab_stopped":run.report.get("lab_stopped")}), flush=True)
    return 0 if run.report["ok"] and run.report.get("lab_stopped") else 1

if __name__ == "__main__":
    raise SystemExit(main())
