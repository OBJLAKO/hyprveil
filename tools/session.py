#!/usr/bin/python3
"""Control a deliberately pinned manual Hyprveil trial; never autoload a plugin."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import service
from install import atomic, encoded, ensure, read_owned, sha

PROJECT = Path(__file__).resolve().parents[1]
STATE = PROJECT / "artifacts/live-session.json"


def controller_for(state):
    if not isinstance(state, dict) or type(state.get("pid")) is not int or state["pid"] <= 0 or \
       not isinstance(state.get("signature"), str) or not isinstance(state.get("runtime"), str) or \
       not isinstance(state.get("abi_hash"), str) or not state["abi_hash"]:
        raise service.Refused("invalid trial identity")
    controller = service.Controller(signature=state["signature"])
    controller.runtime = Path(state["runtime"])
    if controller.runtime != service.standard_runtime() or controller.runtime.resolve() != controller.runtime:
        raise service.Refused("trial must target the user's standard runtime")
    fd = service.check_directory(controller.runtime)
    os.close(fd)
    controller.settings = {"abi_hash": state["abi_hash"]}
    return controller


def verify(state, check_abi=True):
    controller = controller_for(state)
    controller.connect()
    if controller.target.pid != state["pid"] or "started" in state and state["started"] != controller.target.started:
        raise service.Refused("pinned compositor process identity changed")
    if check_abi:
        controller.version()
    return controller


def ctl(state, *argv):
    return verify(state, check_abi=False).raw(*argv)


def read_state():
    contents, mode = read_owned(STATE, 65536)
    if mode != 0o600:
        raise service.Refused("trial state must be owned, regular, single-link and mode 0600")
    value = json.loads(contents)
    if not isinstance(value, dict):
        raise service.Refused("invalid trial state")
    return value


def load_state(check_abi=True):
    state = read_state()
    verify(state, check_abi)
    return state


def release_settings(controller, state):
    plugin = state.get("plugin")
    digest = state.get("plugin_sha256")
    if not isinstance(plugin, str) or not Path(plugin).is_absolute() or any(c.isspace() for c in plugin) or "\x00" in plugin or \
       not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise service.Refused("invalid pinned trial release")
    expected_marker = str(controller.runtime / f".hyprveil-live-{state['pid']}")
    if state.get("marker") != expected_marker:
        raise service.Refused("trial marker does not match the exact pinned session")
    controller.settings.update(plugin=plugin, plugin_sha256=digest,
                               appearance=service.validate_appearance(state.get("appearance", service.DEFAULT_APPEARANCE)))


def prepare(args):
    state = {"pid": args.pid, "signature": args.signature, "runtime": str(service.standard_runtime()),
             "abi_hash": args.abi_hash, "created": time.time(), "autoload": False,
             "appearance": dict(service.DEFAULT_APPEARANCE)}
    controller = controller_for(state)
    with controller.locked():
        try:
            previous = read_state()
        except FileNotFoundError:
            previous = None
        if previous is not None:
            # Stale identity is recoverable; loaded modules in an answering
            # old compositor must be dealt with before replacing trial STATE.
            try:
                old = verify(previous)
            except (OSError, service.Refused, ValueError, KeyError, TypeError):
                old = None
            if old is not None and old.plugins():
                raise service.Refused("unload the existing Hyprveil trial before preparing another")
        controller = verify(state)
        if controller.plugins():
            raise service.Refused("unload the existing Hyprveil plugin before preparing a trial")
        source = PROJECT / "build/hyprveil.so"
        if source.resolve(strict=True) != source:
            raise service.Refused("trial release source must not contain symlinks")
        binary, _ = read_owned(source)
        ensure(STATE.parent)
        target = Path(tempfile.mkdtemp(prefix=f"live-{args.pid}-", dir=STATE.parent))
        target.chmod(0o700)
        plugin = target / "hyprveil.so"
        atomic(plugin, binary, 0o400)
        state.update(started=controller.target.started, plugin=str(plugin), plugin_sha256=sha(binary),
                     compositor_sha256=service.digest_file(Path(f"/proc/{args.pid}/exe"), process_executable=True),
                     marker=str(controller.marker_path()))
        atomic(target / "preflight.json", encoded({"state": state,
               "version": controller.version(), "plugins": controller.plugins(), "config_errors": controller.raw("configerrors")}), 0o600)
        for name in ("hyprland.lua", "screen-privacy.lua", "omarchy_fx.lua"):
            source = Path.home() / ".config/hypr" / name
            try:
                contents, _ = read_owned(source, 1024 * 1024)
            except FileNotFoundError:
                continue
            atomic(target / name, contents, 0o600)
        controller.verify_identity()
        controller.remove_marker()
        expires = int(time.time()) + 7200
        text = f"hyprveil-live-v1\n{args.pid}\n{args.signature}\nblack\n{expires}\n"
        directory = service.check_directory(controller.runtime)
        try:
            fd = os.open(controller.marker_path().name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
            with os.fdopen(fd, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(directory)
        state["marker_expires"] = expires
        try:
            atomic(STATE, encoded(state), 0o600)
        except (OSError, service.Refused):
            controller.remove_marker()
            raise
        return {"prepared": True, "state": str(STATE), "plugin": str(plugin)}


def execute(state, action, image=None, appearance=None):
    controller = controller_for(state)
    with controller.locked():
        if action == "configure":
            # Re-read after acquiring the same lock as live controller commands
            # so separate partial edits cannot discard a previous saved field.
            current = read_state()
            if any(current.get(field) != state.get(field) for field in ("pid", "signature", "started", "plugin", "plugin_sha256")):
                raise service.Refused("pinned trial changed while waiting for its lock")
            state = current
        controller = verify(state, check_abi=action == "load")
        release_settings(controller, state)
        loaded = controller.active()
        if action == "load":
            if loaded:
                raise service.Refused("Hyprveil is already loaded")
            plugin = Path(state["plugin"])
            if plugin.resolve(strict=True) != plugin or service.digest_file(plugin, True) != state["plugin_sha256"]:
                raise service.Refused("trial binary changed or contains symlinks")
            pin = state.get("compositor_sha256")
            if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin):
                raise service.Refused("old trial has no compositor ELF pin; prepare a fresh trial")
            if service.digest_file(Path(f"/proc/{state['pid']}/exe"), process_executable=True) != pin or \
               service.digest_file(Path("/usr/bin/Hyprland")) != pin:
                raise service.Refused("compositor ELF changed; prepare a fresh trial")
            controller.verify_identity()
            # The permission must already exist from initial config or be
            # granted by explicit compositor consent. Runtime hl.permission
            # via eval/reload is a no-op after startup in Hyprland 0.56.
            try:
                if controller.raw("plugin", "load", str(plugin)) != "ok" or not controller.active():
                    raise service.Refused("trial plugin load was not confirmed")
                value = controller.native("status")
                if value["mode"] != "black":
                    raise service.Refused("new trial plugin did not start in black mode")
                return controller.native_appearance(controller.settings["appearance"])
            except (OSError, service.Refused):
                controller.fallback_black()
                # Cancel admission if an asynchronous permission prompt is
                # answered after the controller times out or exits.
                controller.remove_marker()
                raise
        if action == "unload":
            if loaded:
                controller.native("black")
                if controller.raw("plugin", "unload", state["plugin"]) != "ok" or controller.plugins():
                    raise service.Refused("trial plugin unload was not confirmed")
            controller.remove_marker()
            return {"loaded": False, "mode": "native"}
        if not loaded:
            if action == "status":
                return {"loaded": False}
            raise service.Refused("the exact pinned trial plugin is not loaded")
        try:
            if action == "configure":
                current = controller.native("status")
                base = current["appearance"] if current.get("config_api") == 1 else controller.settings["appearance"]
                requested = service.merge_appearance(base, appearance if appearance is not None else {})
                status = controller.native_appearance(requested)
                updated = dict(state, appearance=requested)
                atomic(STATE, encoded(updated), 0o600)
                return status
            chosen = controller.safe_image(str(Path(image).absolute())) if action == "image" else None
            return controller.native(action, chosen)
        except (OSError, service.Refused):
            if action != "status":
                controller.fallback_black()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("prepare")
    start.add_argument("--pid", type=int, required=True)
    start.add_argument("--signature", required=True)
    start.add_argument("--abi-hash", required=True)
    for action in ("load", "status", "omit", "black", "spoiler", "unload"):
        sub.add_parser(action)
    image = sub.add_parser("image")
    image.add_argument("path", type=Path)
    service.appearance_arguments(sub.add_parser("configure"))
    args = parser.parse_args(argv)
    try:
        value = prepare(args) if args.action == "prepare" else execute(read_state(), args.action, getattr(args, "path", None),
                                                                    service.appearance_patch(args) if args.action == "configure" else None)
        print(json.dumps(value))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
        print("session: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
