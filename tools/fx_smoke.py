#!/usr/bin/env python3
"""Exercise real omarchy-fx transformers in a synthetic, guarded compositor."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import signal
import subprocess

import cairo
from lab import PROJECT
from stress import Stress, image_stats
import sys
sys.path.insert(0, str(PROJECT / "tests"))
from verify_capture import Image, absence_report, color_fraction


class FxSmoke(Stress):
    def __init__(self, args):
        args.suite = "fx"
        super().__init__(args)
        self.report["pixel_matching"] = "whole-frame secret-color absence with RGB tolerance 8; exact local synthetic RGB"

    def load_extra(self, path):
        if self.ctl("plugin", "load", str(path)) != "ok":
            raise RuntimeError("test plugin load failed")

    def focus(self, role):
        matches = [c for c in self.clients() if c["class"] == "org.hyprveil.fixture." + role]
        if len(matches) != 1:
            raise RuntimeError("ambiguous focus fixture")
        self.dispatch('hl.dsp.focus({window=' + json.dumps("address:" + matches[0]["address"]) + '})')

    def tests(self):
        self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
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
        before = self.geometry()
        mask = self.artifacts / "mask.png"
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 240)
        paint = cairo.Context(surface)
        paint.set_source_rgb(0.1, 0.75, 0.4)
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
        self.unload()
        self.focus("background")
        self.focus("protected")
        native = self.artifacts / "after-unload.png"
        self.capture(native)
        self.report["native_fx_after_unload"] = {
            "transformers": json.loads(self.ctl("hyprveil-fx-probe")),
            "private_fraction": absence_report(Image(native), (232, 64, 144), tolerance=8)["forbidden_fraction"]}
        self.record("Hyprveil unload preserves FX and window privacy property",
                    self.privacy() and any(p["name"] == "omarchy-fx" for p in json.loads(self.ctl("-j", "plugin", "list"))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    parser.add_argument("--fx-plugin", type=Path, required=True)
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
