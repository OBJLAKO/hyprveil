#!/usr/bin/python3
"""Control a Hyprpm-loaded Hyprveil through its public native API."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import service


class Controller(service.Controller):
    """Reuse attested IPC and literal Lua persistence without legacy receipts."""

    def __init__(self, signature=None):
        super().__init__(signature=signature)
        self.settings = {"abi_hash": service.TESTED_ABI}

    def active(self):
        # Hyprpm owns the plugin path and versioned build. The native module
        # independently enforces its reviewed ABI and standard-session guard.
        return bool(self.plugins())

    def native(self, action, image=None):
        status = super().native(action, image)
        if status.get("config_api") != 1:
            raise service.Refused("this command requires Hyprveil native configuration API 1")
        return status

    def record(self, status):
        # No legacy installation manifest or controller journal is needed:
        # Hyprpm handles module loading, while native status is authoritative.
        pass

    def persistence(self):
        try:
            contents, _ = service.read_lua_settings(self.lua_path)
            service.parse_lua_settings(contents)
        except FileNotFoundError:
            return False, "no managed Lua settings file; changes affect the current session"
        except (OSError, ValueError, service.Refused):
            return False, "Lua settings are unsafe or use custom code; use --runtime or edit the file"
        return True, None

    def optional_plan(self, current, patch, persist):
        if not persist:
            return None
        try:
            return self.prepare_lua(current, **patch)
        except FileNotFoundError:
            return None

    def update(self, patch, current, persist):
        # Refuse unsafe/custom persistence before changing the native state.
        # Missing settings are supported: callers receive persisted=false.
        patch = service.normalize_config_patch(patch, current)
        plan = self.optional_plan(current, patch, persist)
        try:
            status = self.native_configure(patch, current)
            if plan is not None:
                status = self.persist_lua(plan)
            return dict(status, persisted=plan is not None)
        except (OSError, ValueError, service.Refused):
            if patch and set(patch) <= set(service.NOTIFICATION_FIELDS):
                try:
                    status = self.native("status")
                    self.native_configure({"notification_mode": "omit"}, status)
                except (OSError, ValueError, service.Refused):
                    pass
            else:
                self.fallback_black()
            raise

    def run(self, action, image=None, appearance=None, persist=True):
        # Validate user patches before any IPC, even when no plugin is loaded.
        if action == "configure":
            service.normalize_config_patch(appearance if appearance is not None else {})
        elif appearance is not None:
            service.normalize_config_patch(appearance)
        with self.locked():
            self.connect()
            loaded = self.active()
            current = None
            if loaded:
                self.version()
                current = self.native("status")
            if action == "status":
                supported, reason = self.persistence()
                values = service.native_values(current) if loaded else dict(image_path="", **service.DEFAULT_RENDER)
                return {"loaded": loaded, "enabled": loaded,
                        "load_supported": False,
                        "desired_mode": current["mode"] if loaded else "black",
                        "appearance": current["appearance"] if loaded else dict(service.DEFAULT_APPEARANCE),
                        **{key: values[key] for key in ("image_path", *service.DEFAULT_RENDER)},
                        "config_file": str(self.lua_path), "status": current,
                        "persistence_supported": supported, "persistence_reason": reason}
            if not loaded:
                raise service.Refused("Hyprveil is not loaded; enable it with hyprpm enable hyprveil")
            self.verify_identity()
            if action in ("toggle", "hide", "show", "reset-sharing"):
                return self.window_action(action)
            if action == "reload-config":
                return self.reload_config()
            if action == "configure":
                patch = appearance if appearance is not None else {}
                service.normalize_config_patch(patch, current)
            elif action in service.NATIVE_MODES:
                patch = dict(appearance or {}, mode=action)
                if action == "image":
                    try:
                        patch["image_path"] = str(self.safe_image(str(Path(image).absolute())))
                    except (OSError, ValueError, service.Refused):
                        self.fallback_black()
                        raise
            else:
                raise service.Refused("unknown native command")
            return self.update(patch, current, persist)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signature", help="exact Hyprland instance; otherwise use the caller's instance environment")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "toggle", "hide", "show", "reset-sharing", "reload-config"):
        commands.add_parser(action)
    for action in ("omit", "black", "color", "spoiler", "image", "configure", "notifications"):
        command = commands.add_parser(action)
        command.add_argument("--runtime", action="store_true", help="change this session without saving Lua settings")
        if action == "image":
            command.add_argument("path", type=Path)
            command.add_argument("--fit", choices=service.IMAGE_FITS)
        elif action == "color":
            command.add_argument("value", nargs="?", help="opaque #RRGGBB fill; omit to retain the saved mask color")
        elif action == "spoiler":
            command.add_argument("--shader-path", help="trusted absolute .frag file; empty restores the built-in spoiler")
        elif action in ("configure", "notifications"):
            service.appearance_arguments(command)
            if action == "notifications":
                command.add_argument("--enabled", choices=("on", "off"))
                command.add_argument("--mode", choices=service.NATIVE_MODES)
                command.add_argument("--namespace", help="exact layer-shell namespace")
                command.add_argument("--rounding", type=int, help="-1 inherits Hyprland; otherwise 0..64")
    args = parser.parse_args(argv)
    try:
        patch = service.appearance_patch(args) if args.action in ("configure", "notifications") else {}
        if args.action == "notifications":
            for key in ("mode", "namespace", "rounding"):
                if getattr(args, key) is not None:
                    patch[key] = getattr(args, key)
            if args.enabled is not None:
                patch["enabled"] = args.enabled == "on"
            patch = {"notification_" + key: value for key, value in patch.items()}
            args.action = "configure"
        if args.action == "color" and args.value is not None:
            patch["mask_color"] = args.value
        if args.action == "image" and args.fit is not None:
            patch["image_fit"] = args.fit
        if args.action == "spoiler" and args.shader_path is not None:
            patch["shader_path"] = args.shader_path
        result = Controller(args.signature).run(args.action, getattr(args, "path", None),
            patch or None,
            persist=not getattr(args, "runtime", False))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, service.Refused) as error:
        print(json.dumps({"error": str(error), "action": args.action}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
