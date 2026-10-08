#!/usr/bin/env python3
"""Own an isolated lab and test protected GTK popup/dialog capture rendering.

Example:
  python tools/popup_smoke.py --parent-runtime /run/user/1000 --parent-display wayland-1

Only the explicit parent Wayland connection is used to launch a nested lab.
Every mutation and capture thereafter targets the private lab sockets. The
owned lab is stopped in finally, including failed tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time

from lab import PROJECT, lab_env, load_lab

sys.path.insert(0, str(PROJECT / "tests"))
from verify_capture import Image, close, color_fraction, differing_fraction, geometry_report, opacity_fraction


PRIVATE = (232, 64, 144)


def outside_pixels(image: Image, excluded_rect) -> int:
    x, y, width, height = excluded_rect
    count = 0
    for index, pixel in enumerate(image.pixels((0, 0, image.width, image.height))):
        column, row = index % image.width, index // image.width
        if not (x <= column < x + width and y <= row < y + height) and close(pixel, PRIVATE, 3):
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    parser.add_argument("--inherit-dialog", action="store_true",
                        help="test effective ancestor protection with the dialog's native flag unset")
    parser.add_argument("--parent-lifecycle", action="store_true",
                        help="also keep the inherited dialog visible across parent unmap and destruction")
    parser.add_argument("--privacy-debug", action="store_true",
                        help="save the native lab-only fixture ancestry/policy diagnostic")
    args = parser.parse_args()
    if args.parent_lifecycle and not args.inherit_dialog:
        parser.error("--parent-lifecycle requires --inherit-dialog")
    launcher = None
    processes = []
    state = None
    artifacts = None
    loaded = False
    checks = []
    env = None
    report = {"ok": False, "checks": checks,
              "dialog_policy": "inherited" if args.inherit_dialog else "independent-native-rule",
              "parent_lifecycle_requested": args.parent_lifecycle,
              "parent_lifecycle_completed": False,
              "privacy_debug_requested": args.privacy_debug}

    def command(argv, timeout=15):
        completed = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=timeout)
        if completed.returncode:
            raise RuntimeError(f"{argv}: {completed.stdout} {completed.stderr}")
        return completed.stdout.strip()

    def ctl(*argv):
        load_lab(state["runtime_dir"])
        result = command(["hyprctl", "-i", state["signature"], *argv])
        if result.startswith(("err", "Invalid", "unknown", "Couldn")):
            raise RuntimeError(f"hyprctl {argv}: {result}")
        return result

    def check(name, ok, **details):
        item = {"name": name, "ok": bool(ok), **details}
        checks.append(item)
        print(json.dumps(item), flush=True)
        if not ok:
            raise RuntimeError("assertion failed: " + name)

    def clients():
        return json.loads(ctl("-j", "clients"))

    def target(app_id=None, title=None):
        matches = [item for item in clients() if (app_id is None or item.get("class") == app_id)
                   and (title is None or item.get("title") == title)]
        if len(matches) != 1 or not matches[0].get("mapped"):
            raise RuntimeError(f"expected one mapped {app_id or title}, got {len(matches)}")
        return matches[0]

    def spawn_fixture(script, ready_name, extra=()):
        log = (artifacts / (ready_name + ".log")).open("w")
        process = subprocess.Popen([sys.executable, str(PROJECT / "tests/fixtures" / script),
                                    "--lab-dir", str(runtime), "--wayland-display", state["wayland_display"],
                                    *extra], env=dict(env, WAYLAND_DEBUG="1") if script == "popup.py" else env,
                                   stdout=log, stderr=subprocess.STDOUT)
        processes.append((process, log))
        return process

    def wait_until(predicate, description, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for process, log in processes:
                if process.poll() is not None:
                    raise RuntimeError(f"fixture exited {process.returncode}: {Path(log.name).read_text()}")
            result = predicate()
            if result:
                return result
            time.sleep(0.1)
        raise RuntimeError("timed out waiting for " + description)

    def popup_state():
        path = runtime / "popup-ready.json"
        return json.loads(path.read_text()) if path.exists() else None

    def control(command_name, predicate=lambda value: value.get("ready")):
        ready_path = runtime / "popup-ready.json"
        previous = ready_path.stat().st_mtime_ns if ready_path.exists() else 0
        fd = os.open(runtime / "popup-control.fifo", os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.write(fd, (command_name + "\n").encode("ascii"))
        finally:
            os.close(fd)
        def updated():
            if ready_path.exists() and ready_path.stat().st_mtime_ns > previous:
                value = popup_state()
                if value.get("error"):
                    raise RuntimeError("fixture control failed: " + value["error"])
                return value if predicate(value) else None
            return None
        return wait_until(updated, "popup command " + command_name)

    def capture(name):
        ctl("eval", 'hl.dispatch(hl.dsp.cursor.move({x=16,y=16}))')
        time.sleep(0.2)
        path = artifacts / name
        command(["grim", "-o", "HV-POPUP-TEST", str(path)])
        return path

    def sample(name, popup_expected=None, dialog_expected=False, main_expected=True,
               export_dialog_public=False):
        current = control("status")
        (artifacts / (name + ".fixture.json")).write_text(json.dumps(current, indent=2) + "\n")
        (artifacts / (name + ".clients.json")).write_text(json.dumps(clients(), indent=2) + "\n")
        if args.privacy_debug:
            debug = json.loads(ctl("hyprveil", "fixture-privacy"))
            (artifacts / (name + ".privacy.json")).write_text(json.dumps(debug, indent=2) + "\n")
        check(name + ": main mapping reported", current["main"]["mapped"] == main_expected,
              main=current["main"], parent_destroyed=current["parent_destroyed"])
        if main_expected:
            parent = target(title="Hyprveil fixture: protected popup parent")
            check(name + ": parent native privacy remains enabled",
                  ctl("getprop", "address:" + parent["address"], "no_screen_share") == "true",
                  address=parent["address"])
        if popup_expected is not None:
            check(name + ": popup mapping reported", current["popup"]["mapped"] == popup_expected,
                  fixture=current)
        if dialog_expected:
            check(name + ": dialog mapping reported", current["dialog"]["mapped"], fixture=current)
            if main_expected:
                check(name + ": GTK transient points at the live parent", current["dialog_transient_for_parent"],
                      destroy_with_parent=current["dialog_destroy_with_parent"])
            # GTK associates a transient with its parent's app-id after initial
            # realization. Select this synthetic dialog by its stable title and
            # verify its own native flag before any rendering assertion. The
            # ancestor test must not accidentally get an independent rule.
            dialog = target(title="Hyprveil fixture: transient dialog")
            dialog_address = "address:" + dialog["address"]
            dialog_protected = ctl("getprop", dialog_address, "no_screen_share") == "true"
            check(name + (": dialog native flag remains unset" if args.inherit_dialog else
                          ": native policy covers the separate dialog"),
                  dialog_protected == (not args.inherit_dialog),
                  native_protection=dialog_protected,
                  actual_class=dialog["class"], address=dialog["address"])
        ctl("hyprveil", "dump-local")
        captured_path = capture(name + ".capture.png")
        local_path = artifacts / (name + ".local.png")
        shutil.copyfile(runtime / "hyprveil-local.png", local_path)
        local, captured = Image(local_path), Image(captured_path)
        full_rect = (0, 0, captured.width, captured.height)
        leakage = color_fraction(captured, full_rect, PRIVATE, 3)
        local_body = color_fraction(local, body_rect, PRIVATE, 3)
        revealed = differing_fraction(captured, body_rect, underlay, body_rect, 3)
        local_underlay = differing_fraction(local, body_rect, underlay, body_rect, 3)
        check(name + (": main remains local and export reveals underlay" if main_expected else
                      ": unmapped parent leaves the background visible"),
              (local_body >= 0.99 if main_expected else local_underlay <= 0.001) and revealed <= 0.001,
              local_protected_fraction=local_body, capture_underlay_difference=revealed,
              local_underlay_difference=local_underlay)
        check(name + (": explicit reset makes dialog pixels public" if export_dialog_public else
                      ": no protected pixels anywhere in capture"),
              leakage > 0 if export_dialog_public else leakage == 0,
              capture_protected_fraction=leakage)
        if popup_expected is not None:
            external = outside_pixels(local, main_rect)
            check(name + ": local popup outside main rectangle",
                  external > 1000 if popup_expected else external == 0,
                  magenta_pixels_outside_main=external, main_rect=main_rect)
        if dialog_expected:
            dx, dy = dialog["at"]
            dw, dh = dialog["size"]
            dialog_rect = (dx + 8, dy + 8, dw - 16, dh - 16)
            dialog_local = color_fraction(local, dialog_rect, PRIVATE, 3)
            dialog_exported = color_fraction(captured, dialog_rect, PRIVATE, 3)
            check(name + ": dialog remains locally visible with expected exported content",
                  dialog_local >= 0.99 and (dialog_exported >= 0.99 if export_dialog_public else
                                           dialog_exported == 0),
                  local_dialog_fraction=dialog_local, native_protection=dialog_protected,
                  exported_dialog_fraction=dialog_exported,
                  dialog_rect=dialog_rect, effective_policy="explicitly-public" if export_dialog_public else
                  report["dialog_policy"])
            direct_path = artifacts / (name + ".window.png")
            command(["grim", "-T", dialog["stableId"], str(direct_path)])
            direct = Image(direct_path)
            direct_rect = (0, 0, direct.width, direct.height)
            direct_color = color_fraction(direct, direct_rect, PRIVATE if export_dialog_public else (0, 0, 0),
                                          3 if export_dialog_public else 0)
            direct_opacity = opacity_fraction(direct, direct_rect)
            check(name + (": direct dialog capture is intentionally public" if export_dialog_public else
                          ": direct dialog capture is opaque black"),
                  direct_color == 1 and direct_opacity == 1 and (direct.width, direct.height) == (dw, dh),
                  stable_id=dialog["stableId"], native_protection=dialog_protected,
                  expected_color_fraction=direct_color, opacity_fraction=direct_opacity,
                  captured_size=[direct.width, direct.height], expected_size=[dw, dh])
        return current

    try:
        print("starting a dedicated popup lab", flush=True)
        launcher = subprocess.Popen([sys.executable, str(PROJECT / "tools/lab.py"), "run",
                                     "--parent-runtime", args.parent_runtime, "--parent-display", args.parent_display],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            readable, _, _ = select.select([launcher.stdout], [], [], 0.2)
            if readable:
                line = launcher.stdout.readline()
                if line:
                    value = json.loads(line)
                    if value.get("ready"):
                        state = value
                        break
            if launcher.poll() is not None:
                raise RuntimeError("lab startup failed: " + launcher.stderr.read())
        if state is None:
            raise RuntimeError("lab startup did not report an isolated runtime")
        runtime, state = load_lab(state["runtime_dir"])
        env = lab_env(runtime, state)
        artifacts = Path(state["artifacts"])
        report["lab"] = state
        print(json.dumps({"lab": str(runtime), "artifacts": str(artifacts)}), flush=True)
        plugin = artifacts / "hyprveil-popup-test.so"
        shutil.copyfile(args.plugin, plugin)
        plugin.chmod(0o500)
        report["plugin_sha256"] = hashlib.sha256(plugin.read_bytes()).hexdigest()
        ctl("output", "create", "headless", "HV-POPUP-TEST")
        for monitor in json.loads(ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                ctl("output", "remove", monitor["name"])
        config = runtime / "hyprland.lua"
        with config.open("a") as file:
            file.write('\n-- Dedicated synthetic transient dialog geometry and optional policy.\n'
                       '-- GTK can associate a transient with the parent app-id.\n'
                       'hl.window_rule({ match = { title = "^Hyprveil fixture: transient dialog$" }, '
                       + ('' if args.inherit_dialog else 'no_screen_share = true, ')
                       + 'size = {180,120}, move = {760,520} })\n')
        ctl("reload")
        errors = ctl("configerrors")
        if errors:
            raise RuntimeError("lab configuration errors: " + errors)
        spawn_fixture("client.py", "background", ("--role", "background", "--ready-file", str(runtime / "background.json")))
        wait_until(lambda: [item for item in clients() if item.get("class") == "org.hyprveil.fixture.background"], "background mapping")
        time.sleep(0.3)
        underlay_path = capture("popup-underlay.png")
        underlay = Image(underlay_path)
        spawn_fixture("popup.py", "popup")
        wait_until(lambda: (value if (value := popup_state()) and value.get("ready") and value["popup"]["mapped"] else None), "visible initial popup")
        # The first xdg_popup configure can race the main widget's first draw,
        # when its final anchor has not been allocated yet. Reopen it using the
        # current anchor before establishing the outside-parent baseline.
        control("popup-hide", lambda value: value.get("ready") and not value["popup"]["mapped"])
        control("popup-show", lambda value: value.get("ready") and value["popup"]["mapped"])
        main = target("org.hyprveil.fixture.protected")
        main_rect = (*main["at"], *main["size"])
        x, y, width, height = main_rect
        body_rect = (x + 16, y + 16, width - 32, height - 32)
        before = artifacts / "popup-clients.before.json"
        original_clients = clients()
        original_addresses = {item["address"] for item in original_clients}
        before.write_text(json.dumps(original_clients, indent=2) + "\n")
        baseline = Image(capture("popup-unprotected.png"))
        external = outside_pixels(baseline, main_rect)
        check("unprotected popup is visible outside main rectangle", external > 1000,
              magenta_pixels_outside_main=external, main_rect=main_rect, fixture=popup_state())
        address = "address:" + main["address"]
        ctl("eval", 'hl.dispatch(hl.dsp.window.tag({window=' + json.dumps(address) + ',tag="+hyprveil-private-test"}))')
        check("main native protection is enabled", ctl("getprop", address, "no_screen_share") == "true")
        response = ctl("plugin", "load", str(plugin))
        if response != "ok":
            raise RuntimeError("plugin load: " + response)
        loaded = True
        ctl("hyprveil", "omit")
        check("main native protection survives plugin load", ctl("getprop", address, "no_screen_share") == "true")
        # Loading a native plugin may reload config and dismiss GTK popovers.
        # Request and verify the current popup, rather than trust startup JSON.
        control("popup-show", lambda value: value.get("ready") and value["popup"]["mapped"])
        sample("popup-open", popup_expected=True)
        control("popup-hide", lambda value: value.get("ready") and not value["popup"]["mapped"])
        sample("popup-hidden", popup_expected=False)
        control("popup-show", lambda value: value.get("ready") and value["popup"]["mapped"])
        sample("popup-shown-again", popup_expected=True)
        control("dialog-show", lambda value: value.get("ready") and value["dialog"]["mapped"])
        sample("inherited-transient-dialog" if args.inherit_dialog else "protected-transient-dialog",
               dialog_expected=True)
        after = artifacts / "popup-clients.after.json"
        after_clients = clients()
        (artifacts / "popup-clients.all-after.json").write_text(json.dumps(after_clients, indent=2) + "\n")
        # Compare the windows that existed before the transient was created.
        # Address identity remains part of geometry_report's required fields.
        after.write_text(json.dumps([item for item in after_clients if item["address"] in original_addresses], indent=2) + "\n")
        for app_id in ("org.hyprveil.fixture.protected", "org.hyprveil.fixture.background"):
            result = geometry_report(before, after, app_id)
            check(app_id + " geometry preserved", result["ok"], changed=result["changed"])
        if args.parent_lifecycle:
            fields = ("address", "at", "size", "workspace", "monitor", "floating", "fullscreen")
            original_dialog = target(title="Hyprveil fixture: transient dialog")
            dialog_geometry = {key: original_dialog[key] for key in fields}
            for operation in ("parent-hide", "parent-close"):
                control(operation, lambda value: value.get("ready") and not value["main"]["mapped"]
                        and value["dialog"]["mapped"] and
                        (operation != "parent-close" or value["parent_destroyed"]))
                sample("inherited-dialog-after-" + operation, dialog_expected=True, main_expected=False)
                current_dialog = target(title="Hyprveil fixture: transient dialog")
                current_geometry = {key: current_dialog[key] for key in fields}
                check(operation + ": same dialog and geometry survive", current_geometry == dialog_geometry,
                      before=dialog_geometry, after=current_geometry)
            # An intentional native false setter releases only this orphan's
            # retained effective policy. Its own native flag is false both
            # before and after; the exported pixels must visibly change.
            ctl("eval", 'hl.dispatch(hl.dsp.window.set_prop({window=' +
                json.dumps("address:" + original_dialog["address"]) + ',prop="no_screen_share",value="0"}))')
            sample("orphan-dialog-explicitly-public", dialog_expected=True, main_expected=False,
                   export_dialog_public=True)
            current_dialog = target(title="Hyprveil fixture: transient dialog")
            check("explicit reset preserves dialog geometry", dialog_geometry ==
                  {key: current_dialog[key] for key in fields})
            report["parent_lifecycle_completed"] = True
        report["plugin_status"] = json.loads(ctl("hyprveil", "status"))
        report["compositor"] = json.loads(ctl("-j", "version"))
        report["capture_tool"] = "grim"
        report["ok"] = True
        return_code = 0
    except Exception as error:
        report["error"] = str(error)
        if args.privacy_debug and loaded:
            try:
                report["failure_privacy_debug"] = json.loads(ctl("hyprveil", "fixture-privacy"))
            except Exception as diagnostic_error:
                report["failure_privacy_debug_error"] = str(diagnostic_error)
        print("popup smoke failed: " + str(error), file=sys.stderr, flush=True)
        return_code = 1
    finally:
        cleanup_errors = []
        if loaded:
            try:
                ctl("plugin", "unload", str(plugin))
            except Exception as error:
                cleanup_errors.append("plugin unload: " + str(error))
        for process, log in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            log.close()
        if launcher is not None:
            if launcher.poll() is None:
                launcher.terminate()
                try:
                    launcher.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    launcher.kill()
                    launcher.wait()
            launcher.stdout.close()
            launcher.stderr.close()
        report["lab_stopped"] = state is None or not Path(f"/proc/{state['pid']}").exists()
        report["cleanup_errors"] = cleanup_errors
        if cleanup_errors or not report["lab_stopped"]:
            report["ok"] = False
            return_code = 1
        if artifacts is not None:
            trace = artifacts / "popup.log"
            report["wayland_set_parent_requests"] = [line for line in trace.read_text().splitlines()
                if "xdg_toplevel" in line and ".set_parent(" in line] if trace.exists() else []
            path = artifacts / "popup-report.json"
            path.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps({"ok": report["ok"], "report": str(path),
                              "lab_stopped": report["lab_stopped"]}), flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
