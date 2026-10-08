#!/usr/bin/env python3
"""Validate the pinned old-to-new privacy handoff in a synthetic owned lab.

No main-session capture or plugin load is performed. Two real GTK transients
have unset native privacy: one is orphaned before guard admission; the other
loses its protected parent while the old module is absent and captures held.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import selectors
import subprocess
import sys
import time

from lab import PROJECT, load_lab
from stress import Stress
sys.path.insert(0, str(PROJECT / "tests"))
from verify_capture import Image, absence_report, color_fraction, opacity_fraction

OLD_SHA = "0426199c06472e4ae262972ea9d13d9fcea11229af55f7274057ce5e370e4196"
OLD_OFFSETS = {
    OLD_SHA: 0x226E0,
    "0c792b967f3204b5302aaf151997fc972cff04ae07be69b8c43b81fe0e51b95e": 0x1BFD0,
    "cb787c8fc8001fcc822e2a4907d515e8fec087616245557d3244dba461d2dc30": 0x19610,
    "894ec2f1ef28c8f4f6d56d5fbde30c6ba13f01fd47a477adf746c13f7b4d916b": 0x1AA10,
    "6a9c071c38cef45eb5c179312e5af1e846405b4622578a78c8bf3afebbb55ba8": 0x1BFD0,
    "fdf4c7d008af84e9c9e92d3d06bb2833cea77b41be54a2072a4101990bee0630": 0x16890,
}
OLD_PINS = frozenset(OLD_OFFSETS)
OLD_DEFAULT = Path.home() / ".local/share/hyprveil/releases" / OLD_SHA / "hyprveil.so"
PRIVATE = (232, 64, 144)
DIALOG_TITLE = "Hyprveil fixture: transient dialog"
PARENT_TITLE = "Hyprveil fixture: protected popup parent"


class UpgradeSmoke(Stress):
    def __init__(self, args):
        args.suite = "upgrade"
        super().__init__(args)
        self.old_loaded = self.guard_loaded = False
        self.old = self.guard = None
        self.pending = None
        self.watcher = None
        self.report["pixel_matching"] = "whole synthetic capture secret-color absence, RGB tolerance 8"
        self.report["watcher"] = str(args.watcher) if args.watcher is not None else None

    def check(self, name, ok, **details):
        self.record(name, ok, **details)
        if not ok:
            raise RuntimeError("assertion failed: " + name)

    def wait_for(self, predicate, label, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for process in self.fixtures:
                if process.poll() is not None:
                    raise RuntimeError("synthetic fixture exited while waiting for " + label)
            value = predicate()
            if value:
                return value
            time.sleep(0.08)
        raise RuntimeError("timed out waiting for " + label)

    def control(self, name, operation, predicate):
        ready = self.runtime / (name + "-ready.json")
        previous = ready.stat().st_mtime_ns
        fd = os.open(self.runtime / (name + "-control.fifo"), os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.write(fd, (operation + "\n").encode("ascii"))
        finally:
            os.close(fd)

        def updated():
            if ready.stat().st_mtime_ns <= previous:
                return None
            value = self.ready_state(name)
            if value.get("error"):
                raise RuntimeError(value["error"])
            return value if predicate(value) else None
        return self.wait_for(updated, name + " " + operation)

    def popup_fixture(self, name, x):
        before = {item["address"] for item in self.clients()}
        log = (self.artifacts / (name + ".log")).open("w")
        self.logs.append(log)
        process = subprocess.Popen([sys.executable, str(PROJECT / "tests/fixtures/popup.py"),
            "--lab-dir", str(self.runtime), "--wayland-display", self.state["wayland_display"],
            "--start-popup", "hide", "--start-dialog", "hide",
            "--ready-file", str(self.runtime / (name + "-ready.json")),
            "--control-file", str(self.runtime / (name + "-control.fifo"))],
            env=self.env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        self.fixtures.append(process)
        self.wait_for(lambda: (value if (value := self.ready_state(name)) and value.get("ready") else None), name + " ready")
        parent = self.wait_for(lambda: next((item for item in self.clients()
            if item["address"] not in before and item["title"] == PARENT_TITLE), None), name + " parent")
        address = "address:" + parent["address"]
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(address) + ',tag="+hyprveil-private-test"})')
        self.check(name + ": parent uses native privacy", self.ctl("getprop", address, "no_screen_share") == "true")
        self.control(name, "dialog-show", lambda value: value.get("ready") and value["dialog"]["mapped"])
        dialog = self.wait_for(lambda: next((item for item in self.clients()
            if item["address"] not in before and item["title"] == DIALOG_TITLE), None), name + " dialog")
        self.dispatch('hl.dsp.window.move({window=' + json.dumps("address:" + dialog["address"]) +
                      f',x={x},y=520,relative=false}})')
        self.check(name + ": dialog own native privacy stays false",
                   self.ctl("getprop", "address:" + dialog["address"], "no_screen_share") == "false")
        return parent, dialog

    def assert_secret_absent(self, name):
        path = self.artifacts / (name + ".png")
        self.capture(path)
        result = absence_report(Image(path), PRIVATE, tolerance=8)
        self.check(name, result.pop("ok"), **result)
        return path

    def active_status(self, label, expected):
        state = json.loads(self.ctl("hyprveil", "active-privacy"))
        self.check(label + ": atomic native privacy status", state == expected, state=state)
        if self.args.watcher is not None:
            watched = json.loads(self.command(["/usr/bin/python3", str(self.args.watcher), "watch", "--once", "--lab-runtime", str(self.runtime)]))
            self.check(label + ": attested helper agrees without titles", watched == expected, state=watched)

    def privacy_expected(self, kind, address="", native=False, inherited=False):
        stable = ""
        if address:
            window = next(item for item in self.clients() if item["address"] == address)
            token = window["stableId"]
            if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{1,16}", token):
                raise RuntimeError("synthetic stableId is not canonical native hexadecimal")
            stable = str(int(token, 16))
            if not re.fullmatch(r"[1-9][0-9]{0,18}", stable):
                raise RuntimeError("synthetic stable identity exceeds the native decimal bounds")
        return {"state": kind, "address": address, "stable_id": stable, "native_private": native, "inherited": inherited}

    def focus_window(self, address):
        self.dispatch('hl.dsp.focus({window=' + json.dumps("address:" + address) + '})')

    def stream_state(self, label, expected):
        if self.watcher is None:
            return
        with selectors.DefaultSelector() as selector:
            selector.register(self.watcher.stdout, selectors.EVENT_READ)
            ready = selector.select(2.0)
        self.check(label + ": changed stream arrives within bound", bool(ready))
        line = self.watcher.stdout.readline()
        state = json.loads(line)
        self.check(label + ": persistent helper state", state == expected, state=state)

    def active_tests(self, orphan):
        background = next(item for item in self.clients() if item["class"] == "org.hyprveil.fixture.background")
        address = background["address"]
        self.focus_window(address)
        self.active_status("public focused fixture", self.privacy_expected("visible", address))
        if self.args.watcher is not None:
            self.watcher = subprocess.Popen(["/usr/bin/python3", str(self.args.watcher), "watch", "--lab-runtime", str(self.runtime)],
                env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            self.stream_state("initial public focus", self.privacy_expected("visible", address))
            with selectors.DefaultSelector() as selector:
                selector.register(self.watcher.stdout, selectors.EVENT_READ)
                ready = selector.select(0.75)
            self.check("unchanged privacy produces no duplicate stream output across fallback polls", not ready and self.watcher.poll() is None)
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps("address:" + address) + ',tag="+hyprveil-private-test"})')
        self.stream_state("privacy tag changes without focus events", self.privacy_expected("hidden", address, True))
        self.active_status("native private focused fixture", self.privacy_expected("hidden", address, True))
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps("address:" + address) + ',tag="-hyprveil-private-test"})')
        self.stream_state("privacy tag removal", self.privacy_expected("visible", address))
        if self.args.watcher is None:
            self.active_status("privacy tag removal", self.privacy_expected("visible", address))
        self.focus_window(orphan["address"])
        self.stream_state("focus switches to inherited privacy", self.privacy_expected("hidden", orphan["address"], inherited=True))
        self.active_status("retained private focused dialog", self.privacy_expected("hidden", orphan["address"], inherited=True))
        self.dispatch('hl.dsp.focus({workspace="99"})')
        self.stream_state("focus leaves all windows", self.privacy_expected("none"))
        self.active_status("empty workspace", self.privacy_expected("none"))
        self.dispatch('hl.dsp.focus({workspace="1"})')
        self.focus_window(address)
        self.stream_state("public focus restored", self.privacy_expected("visible", address))
        if self.watcher is not None:
            self.watcher.terminate()
            self.watcher.communicate(timeout=2)
            self.watcher = None
        else:
            self.active_status("public focus restored", self.privacy_expected("visible", address))

    def tests(self):
        self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        config = self.runtime / "hyprland.lua"
        with config.open("a") as handle:
            handle.write('\nhl.window_rule({match={title="^Hyprveil fixture: transient dialog$"},size={180,120}})\n')
        self.ctl("reload")
        if self.ctl("configerrors"):
            raise RuntimeError("upgrade lab configuration errors")
        self.fixture("background")
        old_sha = hashlib.sha256(self.args.old_plugin.read_bytes()).hexdigest()
        if old_sha not in OLD_PINS or self.args.old_sha256 is not None and old_sha != self.args.old_sha256:
            raise RuntimeError("old migration source differs from an explicitly allowed immutable ELF")
        self.old = self.artifacts / "hyprveil-old.so"
        self.guard = self.artifacts / "hyprveil-upgrade-guard.so"
        shutil.copyfile(self.args.old_plugin, self.old)
        shutil.copyfile(self.args.guard_plugin, self.guard)
        self.old.chmod(0o500)
        self.guard.chmod(0o500)
        self.report.update(old_sha256=old_sha, old_source=str(self.args.old_plugin.resolve(strict=True)),
                           reviewed_private_window_offset=hex(OLD_OFFSETS[old_sha]),
                           guard_sha256=hashlib.sha256(self.guard.read_bytes()).hexdigest())
        if self.ctl("plugin", "load", str(self.old)) != "ok":
            raise RuntimeError("old plugin load refused")
        self.old_loaded = True
        self.ctl("hyprveil", "omit")

        first_parent, orphan = self.popup_fixture("orphan", 560)
        self.control("orphan", "parent-close", lambda value: value.get("ready") and value["parent_destroyed"] and value["dialog"]["mapped"])
        live_parent, inherited = self.popup_fixture("inherited", 760)
        baseline = json.loads(self.ctl("hyprveil", "fixture-privacy"))
        by_address = {item["address"]: item for item in baseline["windows"]}
        self.check("old binary retains orphan before upgrade", by_address[orphan["address"]]["retained_private"])
        self.check("old binary protects live inherited dialog", by_address[inherited["address"]]["effective_private"] and
                   not by_address[inherited["address"]]["native_private"])
        self.assert_secret_absent("old sanitized monitor excludes inherited and orphan dialogs")
        guard_load = self.ctl("plugin", "load", str(self.guard))
        if guard_load != "ok":
            raise RuntimeError("upgrade guard load refused: " + guard_load)
        self.guard_loaded = True
        status = json.loads(self.ctl("hv-upgrade", "status"))
        self.check("guard holds all capture before old unload", status["held"] and not status["transferred"] and status["private_refs"] >= 3,
                   guard=status)
        denied = json.loads(self.ctl("hv-upgrade", "release"))
        self.check("release before handoff is rejected", "error" in denied)
        new_sha = hashlib.sha256(self.plugin.read_bytes()).hexdigest()
        invalid = json.loads(self.ctl("hv-upgrade", "handoff", str(self.plugin), new_sha))
        self.check("handoff before old unload/new load is rejected", "error" in invalid)
        if self.ctl("plugin", "unload", str(self.old)) != "ok":
            raise RuntimeError("old plugin unload refused")
        self.old_loaded = False
        self.control("inherited", "parent-close", lambda value: value.get("ready") and value["parent_destroyed"] and value["dialog"]["mapped"])
        held_file = self.artifacts / "held-gap.png"
        load_lab(self.runtime)
        self.pending = subprocess.Popen(["grim", "-o", "HV-TEST", str(held_file)], env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.8)
        held_exit = self.pending.poll()
        self.check("capture does not produce pixels during unloaded gap", (held_exit is None or held_exit != 0) and not held_file.exists(),
                   capture_exit=held_exit, output_exists=held_file.exists())
        if self.pending.poll() is None:
            self.pending.terminate()
        self.pending.communicate(timeout=3)
        self.pending = None
        held_status = json.loads(self.ctl("hv-upgrade", "status"))
        self.check("guard remains active after rejected operations and orphaning", held_status["held"] and
                   held_status["held_commits"] > 0 and held_status["private_refs"] >= 2, guard=held_status)
        admitted = self.ctl("plugin", "load", str(self.plugin))
        if admitted != "ok":
            raise RuntimeError("new plugin load refused: " + admitted)
        self.loaded = True
        # The new native source can select a decorative mode during reload.
        # This must never authorize adoption even though captures are held.
        config = self.runtime / "hyprland.lua"
        config.write_text(config.read_text() + '\nhl.config({plugin={hyprveil={mode="spoiler",variant="telegram",speed=0}}})\n')
        self.ctl("reload")
        native = json.loads(self.ctl("hyprveil", "status"))
        self.check("new native file settings apply while capture remains held",
                   not self.ctl("configerrors") and native.get("config_api") == 1 and
                   native.get("mode") == "spoiler" and native.get("appearance", {}).get("variant") == "telegram" and
                   json.loads(self.ctl("hv-upgrade", "status"))["held"], native=native)
        nonblack = json.loads(self.ctl("hv-upgrade", "handoff", str(self.plugin), new_sha))
        held = json.loads(self.ctl("hv-upgrade", "status"))
        self.check("native configuration in spoiler cannot admit privacy handoff",
                   "error" in nonblack and held["held"] and not held["transferred"], guard=held)
        black = json.loads(self.ctl("hyprveil", "black"))
        self.check("explicit black is reasserted after native reload before handoff",
                   black.get("mode") == "black" and json.loads(self.ctl("hyprveil", "status"))["mode"] == "black")
        invalid = json.loads(self.ctl("hv-upgrade", "handoff", str(self.plugin), "0" * 64))
        self.check("wrong new ELF pin rejects handoff and keeps captures held", "error" in invalid and
                   json.loads(self.ctl("hv-upgrade", "status"))["held"])
        status = json.loads(self.ctl("hv-upgrade", "handoff", str(self.plugin), new_sha))
        self.check("weak privacy handoff succeeds while captures held", status.get("transferred") and status.get("held"), guard=status)
        released = json.loads(self.ctl("hv-upgrade", "release"))
        self.check("only confirmed adoption authorizes capture release", released.get("transferred") and released.get("held") is False,
                   guard=released)
        if self.ctl("plugin", "unload", str(self.guard)) != "ok":
            raise RuntimeError("guard teardown refused after handoff")
        self.guard_loaded = False
        adopted = json.loads(self.ctl("hyprveil", "fixture-privacy"))
        by_address = {item["address"]: item for item in adopted["windows"]}
        for name, dialog in (("prior orphan", orphan), ("parent closed during gap", inherited)):
            item = by_address[dialog["address"]]
            self.check(name + ": inherited privacy retained without changing native flags",
                       item["effective_private"] and item["retained_private"] and not item["native_private"], state=item)
            current = next(item for item in self.clients() if item["address"] == dialog["address"])
            direct_path = self.artifacts / ("direct-" + dialog["address"] + ".png")
            self.command(["grim", "-T", current["stableId"], str(direct_path)])
            image = Image(direct_path)
            rect = (0, 0, image.width, image.height)
            self.check(name + ": direct capture stays opaque black", color_fraction(image, rect, (0, 0, 0), 0) == 1 and
                       opacity_fraction(image, rect) == 1)
        for mode in ("black", "spoiler", "omit"):
            self.ctl("hyprveil", mode)
            self.assert_secret_absent("new " + mode + " excludes both transferred private dialogs")
        self.active_tests(orphan)
        self.report["final_plugins"] = json.loads(self.ctl("-j", "plugin", "list"))
        self.check("upgrade guard fully removed", not any(item["name"] == "hyprveil-upgrade-guard" for item in self.report["final_plugins"]))

    def ready_state(self, name):
        path = self.runtime / (name + "-ready.json")
        return json.loads(path.read_text()) if path.exists() else None

    def cleanup(self):
        if self.watcher is not None:
            if self.watcher.poll() is None:
                self.watcher.kill()
            self.watcher.communicate(timeout=2)
            self.watcher = None
        if self.pending is not None:
            if self.pending.poll() is None:
                self.pending.kill()
            self.pending.communicate()
            self.pending = None
        # On failure the guard stays held until our owned compositor shuts down.
        # Never release an untransferred hold merely to simplify cleanup.
        super().cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    parser.add_argument("--guard-plugin", type=Path, default=PROJECT / "build/hyprveil-upgrade-guard.so")
    parser.add_argument("--old-plugin", type=Path, default=OLD_DEFAULT)
    parser.add_argument("--old-sha256", choices=sorted(OLD_PINS), help="optional explicit old-release digest in addition to the built-in allowlist")
    parser.add_argument("--watcher", type=Path, help="optional independent GUI privacy-watch helper for its additional stream assertions")
    run = UpgradeSmoke(parser.parse_args())
    def interrupted(signum, frame):
        raise KeyboardInterrupt("upgrade smoke interrupted")
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
                      "lab_stopped": run.report.get("lab_stopped")}), flush=True)
    return 0 if run.report["ok"] and run.report.get("lab_stopped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
