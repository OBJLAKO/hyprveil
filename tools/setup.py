#!/usr/bin/env python3
"""Build, check and install Hyprveil without an Omarchy dependency."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import install
import service
import upgrade

PROJECT = Path(__file__).resolve().parents[1]
TESTED_ABI = "efb50993780079460b0cbed1363e2166a2de1d9f_aq_0.15_hu_0.14_hg_0.5_hc_0.1_hlg_0.6"


def command(argv, timeout=180):
    result = subprocess.run(argv, cwd=PROJECT, check=False, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise service.Refused("command failed: " + argv[0] + ": " + result.stderr.strip()[-1200:])
    return result.stdout.strip()


def build():
    print("Building Hyprveil and checking safety boundaries…", file=sys.stderr)
    # Installed compositor headers are outside the checkout's dependency list.
    # Rebuild before measuring ABI/release pins rather than trusting old targets.
    command(["make", "-B", "all", "upgrade-guard", "build/abi-probe", "test-session-guard", "test-png-guard",
             "test-privacy-policy", "test-spoiler-pattern", "test-appearance", "test-native-config"])


def preflight(signature=None):
    plugin = PROJECT / "build/hyprveil.so"
    guard = PROJECT / "build/hyprveil-upgrade-guard.so"
    probe = PROJECT / "build/abi-probe"
    for path in (plugin, guard, probe):
        install.read_owned(path)
    header_abi = command([str(probe)], timeout=5)
    if header_abi != TESTED_ABI:
        raise service.Refused("these Hyprland headers are not supported by this release; no configuration was changed")
    ctl = service.Controller(signature=signature)
    # Use exact measured admission pins, never implicit PATH plugin loading.
    measured = {"version": 1, "enabled": True, "plugin": str(plugin), "plugin_sha256": service.digest_file(plugin),
                "compositor_sha256": service.digest_file(Path("/usr/bin/Hyprland")), "abi_hash": header_abi}
    with ctl.locked():
        ctl.settings = service.manifest(measured)
        ctl.connect()
        ctl.version()
        if service.digest_file(Path(f"/proc/{ctl.target.pid}/exe"), process_executable=True) != measured["compositor_sha256"]:
            raise service.Refused("running Hyprland differs from the installed binary; log in again before installation")
        if ctl.raw("configerrors"):
            raise service.Refused("resolve current Hyprland configuration errors before installation")
        if any(item.get("name") == "hyprveil-upgrade-guard" for item in ctl.query("-j", "plugin", "list")):
            raise service.Refused("an unfinished protected update is holding capture; inspect its protected-upgrade report before retrying installation")
        matches = ctl.plugins()
        old_sha = None
        if matches:
            previous = service.Controller(signature=ctl.signature)
            previous.settings = service.manifest(json.loads(service.read_private(previous.config)))
            previous.connect()
            previous.version()
            if not previous.active():
                raise service.Refused("loaded Hyprveil is not the attested installed release")
            if service.digest_file(Path(previous.settings["plugin"]), True) != previous.settings["plugin_sha256"]:
                raise service.Refused("installed release differs from its receipt")
            old_sha = previous.settings["plugin_sha256"]
            if old_sha != measured["plugin_sha256"] and old_sha not in upgrade.OLD_PINS:
                raise service.Refused("this predecessor cannot be upgraded safely in the running session")
        measured.update(pid=ctl.target.pid, signature=ctl.signature, old_sha256=old_sha,
                        guard_sha256=service.digest_file(guard))
        return measured


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "check"))
    parser.add_argument("--signature", help="select an exact Hyprland instance")
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            build()
        pins = preflight(args.signature)
        if args.action == "check":
            print(json.dumps({"compatible": True, "loaded": pins["old_sha256"] is not None,
                              "online_upgrade": pins["old_sha256"] not in (None, pins["plugin_sha256"])}))
            return 0
        if pins["old_sha256"] not in (None, pins["plugin_sha256"]):
            # Upgrade uses the caller's attested instance environment.
            os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = pins["signature"]
            result = upgrade.upgrade(SimpleNamespace(old_sha256=pins["old_sha256"], plugin_sha256=pins["plugin_sha256"],
                                                     guard_sha256=pins["guard_sha256"]))
        else:
            result = install.Installer(SimpleNamespace(plugin=PROJECT / "build/hyprveil.so", plugin_sha256=pins["plugin_sha256"],
                compositor_sha256=pins["compositor_sha256"], abi_hash=pins["abi_hash"], signature=pins["signature"], pid=pins["pid"])).run()
        print(json.dumps({"installed": True, "online_upgrade": pins["old_sha256"] not in (None, pins["plugin_sha256"]),
                          "settings": str(Path.home() / ".config/hypr/hyprveil-settings.lua"),
                          "activation": "current session" if pins["old_sha256"] is not None else "next login",
                          "report": result.get("report")}))
        return 0
    except (OSError, ValueError, service.Refused, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
