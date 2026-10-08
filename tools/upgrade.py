#!/usr/bin/python3
"""One-time protected upgrade from an explicitly reviewed predecessor ELF.

Never capture the desktop. The separately tested native gate holds capture
copies across unload/load; its weak identity handoff preserves inherited
privacy. A failure after gate admission keeps capture held for recovery.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

import install
import service

OLD_PINS = {"fdf4c7d008af84e9c9e92d3d06bb2833cea77b41be54a2072a4101990bee0630",
            "cb787c8fc8001fcc822e2a4907d515e8fec087616245557d3244dba461d2dc30",
            "894ec2f1ef28c8f4f6d56d5fbde30c6ba13f01fd47a477adf746c13f7b4d916b",
            "6a9c071c38cef45eb5c179312e5af1e846405b4622578a78c8bf3afebbb55ba8",
            "0c792b967f3204b5302aaf151997fc972cff04ae07be69b8c43b81fe0e51b95e",
            "0426199c06472e4ae262972ea9d13d9fcea11229af55f7274057ce5e370e4196"}


def preserved_selection(controller):
    """Validate the active choice before any upgrade mutates its session."""
    status = controller.native("status")
    if status.get("config_api") == 1:
        actual = service.native_values(status)
        mode = actual["mode"]
        image = str(controller.safe_image(actual["image_path"])) if mode == "image" else actual["image_path"]
        return mode, image, {key: actual[key] for key in service.DEFAULT_APPEARANCE}
    mode = status["mode"]
    appearance = service.validate_appearance(
        status.get("appearance", controller.settings.get("appearance", service.DEFAULT_APPEARANCE)),
        canonical=True)
    image = controller.settings["image_path"] if mode == "image" else ""
    if mode == "image":
        # Older status has no image path. Only the explicitly persisted,
        # independently admitted image can be restored without guessing.
        image = str(controller.safe_image(image))
    return mode, image, appearance


def black_before_handoff(controller):
    """Override native file settings only while the capture gate is held."""
    black = controller.native("black")
    if black.get("mode") != "black" or controller.native("status").get("mode") != "black":
        raise service.Refused("new native configuration did not confirm black before privacy handoff")
    return black


def upgrade(args):
    if args.old_sha256 not in OLD_PINS:
        raise service.Refused("predecessor ELF has no reviewed migration bridge")
    old_sha = args.old_sha256
    project = Path(__file__).resolve().parents[1]
    plugin = project / "build/hyprveil.so"
    guard = project / "build/hyprveil-upgrade-guard.so"
    if service.digest_file(plugin) != args.plugin_sha256 or service.digest_file(guard) != args.guard_sha256:
        raise service.Refused("candidate binaries differ from the explicit tested pins")
    artifacts = project / "artifacts"
    install.ensure(artifacts)
    folder = Path(tempfile.mkdtemp(prefix="protected-upgrade-", dir=artifacts))
    folder.chmod(0o700)
    frozen_guard = folder / "hyprveil-upgrade-guard.so"
    install.atomic(frozen_guard, install.read_owned(guard)[0], 0o400)
    report = {"version": 1, "ok": False, "phase": "preflight", "capture_held": False,
              "old_sha256": old_sha, "new_sha256": args.plugin_sha256,
              "guard_sha256": args.guard_sha256, "report": str(folder / "report.json")}

    def record(phase):
        report["phase"] = phase
        install.atomic(folder / "report.json", install.encoded(report), 0o600)

    def gate(controller, *command):
        value = controller.query("hv-upgrade", *command)
        if not isinstance(value, dict) or "error" in value:
            raise service.Refused("capture gate refused: " + str(value))
        return value

    old = service.Controller()
    with old.locked():
        try:
            old.settings = service.manifest(json.loads(service.read_private(old.config)))
            if old.settings["plugin_sha256"] != old_sha or not old.settings["enabled"]:
                raise service.Refused("upgrade requires the exact enabled reviewed old release")
            old.connect()
            old.version()
            if not old.active() or service.digest_file(Path(old.settings["plugin"]), True) != old_sha:
                raise service.Refused("exact old release is not active and immutable")
            if service.digest_file(Path(f"/proc/{old.target.pid}/exe"), process_executable=True) != old.settings["compositor_sha256"]:
                raise service.Refused("running compositor ELF differs from tested pin")
            if service.digest_file(Path("/usr/bin/Hyprland")) != old.settings["compositor_sha256"]:
                raise service.Refused("installed compositor ELF differs from tested pin")
            if old.raw("configerrors"):
                raise service.Refused("existing compositor configuration errors")
            selected_mode, selected_image, selected_appearance = preserved_selection(old)
            selected_native_api = old.native("status").get("config_api") == 1
            report.update(selected_mode=selected_mode, selected_image=selected_image,
                          selected_appearance=selected_appearance)
            ledger_path = Path.home() / ".local/share/hyprveil/install.json"
            ledger = json.loads(service.read_private(ledger_path))
            if ledger.get("plugin_sha256") != old_sha or ledger.get("plugin") != old.settings["plugin"]:
                raise service.Refused("installed receipt does not attest the exact old release")
            for path, field in ((Path.home() / ".local/share/hyprveil/controller.py", "controller_sha256"),
                                (Path.home() / ".config/hypr/hyprveil.lua", "lua_sha256")):
                if install.sha(install.read_owned(path)[0]) != ledger.get(field):
                    raise service.Refused("managed file differs from its receipt: " + str(path))
            report.update(pid=old.target.pid, signature=old.signature, abi_hash=old.settings["abi_hash"],
                          compositor_sha256=old.settings["compositor_sha256"])
            for name, path in (("config.json", old.config),
                               ("controller.py", Path.home() / ".local/share/hyprveil/controller.py"),
                               ("install.json", Path.home() / ".local/share/hyprveil/install.json"),
                               ("hyprveil.lua", Path.home() / ".config/hypr/hyprveil.lua"),
                               ("hyprland.lua", Path.home() / ".config/hypr/hyprland.lua")):
                install.atomic(folder / name, install.read_owned(path)[0], 0o600)
            settings_path = Path.home() / ".config/hypr/hyprveil-settings.lua"
            try:
                settings_contents, settings_mode = install.read_owned(settings_path)
            except FileNotFoundError:
                # 0.3.1 has no native Lua settings source. Record absence so
                # recovery can distinguish a new file from an empty old file.
                report["lua_settings_backup"] = {"path": str(settings_path), "existed": False}
            else:
                install.atomic(folder / "hyprveil-settings.lua", settings_contents, 0o600)
                report["lua_settings_backup"] = {"path": str(settings_path), "existed": True,
                                                  "mode": settings_mode,
                                                  "sha256": install.sha(settings_contents)}
            new_path = Path.home() / ".local/share/hyprveil/releases" / args.plugin_sha256 / "hyprveil.so"
            module = install.lua_module(Path.home() / ".local/share/hyprveil/controller.py", new_path)
            original_main = install.read_owned(Path.home() / ".config/hypr/hyprland.lua")[0]
            install.validate_lua(module, install.main_config(original_main), folder)
            record("preflight-verified")
            old.native("black")
            old.create_marker()
            try:
                report["gate_load_attempted"] = True
                record("capture-gate-admission")
                if old.raw("plugin", "load", str(frozen_guard)) != "ok":
                    raise service.Refused("capture gate loading was refused")
            finally:
                old.remove_marker()
            report["capture_held"] = True
            held = gate(old, "status")
            if held.get("held") is not True or held.get("transferred") is not False or held.get("snapshot_refused") is not False:
                raise service.Refused("capture gate did not confirm hold")
            report["gate_before"] = held
            record("capture-held")
            old.verify_identity()
            if old.raw("plugin", "unload", old.settings["plugin"]) != "ok" or old.plugins():
                raise service.Refused("old release unload was not confirmed")
            record("old-unloaded-capture-held")
            next_controller = service.Controller(signature=old.signature)
            installer = install.Installer(SimpleNamespace(plugin=plugin, plugin_sha256=args.plugin_sha256,
                compositor_sha256=old.settings["compositor_sha256"], abi_hash=old.settings["abi_hash"],
                signature=old.signature, pid=old.target.pid))
            installer.preflight(next_controller)
            installer.report_dir = folder / "installation"
            installer.report_dir.mkdir(mode=0o700)
            installer.setup_files()
            next_controller.verify_identity()
            if next_controller.raw("reload") != "ok" or next_controller.raw("configerrors"):
                raise service.Refused("updated autoload configuration validation failed")
            installer.report.update(installed=True, loaded_by_installer=False, config_errors="")
            install.atomic(installer.report_dir / "report.json", install.encoded(installer.report), 0o600)
            next_controller.settings = service.manifest(json.loads(service.read_private(next_controller.config)))
            # In-memory admission choice only. Native initialization, status
            # attestation and handoff must all happen in sanitized black mode.
            next_controller.settings["desired_mode"] = "black"
            next_controller.start()
            if selected_native_api:
                # Preserve even an inactive path selected by runtime Lua. The
                # new release may have reread older file settings on startup.
                # Commit while held/black before the appearance save writes all
                # actual fields back to the managed settings block.
                next_controller.native("black")
                next_controller.native_configure({"image_path": selected_image}, next_controller.native("status"))
            next_controller.apply_appearance(selected_appearance, persist=True)
            # Native 0.4 evaluates its own Hyprland settings while loading.
            # They may select a decorative mode even when the controller's
            # temporary in-memory choice says black. Reassert and attest black
            # after all configuration work, immediately before adoption. The
            # exported adoption function independently rejects any other mode.
            report["black_before_handoff"] = black_before_handoff(next_controller)
            record("new-loaded-black-capture-held")
            transferred = gate(next_controller, "handoff", str(new_path), args.plugin_sha256)
            if transferred.get("held") is not True or transferred.get("transferred") is not True:
                raise service.Refused("weak privacy handoff was not confirmed")
            report["handoff"] = transferred
            # Restore and persist the prior active choice only after handoff.
            report["status"] = next_controller.apply_mode(selected_mode, selected_image, persist=True)
            next_controller.verify_identity()
            released = gate(next_controller, "release")
            if released.get("held") is not False or released.get("transferred") is not True:
                raise service.Refused("capture gate release was not confirmed")
            report["capture_held"] = False
            record("privacy-transferred-capture-resumed")
            if next_controller.raw("plugin", "unload", str(frozen_guard)) != "ok":
                raise service.Refused("temporary gate unload was not confirmed")
            plugins = next_controller.query("-j", "plugin", "list")
            if any(item.get("name") == "hyprveil-upgrade-guard" for item in plugins):
                raise service.Refused("temporary capture gate still loaded")
            if next_controller.raw("configerrors"):
                raise service.Refused("final compositor configuration errors")
            report.update(ok=True, status=next_controller.native("status"), temporary_gate_unloaded=True,
                          installed_config=json.loads(service.read_private(next_controller.config)), config_errors="")
            record("complete")
        except Exception as error:
            if report.get("gate_load_attempted") and not report.get("temporary_gate_unloaded"):
                try:
                    report["capture_held"] = gate(old, "status").get("held") is True
                except Exception:
                    report["capture_hold_unconfirmed"] = True
            report["error"] = str(error)
            record("failed-capture-held" if report["capture_held"] else "failed")
            # Never automatically drop a still-held gate or load an untested
            # fallback. The saved pins/phase/backups make recovery reviewable.
            raise
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-sha256", required=True, choices=sorted(OLD_PINS))
    parser.add_argument("--plugin-sha256", required=True)
    parser.add_argument("--guard-sha256", required=True)
    print(json.dumps(upgrade(parser.parse_args()), ensure_ascii=False))
