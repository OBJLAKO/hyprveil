#!/usr/bin/env python3
"""Exercise native pending-consent capture snapshots in an owned marked lab.

Only this lab's PATH resolves the controlled hyprland-dialog replacement. It
waits for a gate before returning the real native dialog's nonpersistent Allow
button. No desktop picker, user service, input simulation or host capture is
used. The first approved PNG must follow the privacy policy selected while
permission was pending.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import cairo

from stress import Stress, PRIVATE_RGB, BACKGROUND_RGB, image_stats
from lab import PROJECT, proc_env, CONFIG
sys.path.insert(0, str(PROJECT / "tests"))
from verify_capture import Image, color_fraction, absence_report


DIALOG = '''#!{python}
import json,os,pathlib,sys,time
root=pathlib.Path(__file__).resolve().parent
runtime=pathlib.Path(os.environ.get("XDG_RUNTIME_DIR","/nonexistent")).resolve()
allowed=(root/"allowed-runtime").read_text().strip()
if str(runtime)!=allowed or os.environ.get("HYPRVEIL_LAB_RUNTIME")!=allowed:
 sys.exit(70)
marker=runtime/".hyprveil-lab"
if marker.is_symlink() or not marker.is_file() or runtime.stat().st_uid!=os.getuid() or runtime.stat().st_mode&0o077:
 sys.exit(71)
data=json.loads(marker.read_text())
if data["runtime_dir"]!=allowed or not (runtime/data["wayland_display"]).is_socket():
 sys.exit(72)
args=sys.argv[1:]
buttons=args[args.index("--buttons")+1].split(";")
choices=[b for b in buttons if b.lower().startswith("allow") and "remember" not in b.lower()]
if not choices:
 sys.exit(73)
ready=runtime/"permission-dialog-ready.json"
tmp=ready.with_suffix(".tmp")
tmp.write_text(json.dumps({{"pid":os.getpid(),"argv":args,"buttons":buttons,"selected":choices[-1]}}))
tmp.replace(ready)
gate=runtime/"permission-dialog-gate"
deadline=time.monotonic()+45
while time.monotonic()<deadline:
 if gate.is_file():
  reply=gate.read_text().strip()
  gate.unlink()
  if reply=="allow":
   selected=choices[-1]
  else:
   selected=buttons[0]
  sys.stdout.write(selected);sys.stdout.flush()
  response=runtime/"permission-dialog-response.json"
  temp=response.with_suffix(".tmp")
  temp.write_text(json.dumps({{"pid":os.getpid(),"selected":selected,"reply":reply}}))
  temp.replace(response)
  sys.exit(0)
 time.sleep(0.02)
sys.exit(74)
'''


class PermissionSmoke(Stress):
    def __init__(self, args):
        args.suite = "permission"
        super().__init__(args)
        self.dialog_directory = Path(tempfile.mkdtemp(prefix="hyprveil-consent-bin-", dir="/tmp"))
        self.dialog_directory.chmod(0o700)
        self.consumer = None
        self.consumer_log = None
        self.dialog_pids = []
        script = self.dialog_directory / "hyprland-dialog"
        script.write_text(DIALOG.format(python=sys.executable))
        # Hyprland's executableExistsInPath checks others_exec. The enclosing
        # owned directory remains 0700, so nobody else can traverse to this.
        script.chmod(0o755)
        allow = 'hl.permission("/usr/bin/grim", "screencopy", "allow")'
        if CONFIG.count(allow) != 1:
            raise RuntimeError("expected exactly one starter grim allow rule")
        initial_config = CONFIG.replace(allow, 'hl.permission("/usr/bin/grim", "screencopy", "ask")') + \
                         '\nhl.config({ecosystem={enforce_permissions=true}})\n'
        # Plugin permissions match the immutable module path. Scope the rule
        # to the copied binary in this test suite's artifact directories.
        plugin_regex = str(PROJECT / "artifacts") + "/hv-[^/]+/permission/hyprveil[.]so"
        initial_config += '\nhl.permission(' + json.dumps(plugin_regex) + ', "plugin", "allow")\n'
        # Permission rules are intentionally registered only on first launch.
        # Replacing a rule through reload would leave the old allow registered.
        bootstrap = self.dialog_directory / "start-permission-lab.py"
        bootstrap.write_text('import sys\nsys.path.insert(0,' + repr(str(PROJECT / "tools")) + ')\n'
                             'import lab\nlab.CONFIG=' + repr(initial_config) + '\nraise SystemExit(lab.main())\n')
        args.lab_entry = bootstrap
        self.report.update(capture_tool="grim", consent="native Hyprland ask with lab-only gated dialog stdout")
        self.report["case"] = args.case

    def start(self):
        previous_path = os.environ.get("PATH", "")
        previous_lang = os.environ.get("LANG")
        os.environ["PATH"] = str(self.dialog_directory) + os.pathsep + previous_path
        os.environ["LANG"] = "C.UTF-8"
        try:
            super().start()
        finally:
            os.environ["PATH"] = previous_path
            if previous_lang is None:
                os.environ.pop("LANG", None)
            else:
                os.environ["LANG"] = previous_lang
        (self.dialog_directory / "allowed-runtime").write_text(str(self.runtime))
        self.report["dialog_wrapper"] = str(self.dialog_directory)
        actual_path = proc_env(self.state["pid"]).get(b"PATH", b"").decode()
        self.report["compositor_path"] = actual_path
        if str(self.dialog_directory) not in actual_path.split(os.pathsep):
            raise RuntimeError("compositor did not inherit the consent-wrapper PATH")

    def until(self, predicate, description, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            if self.consumer and self.consumer.poll() is not None:
                stderr = Path(self.consumer_log.name).read_text() if self.consumer_log else ""
                raise RuntimeError("pending consumer exited before " + description + ": " + stderr[-3000:])
            time.sleep(0.05)
        raise RuntimeError("timeout waiting for " + description)

    def pending(self, name, change, expected_rgb, protection_expected=True, invalidation_required=True,
                native_property_expected=None):
        # Effective inherited privacy can hide a transient whose own native
        # flag is false. Keep that observation separate from pixel privacy.
        if native_property_expected is None:
            native_property_expected = protection_expected
        ready = self.runtime / "permission-dialog-ready.json"
        ready.unlink(missing_ok=True)
        gate = self.runtime / "permission-dialog-gate"
        gate.unlink(missing_ok=True)
        response = self.runtime / "permission-dialog-response.json"
        response.unlink(missing_ok=True)
        destination = self.artifacts / (name + ".png")
        self.consumer_log = (self.artifacts / (name + ".protocol.log")).open("w")
        self.logs.append(self.consumer_log)
        self.consumer = subprocess.Popen(["grim", "-o", "HV-TEST", str(destination)], env=dict(self.env, WAYLAND_DEBUG="1"),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.consumer_log)
        dialog = self.until(lambda: json.loads(ready.read_text()) if ready.exists() else None, "native consent dialog")
        self.dialog_pids.append(dialog["pid"])
        # A real output commit while consent is pending makes the session's
        # temporary framebuffer exist; status must confirm it before mutation.
        self.dispatch('hl.dsp.cursor.move({x=8,y=8})')
        self.until(lambda: (value if (value := json.loads(self.ctl("hyprveil", "status"))).get("pending_snapshots", 0) >= 1 else None),
                   "pending snapshot render")
        pending_status = json.loads(self.ctl("hyprveil", "status"))
        (self.artifacts / (name + ".pending.json")).write_text(json.dumps({
            "dialog": dialog, "status": pending_status, "consumer_waiting": self.consumer.poll() is None}, indent=2) + "\n")
        if self.consumer.poll() is not None or destination.exists():
            raise RuntimeError("capture completed before explicit native consent")
        if self.args.case == "privacy":
            local_path = self.runtime / "hyprveil-local.png"
            local_fraction = color_fraction(Image(local_path), self.roi(), (232, 64, 144), 3)
            if local_fraction < 0.99 or self.privacy():
                raise RuntimeError("own fixture was not locally visible and initially public during pending consent")
            shutil.copy2(local_path, self.artifacts / "pending-public-local.png")
            self.report["positive_local_fixture_fraction"] = local_fraction
        geometry_before = self.geometry()
        change()
        gate.write_text("allow")
        self.until(lambda: (value if (value := json.loads(response.read_text()))["pid"] == dialog["pid"] else None)
                   if response.exists() else None, "native dialog reply")
        # Native consent changes the permission but schedules no output frame.
        # A real dialog's closure normally damages the output. Our stand-in has
        # no Wayland surface, so make that commit explicitly after its reply.
        # This does not replace or repair the cached permission framebuffer.
        time.sleep(0.05)
        self.dispatch('hl.dsp.cursor.move({x=12,y=12})')
        self.consumer.wait(timeout=15)
        if self.consumer.returncode:
            raise RuntimeError("approved capture failed: " + Path(self.consumer_log.name).read_text()[-3000:])
        self.consumer = None
        pixels = image_stats(destination, self.roi())
        exported = Image(destination)
        leak = absence_report(exported, (232, 64, 144), tolerance=3)
        expected_color = ((expected_rgb >> 16) & 255, (expected_rgb >> 8) & 255, expected_rgb & 255)
        expected_fraction = color_fraction(exported, self.roi(), expected_color, 3)
        # A blank whole output must not masquerade as a valid black-window
        # replacement. This region is background, outside the 320x240 fixture.
        public_fraction = color_fraction(exported, (32, 32, 96, 96), (36, 132, 196), 3)
        after = json.loads(self.ctl("hyprveil", "status"))
        (self.artifacts / (name + ".accepted.json")).write_text(json.dumps({
            "pixels": pixels, "whole_frame_privacy": leak, "expected_fraction": expected_fraction,
            "public_background_fraction": public_fraction, "status": after,
            "native_property": self.ctl("getprop", self.address(), "no_screen_share"),
            "geometry_before": geometry_before, "geometry_after": self.geometry()}, indent=2) + "\n")
        local_path = self.runtime / "hyprveil-local.png"
        local_path.unlink(missing_ok=True)
        self.ctl("hyprveil", "dump-local")
        # dumpLocalMirror belongs to the capture render path. A cursor commit
        # alone cannot trigger it after the first consumer has finished. This
        # second ordinary capture is diagnostic only: the first approved PNG
        # and its status/geometry evidence above are already stored unchanged.
        diagnostic_capture = self.artifacts / (name + ".local-trigger.png")
        self.capture(diagnostic_capture)
        self.until(lambda: local_path.exists() and
                   json.loads(self.ctl("hyprveil", "status")).get("local_dump") != "pending",
                   "local fixture rendering after capture")
        local_fraction = color_fraction(Image(local_path), self.roi(), (232, 64, 144), 3)
        shutil.copy2(local_path, self.artifacts / (name + ".local.png"))
        invalidations_before = pending_status.get("invalidated_snapshots", 0)
        invalidations_after = after.get("invalidated_snapshots", 0)
        self.record(name, (not protection_expected or leak["ok"]) and expected_fraction >= 0.99
                    and public_fraction >= 0.99 and local_fraction >= 0.99
                    and self.privacy() == native_property_expected and geometry_before == self.geometry()
                    and (not invalidation_required or invalidations_after > invalidations_before),
                    pixels=pixels, dialog=dialog, pending_status=pending_status, final_status=after,
                    whole_frame_privacy=leak,
                    invalidation_observed=invalidations_after > invalidations_before,
                    invalidation_required=invalidation_required,
                    expected_color=f"{expected_rgb:06x}", expected_fraction=expected_fraction,
                    public_background_fraction=public_fraction,
                    local_fixture_fraction=local_fraction,
                    local_diagnostic_capture=str(diagnostic_capture),
                    native_property_expected=native_property_expected,
                    native_property=self.ctl("getprop", self.address(), "no_screen_share"))

    def tests(self):
        self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        self.fixture("background")
        self.fixture("protected")
        self.load()
        if self.ctl("configerrors"):
            raise RuntimeError("invalid native permission lab config")
        self.ctl("dismissnotify", "-1")
        self.report["enforce_permissions_option"] = json.loads(self.ctl("-j", "getoption", "ecosystem:enforce_permissions"))
        if self.report["enforce_permissions_option"].get("bool") is not True:
            raise RuntimeError("native permission enforcement is not active in the own lab")
        self.report["dialog_discovery"] = self.command(["which", "hyprland-dialog"])
        # One ordinary first request per fresh compositor. Allow once concerns
        # the requesting application/session, not an individual image frame;
        # repeated clients cannot be assumed to open a new dialog immediately.
        if self.args.case == "privacy":
            self.ctl("hyprveil", "dump-local")
            change = lambda: self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) +
                                          ',tag="+hyprveil-private-test"})')
            name, expected = "protect-while-consent-pending", BACKGROUND_RGB
        else:
            self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
            if self.args.case == "black":
                change = lambda: self.ctl("hyprveil", "black")
                name, expected = "black-mode-while-consent-pending", 0
            elif self.args.case == "omit":
                self.ctl("hyprveil", "black")
                change = lambda: self.ctl("hyprveil", "omit")
                name, expected = "omit-mode-while-consent-pending", BACKGROUND_RGB
            else:
                mask = self.artifacts / "pending-gold-mask.png"
                surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 320, 240)
                paint = cairo.Context(surface)
                paint.set_source_rgb(242/255, 207/255, 82/255)
                paint.paint()
                surface.write_to_png(str(mask))
                change = lambda: self.ctl("hyprveil", "image", str(mask))
                name, expected = "PNG-mode-while-consent-pending", 0xF2CF52
        self.pending(name, change, expected)
        self.report["compositor"] = json.loads(self.ctl("-j", "version"))

    def cleanup(self):
        if self.consumer and self.consumer.poll() is None:
            self.consumer.terminate()
            try:
                self.consumer.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.consumer.kill()
                self.consumer.wait()
        if self.runtime:
            (self.runtime / "permission-dialog-gate").write_text("deny")
        super().cleanup()
        for pid in self.dialog_pids:
            try:
                argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                if os.fsencode(str(self.dialog_directory / "hyprland-dialog")) in argv:
                    os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        shutil.rmtree(self.dialog_directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    parser.add_argument("--case", choices=("privacy", "black", "omit", "image"), default="privacy")
    args = parser.parse_args()
    run = PermissionSmoke(args)
    def interrupted(signum, frame):
        raise KeyboardInterrupt("native permission smoke interrupted")
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
    print(json.dumps({"ok": run.report["ok"], "report": str(run.artifacts / "report.json") if run.artifacts else None,
                      "lab_stopped": run.report["lab_stopped"]}), flush=True)
    return 0 if run.report["ok"] and run.report["lab_stopped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
