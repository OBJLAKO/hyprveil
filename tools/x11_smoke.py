#!/usr/bin/python3
"""Exercise dynamic X11 transient privacy in a newly owned, guarded lab."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import time

import permission_smoke as permission
from lab import PROJECT, load_lab
from stress import BACKGROUND_RGB, PRIVATE_RGB, image_stats
from x11_guard import connect_lab


X11_RULES = '''
hl.window_rule({match={class="^org[.]hyprveil[.]fixture[.]x11[.].*$"},
  float=true,no_anim=true,no_shadow=true,no_blur=true,border_size=0})
hl.window_rule({match={class="^org[.]hyprveil[.]fixture[.]x11[.]parent$"},size={240,160},move={64,64}})
hl.window_rule({match={class="^org[.]hyprveil[.]fixture[.]x11[.]child$"},size={200,120},move={576,360}})
hl.window_rule({match={class="^org[.]hyprveil[.]fixture[.]x11[.]public$"},size={160,120},move={96,480}})
'''


class X11Smoke(permission.PermissionSmoke):
    def __init__(self, args):
        previous = permission.CONFIG
        if previous.count("xwayland = { enabled = false }") != 1:
            raise RuntimeError("expected one disabled-Xwayland lab starter rule")
        permission.CONFIG = previous.replace("xwayland = { enabled = false }", "xwayland = { enabled = true }") + X11_RULES
        args.case = "privacy"
        try:
            super().__init__(args)
        finally:
            permission.CONFIG = previous
        self.xcb_client = None
        self.report.update(suite="x11-transient", static_stream_tested=False,
                           unset_semantics="Hyprland 0.56 retains its cached parent on deleted WM_TRANSIENT_FOR")

    def window(self, role):
        matches = [item for item in self.clients() if item.get("class") == "org.hyprveil.fixture.x11." + role and item.get("mapped")]
        if len(matches) != 1:
            raise RuntimeError("expected one mapped synthetic X11 " + role)
        return matches[0]

    def protected(self):
        # The target has no native no_screen_share flag; effective protection
        # comes exclusively from its separate protected X11 parent's flag.
        return self.window("child")

    def roi(self):
        item = self.protected()
        monitor = self.monitor()
        return int(item["at"][0] - monitor["x"]) + 12, int(item["at"][1] - monitor["y"]) + 12, int(item["size"][0]) - 24, int(item["size"][1]) - 24

    def generation(self):
        return json.loads(self.ctl("hyprveil", "status"))["policy_generation"]

    def xcb_line(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.xcb_client.poll() is not None:
                raise RuntimeError("synthetic XCB fixture exited")
            if select.select([self.xcb_client.stdout], [], [], 0.1)[0]:
                line = self.xcb_client.stdout.readline()
                if line:
                    return json.loads(line)
        raise RuntimeError("synthetic XCB fixture acknowledgement timeout")

    def xcb_command(self, command):
        load_lab(self.runtime)
        self.xcb_client.stdin.write(command + "\n")
        self.xcb_client.stdin.flush()
        reply = self.xcb_line()
        if reply.get("command") != command or not reply.get("ack"):
            raise RuntimeError("unexpected synthetic XCB reply")
        return reply

    def spawn_xcb(self):
        deadline = time.monotonic() + 10
        while True:
            try:
                endpoint, peer = connect_lab(self.runtime)
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        log = (self.artifacts / "xcb-fixture.log").open("w")
        self.logs.append(log)
        try:
            self.xcb_client = subprocess.Popen([sys.executable, str(PROJECT / "tests/fixtures/x11.py"),
                "--lab-dir", str(self.runtime), "--xcb-fd", str(endpoint.fileno())], env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, pass_fds=(endpoint.fileno(),))
            self.fixtures.append(self.xcb_client)
        finally:
            endpoint.close()
        ready = self.xcb_line()
        if not ready.get("ready"):
            raise RuntimeError("synthetic XCB fixture did not report ready")
        self.report["x11_connection"] = peer
        self.report["x11_fixture"] = ready
        self.until(lambda: len([item for item in self.clients() if item.get("class", "").startswith("org.hyprveil.fixture.x11.")]) == 3,
                   "three synthetic X11 windows mapping")
        self.record("three explicitly mapped X11 fixtures", all(self.window(role)["xwayland"] for role in ("parent", "child", "public")))

    def capture_state(self, name, protected, parent_gone=False):
        before = self.geometry()
        roi = self.roi()
        status_before = json.loads(self.ctl("hyprveil", "status"))
        generation_before = status_before["policy_generation"]
        self.ctl("hyprveil", "dump-local")
        path = self.artifacts / (name + ".png")
        self.capture(path)
        exported = image_stats(path, roi)
        local_path = self.artifacts / (name + "-local.png")
        shutil.copy2(self.runtime / "hyprveil-local.png", local_path)
        local = image_stats(local_path, roi)
        status_after = json.loads(self.ctl("hyprveil", "status"))
        generation_after = status_after["policy_generation"]
        vanished_parent = None
        if parent_gone:
            vanished_parent = {"exported": image_stats(path, self.parent_roi),
                               "local": image_stats(local_path, self.parent_roi)}
        self.record(name, (exported["private_fraction"] == 0 and exported["background_roi_fraction"] >= 0.99 if protected
                          else exported["private_roi_fraction"] >= 0.99)
                    and local["private_roi_fraction"] >= 0.99 and not self.privacy() and before == self.geometry()
                    and generation_before == generation_after
                    and status_after["scene_frames"] > status_before["scene_frames"]
                    and (not parent_gone or all(item["background_roi_fraction"] >= 0.99 for item in vanished_parent.values())),
                    pixels=exported, local=local, own_native_flag=self.privacy(), geometry_unchanged=before == self.geometry(),
                    inherited_privacy_expected=protected, child_roi=list(roi),
                    generation_before=generation_before, generation_after=generation_after,
                    scene_frames_before=status_before["scene_frames"], scene_frames_after=status_after["scene_frames"],
                    parent_gone_expected=parent_gone, parent_pixels=vanished_parent)

    def tests(self):
        self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        self.fixture("background")
        self.spawn_xcb()
        parent_address = "address:" + self.window("parent")["address"]
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(parent_address) + ',tag="+hyprveil-private-test"})')
        self.load()
        self.ctl("dismissnotify", "-1")
        self.record("parent native private and child own native public", self.ctl("getprop", parent_address, "no_screen_share") == "true" and not self.privacy())
        self.report["enforce_permissions"] = json.loads(self.ctl("-j", "getoption", "ecosystem:enforce_permissions"))
        self.ctl("hyprveil", "dump-local")
        before = self.generation()

        def attach_private():
            self.xcb_command("private")
            self.until(lambda: self.generation() > before, "silent X11 parent privacy invalidating generation")

        # The FIRST capture freezes the actual public-child permission snapshot.
        # Mutate only WM_TRANSIENT_FOR while native consent is pending.
        self.pending("X11-attach-private-while-consent-pending", attach_private, BACKGROUND_RGB,
                     native_property_expected=False)
        self.record("X11 privacy generation increments on parent attach", self.generation() > before,
                    before=before, after=self.generation())
        # Subsequent fresh-frame assertions do not need another consent model;
        # switch enforcement only in this disposable compositor after the real
        # first pending-snapshot case has completed.
        self.ctl("eval", "hl.config({ecosystem={enforce_permissions=false}})")
        self.capture_state("fresh-inherited-private", True)
        unchanged = self.generation()
        self.xcb_command("unset")
        time.sleep(0.15)
        self.capture_state("native-unset-retains-parent", True)
        self.record("native unset retains effective privacy generation", self.generation() == unchanged,
                    before=unchanged, after=self.generation())
        before = self.generation()
        self.xcb_command("public")
        self.until(lambda: self.generation() > before, "X11 reparent to public invalidation")
        self.capture_state("fresh-reparent-public", False)
        before = self.generation()
        self.xcb_command("private")
        self.until(lambda: self.generation() > before, "second X11 private-parent invalidation")
        self.capture_state("fresh-reparent-private-again", True)
        child_geometry = self.geometry()["org.hyprveil.fixture.x11.child"]
        child_address = self.address()
        parent = self.window("parent")
        monitor = self.monitor()
        self.parent_roi = (int(parent["at"][0] - monitor["x"]) + 12, int(parent["at"][1] - monitor["y"]) + 12,
                           int(parent["size"][0]) - 24, int(parent["size"][1]) - 24)
        before = self.generation()
        self.xcb_command("parent-hide")
        self.until(lambda: self.generation() > before, "X11 parent unmap retaining inherited privacy")
        self.capture_state("inherited-private-after-parent-hide", True, parent_gone=True)
        self.record("X11 unmap leaves the same child mapped and unchanged",
                    self.window("child")["mapped"] and self.geometry()["org.hyprveil.fixture.x11.child"] == child_geometry,
                    generation_before=before, generation_after=self.generation())
        # Destroy the already unmapped parent; no extra generation is required
        # when its effective policy was already retained on the real unmap.
        before = self.generation()
        self.xcb_command("parent-destroy")
        time.sleep(0.15)
        self.capture_state("inherited-private-after-parent-destroy", True, parent_gone=True)
        self.record("X11 destroy leaves the same child mapped and unchanged",
                    self.window("child")["mapped"] and self.geometry()["org.hyprveil.fixture.x11.child"] == child_geometry,
                    generation_before=before, generation_after=self.generation())
        before = self.generation()
        self.dispatch('hl.dsp.window.set_prop({window=' + json.dumps(child_address) + ',prop="no_screen_share",value="0"})')
        self.until(lambda: self.generation() > before, "X11 orphan explicit false reset")
        self.capture_state("orphan-child-explicitly-public", False, parent_gone=True)
        self.record("explicit false reset releases only the surviving child",
                    not self.privacy() and self.geometry()["org.hyprveil.fixture.x11.child"] == child_geometry,
                    generation_before=before, generation_after=self.generation())
        self.report["parent_lifecycle_completed"] = True
        # A fresh XID and the same explicitly reset/public child isolate this
        # test from the prior unmap's retained latch. Destroy a still mapped
        # protected parent without sending xcb_unmap_window first.
        recreated = self.xcb_command("parent-recreate")
        self.until(lambda: any(item.get("class") == "org.hyprveil.fixture.x11.parent" and item.get("mapped")
                               for item in self.clients()), "fresh X11 parent mapping")
        parent = self.window("parent")
        parent_address = "address:" + parent["address"]
        self.record("direct destroy uses a fresh X11 parent XID",
                    recreated["windows"]["parent"] != self.report["x11_fixture"]["windows"]["parent"],
                    original_xid=self.report["x11_fixture"]["windows"]["parent"], fresh_xid=recreated["windows"]["parent"])
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(parent_address) + ',tag="+hyprveil-private-test"})')
        self.record("fresh parent native private before direct destroy",
                    self.ctl("getprop", parent_address, "no_screen_share") == "true" and not self.privacy())
        self.parent_roi = (int(parent["at"][0] - monitor["x"]) + 12, int(parent["at"][1] - monitor["y"]) + 12,
                           int(parent["size"][0]) - 24, int(parent["size"][1]) - 24)
        before = self.generation()
        self.xcb_command("private")
        self.until(lambda: self.generation() > before, "fresh X11 private-parent relationship")
        self.capture_state("fresh-parent-inherited-private", True)
        before = self.generation()
        self.xcb_command("parent-destroy")
        self.until(lambda: self.generation() > before, "mapped X11 parent direct destroy retaining privacy")
        self.capture_state("inherited-private-after-direct-parent-destroy", True, parent_gone=True)
        self.record("direct X11 destroy preserves child address and geometry",
                    self.address() == child_address and self.geometry()["org.hyprveil.fixture.x11.child"] == child_geometry,
                    generation_before=before, generation_after=self.generation())
        before = self.generation()
        self.dispatch('hl.dsp.window.set_prop({window=' + json.dumps(child_address) + ',prop="no_screen_share",value="0"})')
        self.until(lambda: self.generation() > before, "direct-destroy orphan explicit false reset")
        self.capture_state("direct-destroy-orphan-explicitly-public", False, parent_gone=True)
        self.record("direct destroy reset preserves the surviving child",
                    not self.privacy() and self.geometry()["org.hyprveil.fixture.x11.child"] == child_geometry,
                    generation_before=before, generation_after=self.generation())
        self.report["parent_lifecycle_direct_destroy_completed"] = True
        self.report["plugin_status"] = json.loads(self.ctl("hyprveil", "status"))
        self.report["compositor"] = json.loads(self.ctl("-j", "version"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    args = parser.parse_args(argv)
    run = X11Smoke(args)
    def interrupted(signum, frame):
        raise KeyboardInterrupt("X11 smoke interrupted")
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        run.start()
        run.tests()
        run.report["ok"] = bool(run.checks) and all(item["ok"] for item in run.checks)
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
