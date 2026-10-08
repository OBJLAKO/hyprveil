#!/usr/bin/env python3
"""Capture README frames from the real GPU renderer in an owned marked lab.

Requires the same GTK/cairo/native lab dependencies as the regression suite.
No compositor state, socket or environment is selected implicitly.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from stress import Stress, image_stats

class Demo(Stress):
    def checks_and_capture(self):
        if self.report["plugin_sha256"] != self.args.plugin_sha256:
            raise RuntimeError("demo binary differs from the explicit tested pin")
        self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        config = self.runtime / "hyprland.lua"
        config.write_text(config.read_text() + '\nhl.monitor({output="HV-TEST",mode="800x500@60",position="0x0",scale=1})\n')
        self.ctl("reload")
        if self.ctl("configerrors"):
            raise RuntimeError("synthetic demo config has errors")
        if self.ctl("plugin", "load", str(self.plugin)) != "ok":
            raise RuntimeError("synthetic native load refused")
        self.loaded = True
        log = (self.artifacts / "fixture.log").open("w")
        self.logs.append(log)
        self.fixtures.append(subprocess.Popen([sys.executable, str(Path(__file__).with_name("fixture.py")),
            "--lab-dir", str(self.runtime), "--wayland-display", self.state["wayland_display"]],
            env=self.env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in self.fixtures):
                raise RuntimeError("demo document exited")
            if any(w["class"] == "org.hyprveil.fixture.protected" for w in self.clients()):
                break
            time.sleep(.1)
        address = self.address()
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(address) + ',tag="+hyprveil-private-test"})')
        self.dispatch('hl.dsp.window.fullscreen({window=' + json.dumps(address) + ',action="set",mode="fullscreen",layout_aware=false})')
        self.dispatch('hl.dsp.cursor.move({x=0,y=0})')
        self.ctl("dismissnotify", "-1")
        time.sleep(.3)
        self.ctl("hyprveil", "spoiler")
        self.ctl("hyprveil", "dump-local")
        self.capture(self.artifacts / "local-trigger.png")
        local = self.artifacts / "local.png"
        shutil.copyfile(self.runtime / "hyprveil-local.png", local)
        baseline = self.geometry()
        visible = image_stats(local)
        if not self.record("synthetic local document is visible and privacy remains enabled", visible["private_fraction"] > .001 and self.privacy()):
            raise RuntimeError("local synthetic marker was not visible")
        frames = {}
        for variant in ("satin", "telegram"):
            self.ctl("hyprveil", "appearance", variant, "#a2d9c8", "65", "125", "28", "1", "96")
            variant_frames = []
            started = time.monotonic()
            for index in range(24):
                delay = started + index / 10 - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                path = self.artifacts / (variant + "-%03d.png" % index)
                self.capture(path)
                pixels = image_stats(path)
                if pixels["private_fraction"] != 0 or self.geometry() != baseline or not self.privacy():
                    raise RuntimeError("protected demo capture exposed marker or changed window policy")
                variant_frames.append(path)
            unique = len({hashlib.sha256(p.read_bytes()).hexdigest() for p in variant_frames})
            if not self.record(variant + " real GPU frames animate with no synthetic private marker", unique > 20, frames=len(variant_frames), unique=unique):
                raise RuntimeError("GPU demo did not animate")
            frames[variant] = [p.name for p in variant_frames]
        self.report.update(demo_frames=frames, local_frame=local.name,
                           demo_settings=dict(color="#a2d9c8", grain=65, speed=125, darkness=28, eye=True, eye_size=96),
                           synthetic_content="GTK cairo notes fixture; unique pink marker absent from all protected captures",
                           native_frame_size=[800, 500], capture_fps=10)
        self.report["ok"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=ROOT / "build/hyprveil.so")
    parser.add_argument("--plugin-sha256", required=True, help="explicit SHA-256 of the build to demonstrate")
    args = parser.parse_args()
    args.suite = "readme-demo"
    demo = Demo(args)
    def interrupt(signum, frame):
        raise KeyboardInterrupt("demo capture interrupted")
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        demo.start()
        demo.checks_and_capture()
    except (Exception, KeyboardInterrupt) as error:
        demo.report["error"] = str(error)
        demo.report["ok"] = False
    finally:
        demo.cleanup()
    print(json.dumps({"ok": demo.report["ok"], "report": str(demo.artifacts / "report.json") if demo.artifacts else None,
                      "lab_stopped": demo.report.get("lab_stopped")}), flush=True)
    return 0 if demo.report["ok"] and demo.report.get("lab_stopped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
