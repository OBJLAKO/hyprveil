#!/usr/bin/python3
"""Install an explicitly pinned per-user release; never load or unload plugins."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import uuid

import service

PROJECT = Path(__file__).resolve().parents[1]
LEGACY_BEGIN = "-- BEGIN HYPRVEIL MANAGED AUTOLOAD v1"
LEGACY_END = "-- END HYPRVEIL MANAGED AUTOLOAD v1"
LEGACY_BLOCK = LEGACY_BEGIN + '\nrequire("hypr.hyprveil")\n' + LEGACY_END
BEGIN = "-- BEGIN HYPRVEIL MANAGED AUTOLOAD v2"
END = "-- END HYPRVEIL MANAGED AUTOLOAD v2"
BLOCK = BEGIN + '\ndofile(os.getenv("HOME") .. "/.config/hypr/hyprveil.lua")\n' + END


def directory(path, private=False):
    if path.resolve() != path:
        raise service.Refused("installation directory must not contain symlinks: " + str(path))
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o022 or private and stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise service.Refused("installation directory has unsafe ownership or permissions: " + str(path))
    return fd


def ensure(path, private=False):
    if not path.exists():
        ensure(path.parent)
        path.mkdir(mode=0o700 if private else 0o755)
    fd = directory(path, private)
    os.close(fd)


def read_owned(path, limit=64 * 1024 * 1024):
    parent = directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > limit:
            raise service.Refused("unsafe installation file: " + str(path))
        result = bytearray()
        while chunk := os.read(fd, min(1024 * 1024, limit + 1 - len(result))):
            result.extend(chunk)
            if len(result) > limit:
                raise service.Refused("oversized installation file: " + str(path))
        return bytes(result), stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def atomic(path, contents, mode):
    parent = directory(path.parent)
    name = ".hyprveil-" + uuid.uuid4().hex + ".tmp"
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def sha(contents):
    return hashlib.sha256(contents).hexdigest()


def permission_pattern(plugin):
    # Hyprland uses RE2::FullMatch for config permission binaries. Escape all
    # regex operators; a .local or .so must not authorize similar other paths.
    return "^" + "".join("\\" + char if char in r"\.^$*+?{}[]|()" else char for char in str(plugin)) + "$"


def lua_module(controller, plugin):
    # Only Hyprland's standard hl API is required. No Omarchy globals,
    # helpers, package.path changes or separate privacy module are needed.
    settings = Path.home() / ".config/hypr/hyprveil-settings.lua"
    return ('-- Hyprveil managed autoload v2; runtime controller validates all pins.\n'
            '-- Permission rules apply only during initial compositor config.\n'
            'hl.permission({ binary = ' + json.dumps(permission_pattern(plugin), ensure_ascii=False) + ', type = "plugin", mode = "allow" })\n'
            'dofile(' + service.lua_string(str(settings)) + ')\n'
            'hl.on("hyprland.start", function()\n'
            '  hl.exec_cmd(' + service.lua_string("/usr/bin/python3 " + shlex.quote(str(controller)) + " start") + ')\n'
            'end)\n').encode()


def main_config(original):
    text = original.decode("utf-8")
    found = [block for block in (BLOCK, LEGACY_BLOCK) if block in text]
    if any(marker in text for marker in (BEGIN, END, LEGACY_BEGIN, LEGACY_END)):
        if len(found) != 1 or sum(text.count(marker) for marker in (BEGIN, LEGACY_BEGIN)) != 1 or \
           sum(text.count(marker) for marker in (END, LEGACY_END)) != 1:
            raise service.Refused("existing Hyprveil managed block differs; inspect it before installation")
        text = text.replace(found[0], "", 1)
    elif 'require("hypr.hyprveil")' in text or "require('hypr.hyprveil')" in text:
        raise service.Refused("existing unmanaged Hyprveil require must be reviewed first")
    # Load persisted defaults just after the Omarchy bootstrap. Ordinary user
    # configuration parsed later can deliberately override these values.
    bootstrap = re.search(r"(?m)^[ \t]*dofile\([^\n]*bootstrap\.lua[^\n]*\)[ \t]*$", text)
    if bootstrap:
        return (text[:bootstrap.end()] + "\n\n" + BLOCK + "\n\n" + text[bootstrap.end():].lstrip("\n")).encode()
    return (BLOCK + "\n\n" + text.lstrip("\n")).encode()


def validate_lua(module, main, folder):
    compiler = shutil.which("luac", path="/usr/bin:/bin")
    if not compiler:
        raise service.Refused("luac is required for installation syntax validation")
    for name, contents in (("module.lua", module), ("main.lua", main)):
        path = folder / name
        atomic(path, contents, 0o600)
        result = subprocess.run([compiler, "-p", str(path)], capture_output=True, text=True, timeout=5,
                                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        if result.returncode:
            raise service.Refused("Lua syntax validation failed: " + result.stderr.strip()[:300])


class Installer:
    def __init__(self, args):
        self.args = args
        self.home = Path.home()
        self.base = self.home / ".local/share/hyprveil"
        self.controller_path = self.base / "controller.py"
        self.release = self.base / "releases" / args.plugin_sha256 / "hyprveil.so"
        self.ledger = self.base / "install.json"
        self.cli = self.home / ".local/bin/hyprveil"
        self.config = self.home / ".config/hyprveil/config.json"
        self.lua = self.home / ".config/hypr/hyprveil.lua"
        self.lua_settings = self.home / ".config/hypr/hyprveil-settings.lua"
        self.main = self.home / ".config/hypr/hyprland.lua"
        self.snapshots = {}
        self.changed = []
        self.written = {}
        self.cli_created = False
        self.report_dir = None
        self.report = {"version": 1, "installed": False, "loaded_by_installer": False}

    def preflight(self, controller):
        source = self.args.plugin.absolute()
        if source.resolve(strict=True) != source:
            raise service.Refused("source release path must not contain symlinks")
        self.plugin_bytes, _ = read_owned(source)
        if sha(self.plugin_bytes) != self.args.plugin_sha256:
            raise service.Refused("source release SHA-256 does not match the explicit pin")
        controller.settings = service.manifest({"version": 1, "enabled": True, "plugin": str(self.release),
            "plugin_sha256": self.args.plugin_sha256, "compositor_sha256": self.args.compositor_sha256,
            "abi_hash": self.args.abi_hash, "desired_mode": "omit", "image_path": ""})
        controller.connect()
        if self.args.pid is not None and controller.target.pid != self.args.pid:
            raise service.Refused("selected compositor PID differs from --pid")
        version = controller.version()
        if service.digest_file(Path(f"/proc/{controller.target.pid}/exe"), process_executable=True) != self.args.compositor_sha256:
            raise service.Refused("running compositor ELF differs from the explicit tested pin")
        if service.digest_file(Path("/usr/bin/Hyprland")) != self.args.compositor_sha256:
            raise service.Refused("installed compositor ELF differs from the explicit tested pin")
        errors = controller.raw("configerrors")
        if errors:
            raise service.Refused("existing compositor configuration errors must be resolved before installation")
        # Replacing the manifest while a different release remains mapped
        # would make the newly installed controller refuse its own rollback.
        # Existing exact-release mappings are safe for an idempotent install.
        try:
            controller.active()
        except service.Refused as error:
            raise service.Refused("stop the current Hyprveil release before installing a different pinned release") from error
        self.original_main, self.main_mode = read_owned(self.main, 1024 * 1024)
        self.controller_bytes = Path(__file__).with_name("service.py").read_bytes()
        self.module_bytes = lua_module(self.controller_path, self.release)
        self.main_bytes = main_config(self.original_main)
        controller.verify_identity()
        self.report["preflight"] = {"pid": controller.target.pid, "signature": controller.signature,
            "abi_hash": version["abiHash"], "compositor_sha256": self.args.compositor_sha256,
            "plugin_sha256": self.args.plugin_sha256, "plugins": controller.plugins(), "config_errors": errors}

    def previous(self, path):
        if path not in self.snapshots:
            try:
                value = read_owned(path)
            except FileNotFoundError:
                value = None
            self.snapshots[path] = value
        return self.snapshots[path]

    def managed_write(self, path, contents, mode, previous_hash=None):
        before = self.previous(path)
        if before is not None and before[0] != contents:
            if previous_hash is None or sha(before[0]) != previous_hash:
                raise service.Refused("refusing to overwrite an unrelated existing file: " + str(path))
        if before != (contents, mode):
            try:
                current = read_owned(path)
            except FileNotFoundError:
                current = None
            if current != before:
                raise service.Refused("installation file changed during preflight: " + str(path))
            # Register the intent before replace/fsync: a write can fail after
            # the new inode is already visible and still needs rollback.
            self.written[path] = (contents, mode)
            self.changed.append(path)
            atomic(path, contents, mode)

    def save_backups(self):
        backups = self.report_dir / "backups"
        backups.mkdir(mode=0o700)
        values = []
        for index, (path, saved) in enumerate(self.snapshots.items()):
            value = {"path": str(path), "existed": saved is not None}
            if saved is not None:
                name = f"{index}-{path.name}"
                atomic(backups / name, saved[0], 0o600)
                value.update(backup=str(backups / name), mode=oct(saved[1]), sha256=sha(saved[0]))
            values.append(value)
        self.report["backups"] = values

    def setup_files(self):
        for path, private in ((self.base, True), (self.base / "releases", True), (self.release.parent, True),
                              (self.config.parent, True), (self.home / ".local/state/hyprveil", True), (self.cli.parent, False)):
            ensure(path, private)
        old = {}
        try:
            old = json.loads(service.read_private(self.ledger))
            fields = {"version", "controller_sha256", "lua_sha256", "plugin_sha256", "plugin", "abi_hash", "compositor_sha256"}
            if not isinstance(old, dict) or old.keys() != fields or type(old.get("version")) is not int or old["version"] != 1 or \
               any(not isinstance(old.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", old[key]) for key in ("controller_sha256", "lua_sha256", "plugin_sha256", "compositor_sha256")):
                raise service.Refused("invalid existing installation receipt")
            service.manifest({"version": 1, "enabled": True, "plugin": old["plugin"], "plugin_sha256": old["plugin_sha256"],
                              "compositor_sha256": old["compositor_sha256"], "abi_hash": old["abi_hash"]})
        except FileNotFoundError:
            pass
        # Check all existing files before replacing any of them.
        for path in (self.release, self.controller_path, self.lua, self.lua_settings, self.config, self.ledger, self.main):
            self.previous(path)
        release = self.snapshots[self.release]
        if release is not None and (sha(release[0]) != self.args.plugin_sha256 or release[1] != 0o400):
            raise service.Refused("existing pinned release differs or is not read-only")
        for path, value, field in ((self.controller_path, self.controller_bytes, "controller_sha256"), (self.lua, self.module_bytes, "lua_sha256")):
            before = self.snapshots[path]
            if before is not None and before[0] != value and sha(before[0]) != old.get(field):
                raise service.Refused("refusing to overwrite an unrelated existing file: " + str(path))
        if os.path.lexists(self.cli):
            if not self.cli.is_symlink() or self.cli.lstat().st_uid != os.getuid() or os.readlink(self.cli) != str(self.controller_path):
                raise service.Refused("existing hyprveil CLI is unrelated")
        self.cli_existed = os.path.lexists(self.cli)
        settings = {"version": 1, "enabled": True, "plugin": str(self.release), "plugin_sha256": self.args.plugin_sha256,
                    "compositor_sha256": self.args.compositor_sha256, "abi_hash": self.args.abi_hash, "desired_mode": "omit", "image_path": "",
                    "appearance": dict(service.DEFAULT_APPEARANCE)}
        if self.snapshots[self.config] is not None:
            previous_config = service.manifest(json.loads(service.read_private(self.config)))
            # Keep the user's explicit enable/effect choice on a repeat install.
            if not old and any(previous_config.get(key) != value for key, value in settings.items() if key not in ("enabled", "desired_mode", "image_path", "appearance")):
                raise service.Refused("existing configuration has unrelated release pins")
            settings.update({key: previous_config[key] for key in ("enabled", "desired_mode", "image_path", "appearance")})
        self.settings_bytes = encoded(settings)
        previous_lua = self.snapshots[self.lua_settings]
        self.lua_settings_bytes = previous_lua[0] if previous_lua else service.lua_settings_source(settings)
        compiler = shutil.which("luac", path="/usr/bin:/bin")
        if not compiler:
            raise service.Refused("luac is required for Lua settings validation")
        syntax_path = self.report_dir / "settings.lua"
        atomic(syntax_path, self.lua_settings_bytes, 0o600)
        syntax = subprocess.run([compiler, "-p", str(syntax_path)], capture_output=True, text=True, timeout=5,
                                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        if syntax.returncode:
            raise service.Refused("Lua settings syntax validation failed: " + syntax.stderr.strip()[:300])
        self.ledger_bytes = encoded({"version": 1, "controller_sha256": sha(self.controller_bytes),
            "lua_sha256": sha(self.module_bytes), "plugin_sha256": self.args.plugin_sha256,
            "plugin": str(self.release), "abi_hash": self.args.abi_hash, "compositor_sha256": self.args.compositor_sha256})
        if read_owned(self.main, 1024 * 1024)[0] != self.original_main:
            raise service.Refused("main configuration changed during installation preflight")
        self.save_backups()
        self.managed_write(self.release, self.plugin_bytes, 0o400)
        self.managed_write(self.controller_path, self.controller_bytes, 0o755, old.get("controller_sha256"))
        self.managed_write(self.config, self.settings_bytes, 0o600,
                           sha(self.snapshots[self.config][0]) if self.snapshots[self.config] else None)
        self.managed_write(self.ledger, self.ledger_bytes, 0o600,
                           sha(self.snapshots[self.ledger][0]) if self.snapshots[self.ledger] else None)
        # User Lua is intentionally outside the receipt pins: updates retain
        # it byte for byte, including custom code and formatting.
        if previous_lua is None:
            self.managed_write(self.lua_settings, self.lua_settings_bytes, 0o600)
        self.managed_write(self.lua, self.module_bytes, 0o600, old.get("lua_sha256"))
        if not self.cli_existed:
            os.symlink(str(self.controller_path), self.cli)
            self.cli_created = True
        self.managed_write(self.main, self.main_bytes, self.main_mode, sha(self.original_main))

    def rollback(self, controller):
        # Restore the entry point before removing its required module.
        order = [self.main] + [path for path in reversed(self.changed) if path != self.main]
        errors = []
        config_changed = False
        main_restored = self.main not in self.changed
        for path in order:
            if path not in self.changed:
                continue
            try:
                value = self.snapshots[path]
                try:
                    current = read_owned(path)
                except FileNotFoundError:
                    current = None
                if current == value:
                    if path == self.main:
                        main_restored = True
                    continue
                if current != self.written[path]:
                    raise service.Refused("rollback refuses a concurrently changed file: " + str(path))
                if path in (self.main, self.lua, self.lua_settings):
                    config_changed = True
                if path != self.main and not main_restored:
                    raise service.Refused("keeping dependencies because main configuration could not be restored: " + str(path))
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic(path, value[0], value[1])
                if path == self.main:
                    main_restored = True
            except (OSError, service.Refused) as error:
                errors.append(str(error))
        if self.cli_created and main_restored:
            try:
                if not self.cli.is_symlink() or self.cli.lstat().st_uid != os.getuid() or os.readlink(self.cli) != str(self.controller_path):
                    raise service.Refused("rollback refuses a concurrently changed CLI")
                self.cli.unlink()
            except (OSError, service.Refused) as error:
                errors.append(str(error))
        if config_changed:
            try:
                controller.verify_identity()
                if controller.raw("reload") != "ok":
                    raise service.Refused("rollback reload was refused")
                if controller.raw("configerrors"):
                    raise service.Refused("configuration errors remain after rollback")
            except (OSError, service.Refused) as error:
                errors.append(str(error))
        self.report["rollback"] = {"restored": not errors, "reload_required": config_changed, "errors": errors}

    def run(self):
        controller = service.Controller(signature=self.args.signature)
        with controller.locked():
            self.preflight(controller)
            artifacts = PROJECT / "artifacts"
            ensure(artifacts)
            self.report_dir = Path(tempfile.mkdtemp(prefix="install-", dir=artifacts))
            self.report_dir.chmod(0o700)
            atomic(self.report_dir / "preflight.json", encoded(self.report["preflight"]), 0o600)
            try:
                validate_lua(self.module_bytes, self.main_bytes, self.report_dir)
                self.setup_files()
                controller.verify_identity()
                # A separate manual loader does not share our flock. Preserve
                # the previous files if it loaded a foreign release meanwhile.
                controller.active()
                reload_required = any(path in self.changed for path in (self.main, self.lua, self.lua_settings))
                if reload_required and controller.raw("reload") != "ok":
                    raise service.Refused("configuration reload was refused")
                if controller.raw("configerrors"):
                    raise service.Refused("configuration errors appeared after installation")
                self.report.update(installed=True, config_errors="", reload_validated=reload_required,
                    autoload_activation="next-compositor-start", permission_activation="initial-config-only", report=str(self.report_dir / "report.json"),
                    files=[{"path": str(path), "mode": oct(path.stat().st_mode & 0o777), "sha256": sha(read_owned(path)[0])}
                           for path in (self.release, self.controller_path, self.config, self.lua, self.lua_settings, self.main, self.ledger)], cli=str(self.cli))
                atomic(self.report_dir / "report.json", encoded(self.report), 0o600)
            except (OSError, ValueError, service.Refused, subprocess.TimeoutExpired) as error:
                self.report["installed"] = False
                self.report["error"] = str(error)
                self.rollback(controller)
                try:
                    atomic(self.report_dir / "report.json", encoded(self.report), 0o600)
                except (OSError, service.Refused) as report_error:
                    self.report["report_error"] = str(report_error)
                raise
            return self.report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--plugin-sha256", required=True)
    parser.add_argument("--abi-hash", required=True)
    parser.add_argument("--compositor-sha256", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--pid", type=int, help="optional additional compositor PID pin")
    args = parser.parse_args(argv)
    installer = Installer(args)
    try:
        print(json.dumps(installer.run(), ensure_ascii=False))
        return 0
    except (OSError, ValueError, service.Refused, subprocess.TimeoutExpired) as error:
        report = installer.report_dir / "report.json" if installer.report_dir else None
        print(json.dumps({"error": str(error), "report": str(report) if report and report.exists() else None,
                          "rollback": installer.report.get("rollback"), "report_error": installer.report.get("report_error")}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
