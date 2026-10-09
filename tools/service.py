#!/usr/bin/python3
"""Manage one explicitly selected Hyprland session with a pinned Hyprveil release."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid
import zlib


class Refused(RuntimeError):
    pass


class NotReady(Refused):
    pass


def bounded_command(argv, *, env, timeout, limit=131072):
    """Drain both IPC streams under one byte budget and reap every child."""
    process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline = time.monotonic() + timeout
    total = 0
    try:
        with selectors.DefaultSelector() as ready:
            for stream in streams:
                ready.register(stream, selectors.EVENT_READ)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in ready.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), min(8192, limit - total + 1))
                    if not chunk:
                        ready.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise Refused("oversized compositor response")
                    streams[key.fileobj].extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            status = process.wait(timeout=remaining)
        return subprocess.CompletedProcess(argv, status,
            streams[process.stdout].decode("utf-8"), streams[process.stderr].decode("utf-8"))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()


TESTED_ABI = "efb50993780079460b0cbed1363e2166a2de1d9f_aq_0.15_hu_0.14_hg_0.5_hc_0.1_hlg_0.6"
APPEARANCE_VARIANTS = ("prism", "signal", "aurora", "contour", "radar", "matte", "error404", "matrix", "anonymous", "glass")
APPEARANCE_ALIASES = {"satin": "prism", "telegram": "signal", "grid": "radar", "404": "error404", "cmatrix": "matrix",
                      "anon": "anonymous", "liquid-glass": "glass", "liquidglass": "glass"}
DEFAULT_APPEARANCE = {"variant": "prism", "color": "#ffffff", "grain": 50,
                      "speed": 100, "darkness": 50, "eye": True, "eye_size": 80,
                      "icon": "eye", "icon_opacity": 75}
ICON_FIELDS = {"icon", "icon_opacity"}
APPEARANCE_RANGES = {"grain": (0, 100), "speed": (0, 200),
                     "darkness": (0, 100), "eye_size": (40, 128), "icon_opacity": (0, 100)}
LUA_BEGIN = "-- BEGIN HYPRVEIL SETTINGS v1"
LUA_END = "-- END HYPRVEIL SETTINGS v1"
CONFIG_FIELDS = ("mode", "image_path", *DEFAULT_APPEARANCE)


def lua_string(value):
    # Lua has decimal byte escapes, not JSON's Unicode escapes. This also
    # prevents a path/quote from becoming executable configuration code.
    return '"' + ''.join(chr(byte) if 32 <= byte < 127 and byte not in (34, 92)
                         else "\\" + f"{byte:03d}" for byte in value.encode("utf-8")) + '"'


def native_values(status):
    if not isinstance(status.get("appearance"), dict):
        raise Refused("invalid native appearance")
    appearance = dict(status["appearance"])
    for field in ICON_FIELDS:
        if field in status:
            appearance[field] = status[field]
    appearance = validate_appearance(appearance, canonical=True)
    mode, image = status.get("mode"), status.get("image_path", "")
    if mode not in ("omit", "black", "image", "spoiler") or not isinstance(image, str) or len(image.encode()) > 4096 or \
       any(ord(c) < 32 or ord(c) == 127 for c in image) or image and not Path(image).is_absolute():
        raise Refused("invalid native configuration values")
    return dict(mode=mode, image_path=image, **appearance)


def lua_settings_block(values):
    values = native_values({"mode": values["mode"], "image_path": values["image_path"],
                   "appearance": validate_appearance({key: values[key] for key in DEFAULT_APPEARANCE if key in values})})
    lines = [LUA_BEGIN, "local hyprveil_settings = {"]
    for key in CONFIG_FIELDS:
        value = values[key]
        literal = lua_string(value) if isinstance(value, str) else ("true" if value else "false") if type(value) is bool else str(value)
        lines.append(f"  {key} = {literal},")
    return "\n".join([*lines, "}", LUA_END])


def lua_settings_source(settings):
    values = dict(mode=settings["desired_mode"], image_path=settings["image_path"], **settings["appearance"])
    return ("-- Hyprveil: standard Hyprland configuration. Edit the literal values below.\n"
            "-- The Omarchy panel updates this block; custom Lua may follow it.\n" + lua_settings_block(values) + "\n\n"
            'local _, missing = hl.get_config("plugin.hyprveil.mode")\n'
            "if not missing then\n"
            "  hl.config({ plugin = { hyprveil = hyprveil_settings } })\n"
            "end\n").encode()


def parse_lua_settings(contents):
    # We never execute Lua in the controller. GUI persistence is limited to a
    # literal nine-field table; unrelated code outside the block is preserved.
    text = contents.decode("utf-8")
    if text.count(LUA_BEGIN) != 1 or text.count(LUA_END) != 1:
        raise Refused("hyprveil-settings.lua needs one managed settings block")
    start, finish = text.index(LUA_BEGIN), text.index(LUA_END)
    if start >= finish:
        raise Refused("invalid Lua settings block order")
    body = text[start + len(LUA_BEGIN):finish].strip()
    match = re.fullmatch(r"local\s+hyprveil_settings\s*=\s*\{(.*)\}", body, re.S)
    if not match:
        raise Refused("custom Lua inside the settings block: use Lua to update it, or move code after the block")
    values = {}
    string = r'"(?:[^"\\\r\n]|\\(?:[0-9]{3}|[\\"nrt]))*"'
    remainder = match[1]
    while remainder.strip():
        # Allow comments and layout changes without treating arbitrary Lua as data.
        comment = re.match(r"\s*--[^\n]*(?:\n|$)", remainder)
        if comment:
            remainder = remainder[comment.end():]
            continue
        field = re.match(r"\s*([a-z_]+)\s*=\s*(" + string + r"|true|false|[0-9]+)\s*,\s*", remainder)
        if not field or field[1] not in CONFIG_FIELDS or field[1] in values:
            raise Refused("settings block must contain each supported literal field exactly once")
        key, literal = field[1], field[2]
        if literal[0] == '"':
            raw = bytearray()
            escaped = literal[1:-1]
            i = 0
            while i < len(escaped):
                if escaped[i] != "\\":
                    raw.extend(escaped[i].encode())
                    i += 1
                elif escaped[i + 1].isdigit():
                    byte = int(escaped[i + 1:i + 4])
                    if byte > 255:
                        raise Refused("invalid Lua byte escape")
                    raw.append(byte)
                    i += 4
                else:
                    raw.extend({"\\": b"\\", '"': b'"', "n": b"\n", "r": b"\r", "t": b"\t"}[escaped[i + 1]])
                    i += 2
            values[key] = raw.decode("utf-8")
        else:
            values[key] = literal == "true" if literal in ("true", "false") else int(literal)
        remainder = remainder[field.end():]
    if not set(CONFIG_FIELDS) - ICON_FIELDS <= values.keys():
        raise Refused("settings block must contain all supported legacy fields")
    values = native_values({"mode": values["mode"], "image_path": values["image_path"],
                   "appearance": validate_appearance({key: values[key] for key in DEFAULT_APPEARANCE if key in values})})
    return text, start, finish + len(LUA_END), values


def config_directory(path):
    if path.resolve() != path:
        raise Refused("Lua configuration directory must not contain symlinks")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        os.close(fd)
        raise Refused("unsafe Lua configuration directory")
    return fd


def read_lua_settings(path):
    directory = config_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 65536:
            raise Refused("unsafe or oversized Lua settings file")
        data = bytearray()
        while chunk := os.read(fd, min(8192, 65537 - len(data))):
            data.extend(chunk)
            if len(data) > 65536:
                raise Refused("oversized Lua settings file")
        return bytes(data), stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def atomic_lua_settings(path, contents, mode=0o600):
    directory = config_directory(path.parent)
    temporary = ".hyprveil-" + uuid.uuid4().hex + ".tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as handle:
            handle.write(contents)
            os.fchmod(handle.fileno(), mode)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def validate_appearance(value, canonical=False):
    if not isinstance(value, dict) or not DEFAULT_APPEARANCE.keys() - ICON_FIELDS <= value.keys() or value.keys() - DEFAULT_APPEARANCE.keys():
        raise Refused("appearance must contain the supported fields; only icon additions are optional")
    value = dict({field: DEFAULT_APPEARANCE[field] for field in ICON_FIELDS}, **value)
    variant = APPEARANCE_ALIASES.get(value["variant"], value["variant"]) if isinstance(value["variant"], str) else None
    if variant not in APPEARANCE_VARIANTS:
        raise Refused("invalid appearance variant")
    if not isinstance(value["icon"], str) or value["icon"] not in ("eye", "lock", "shield", "none"):
        raise Refused("icon must be eye, lock, shield or none")
    color = value["color"]
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color) or canonical and color != color.lower():
        raise Refused("appearance color must be #RRGGBB")
    for field, (minimum, maximum) in APPEARANCE_RANGES.items():
        if type(value[field]) is not int or not minimum <= value[field] <= maximum:
            raise Refused(f"appearance {field} must be an integer from {minimum} to {maximum}")
    if type(value["eye"]) is not bool:
        raise Refused("appearance eye must be a boolean")
    return dict(value, variant=variant, color=color.lower())


def merge_appearance(current, patch):
    if not isinstance(patch, dict) or patch.keys() - DEFAULT_APPEARANCE.keys():
        raise Refused("unknown appearance patch field")
    return validate_appearance(dict(validate_appearance(current), **patch))


def appearance_integer(field):
    minimum, maximum = APPEARANCE_RANGES[field]

    def parse(value):
        if not re.fullmatch(r"0|[1-9][0-9]{0,2}", value) or not minimum <= int(value) <= maximum:
            raise argparse.ArgumentTypeError(f"{field} must be an integer from {minimum} to {maximum}")
        return int(value)
    return parse


def appearance_arguments(parser):
    parser.add_argument("--variant", choices=(*APPEARANCE_VARIANTS, *APPEARANCE_ALIASES))
    parser.add_argument("--icon", choices=("eye", "lock", "shield", "none"))
    parser.add_argument("--color", help="opaque tint in #RRGGBB format")
    for field in APPEARANCE_RANGES:
        parser.add_argument("--" + field.replace("_", "-"), type=appearance_integer(field))
    parser.add_argument("--eye", choices=("on", "off"))


def appearance_patch(args):
    patch = {field: getattr(args, field) for field in DEFAULT_APPEARANCE if getattr(args, field, None) is not None}
    if "eye" in patch:
        patch["eye"] = patch["eye"] == "on"
    return patch


def standard_runtime():
    return Path(f"/run/user/{os.getuid()}")


def check_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise Refused("directory must be owned and mode 0700: " + str(path))
    return fd


def read_private(path, limit=65536):
    directory = check_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise Refused("file must be owned, regular, single-link and mode 0600: " + str(path))
        if info.st_size > limit:
            raise Refused("oversized private file: " + str(path))
        data = bytearray()
        while chunk := os.read(fd, min(8192, limit + 1 - len(data))):
            data.extend(chunk)
            if len(data) > limit:
                raise Refused("oversized private file: " + str(path))
        return bytes(data)
    finally:
        os.close(fd)


def atomic_json(path, value):
    directory = check_directory(path.parent)
    temporary = ".hyprveil-" + uuid.uuid4().hex + ".tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def digest_file(path, private_release=False, process_executable=False):
    # /proc/PID/exe is the kernel-owned symlink to the running ELF, including
    # an unlinked old ELF after a package upgrade. Release files never follow links.
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if not process_executable:
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused("binary is not a regular file")
        if private_release and (info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022):
            raise Refused("release must be owned, single-link and not writable by other users")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def manifest(data):
    required = {"version", "enabled", "plugin", "plugin_sha256", "compositor_sha256", "abi_hash"}
    optional = {"desired_mode", "image_path", "appearance"}
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - optional:
        raise Refused("invalid configuration fields")
    if type(data["version"]) is not int or data["version"] != 1 or type(data["enabled"]) is not bool:
        raise Refused("unsupported configuration version or enabled value")
    for field in ("plugin_sha256", "compositor_sha256"):
        if not isinstance(data[field], str) or not re.fullmatch(r"[0-9a-f]{64}", data[field]):
            raise Refused("invalid SHA-256 pin: " + field)
    for field in ("plugin", "abi_hash"):
        if not isinstance(data[field], str) or not data[field] or any(c in data[field] for c in "\x00\r\n"):
            raise Refused("invalid configuration value: " + field)
    if not Path(data["plugin"]).is_absolute() or any(c.isspace() for c in data["plugin"]):
        raise Refused("release path must be absolute and contain no whitespace")
    result = dict(data)
    result.setdefault("desired_mode", "omit")
    result.setdefault("image_path", "")
    result["appearance"] = validate_appearance(result.get("appearance", DEFAULT_APPEARANCE))
    if result["desired_mode"] not in ("omit", "black", "image", "spoiler") or not isinstance(result["image_path"], str) or any(c in result["image_path"] for c in "\x00\r\n"):
        raise Refused("invalid desired mode or image path")
    if result["image_path"] and not Path(result["image_path"]).is_absolute():
        raise Refused("persistent image path must be absolute")
    return result


@dataclass(frozen=True)
class Target:
    pid: int
    signature: str
    started: str


class Controller:
    def __init__(self, config=None, signature=None):
        self.config = Path(config or Path.home() / ".config/hyprveil/config.json")
        self.runtime = Path(os.environ.get("XDG_RUNTIME_DIR", str(standard_runtime())))
        self.signature = signature if signature is not None else os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        self.state_path = self.runtime / ".hyprveil-controller.json"
        self.startup_path = Path.home() / ".local/state/hyprveil/startup.json"
        self.lua_path = Path.home() / ".config/hypr/hyprveil-settings.lua"
        self.target = None
        self.settings = None
        self.connection_deadline = None

    @contextmanager
    def locked(self):
        if self.runtime != standard_runtime() or self.runtime.resolve() != self.runtime:
            raise Refused("controller requires the user's standard runtime")
        directory = check_directory(self.runtime)
        fd = -1
        try:
            fd = os.open(".hyprveil-controller.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=directory)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise Refused("invalid controller lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Refused("another controller command is running") from error
            yield
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(directory)
        # Never unlink the lock: a second inode would defeat flock.

    def raw(self, *args):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8",
               "XDG_RUNTIME_DIR": str(self.runtime), "HYPRLAND_INSTANCE_SIGNATURE": self.signature}
        try:
            timeout = 3
            if self.connection_deadline is not None:
                timeout = min(timeout, self.connection_deadline - time.monotonic())
                if timeout <= 0:
                    raise NotReady("compositor startup deadline elapsed")
            response = bounded_command(["/usr/bin/hyprctl", "-i", self.signature, *args], env=env, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise Refused("compositor IPC timed out") from error
        value = response.stdout.strip()
        if response.returncode:
            if "connect" in (value + response.stderr).lower():
                raise NotReady("selected compositor socket is not ready")
            raise Refused("hyprctl failed: " + (value or response.stderr.strip())[:300])
        if len(value) > 131072:
            raise Refused("oversized compositor response")
        return value

    def query(self, *args):
        try:
            value = json.loads(self.raw(*args))
        except (ValueError, TypeError) as error:
            raise Refused("compositor returned invalid JSON") from error
        if isinstance(value, dict) and value.get("error"):
            raise Refused("compositor refused command: " + str(value["error"])[:300])
        return value

    def select_target(self):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.signature) or self.signature in (".", ".."):
            raise Refused("provide the exact compositor instance signature")
        instances = self.query("-j", "instances")
        if not isinstance(instances, list):
            raise Refused("invalid compositor instance list")
        selected = [item for item in instances if isinstance(item, dict) and item.get("instance") == self.signature]
        if not selected:
            raise NotReady("selected compositor instance is not ready")
        if len(selected) != 1 or type(selected[0].get("pid")) is not int or selected[0]["pid"] <= 0:
            raise Refused("ambiguous compositor identity")
        pid = selected[0]["pid"]
        process = Path(f"/proc/{pid}")
        try:
            if process.stat().st_uid != os.getuid():
                raise Refused("selected compositor belongs to another user")
            executable = os.readlink(process / "exe").removesuffix(" (deleted)")
            if Path(executable).name != "Hyprland":
                raise Refused("selected process is not Hyprland")
            environment = dict(item.split(b"=", 1) for item in (process / "environ").read_bytes().split(b"\0") if b"=" in item)
            if environment.get(b"XDG_RUNTIME_DIR", b"").decode() != str(self.runtime) or environment.get(b"HYPRVEIL_LAB_RUNTIME") or environment.get(b"LIBSEAT_BACKEND") == b"hyprveil-disabled":
                raise Refused("selected compositor is not the standard desktop session")
            started = (process / "stat").read_text().rsplit(") ", 1)[1].split()[19]
            endpoint = self.runtime / "hypr" / self.signature / ".socket.sock"
            if endpoint.is_symlink() or not endpoint.is_socket():
                raise NotReady("selected compositor IPC socket is not ready")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                timeout = 1
                if self.connection_deadline is not None:
                    timeout = min(timeout, self.connection_deadline - time.monotonic())
                    if timeout <= 0:
                        raise NotReady("compositor startup deadline elapsed")
                connection.settimeout(timeout)
                connection.connect(str(endpoint))
                peer_pid, peer_uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if peer_pid != pid or peer_uid != os.getuid():
                raise Refused("compositor socket peer does not match selected process")
        except (FileNotFoundError, ConnectionRefusedError) as error:
            raise NotReady("selected compositor is not ready") from error
        return Target(pid, self.signature, started)

    def connect(self, retry=False):
        deadline = time.monotonic() + (10 if retry else 0)
        self.connection_deadline = deadline if retry else None
        try:
            while True:
                try:
                    self.target = self.select_target()
                    return
                except NotReady:
                    if not retry or time.monotonic() >= deadline:
                        raise
                    time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        finally:
            self.connection_deadline = None

    def verify_identity(self):
        if self.select_target() != self.target:
            raise Refused("compositor process identity changed")

    def version(self):
        value = self.query("-j", "version")
        if not isinstance(value, dict) or value.get("abiHash") != self.settings["abi_hash"]:
            raise Refused("compositor ABI differs from the tested release")
        return value

    def plugins(self):
        value = self.query("-j", "plugin", "list")
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise Refused("invalid plugin list")
        matches = [item for item in value if item.get("name") == "hyprveil"]
        if len(matches) > 1:
            raise Refused("multiple Hyprveil plugins are loaded")
        return matches

    def mapped_release(self):
        expected = self.settings["plugin"]
        for line in Path(f"/proc/{self.target.pid}/maps").read_text().splitlines():
            fields = line.split(None, 5)
            # An already mapped release remains unloadable after its inode was
            # unlinked; dlclose still identifies it by the original load path.
            if len(fields) == 6 and fields[5].removesuffix(" (deleted)") == expected:
                return True
        return False

    def active(self):
        matches = self.plugins()
        if not matches:
            return False
        if not self.mapped_release():
            raise Refused("loaded Hyprveil does not match the exact configured release path")
        return True

    def live_status(self, value):
        if not isinstance(value, dict) or value.get("session") != "live" or value.get("mode") not in ("omit", "black", "image", "spoiler") or value.get("local_dump") != "disabled-in-live":
            raise Refused("invalid live plugin status")
        # Missing appearance remains valid for black/status commands used by
        # the guarded upgrade of a pinned 0.2 predecessor.
        if "appearance" in value:
            appearance = dict(value["appearance"]) if isinstance(value["appearance"], dict) else value["appearance"]
            if ("icon" in value) != ("icon_opacity" in value):
                raise Refused("incomplete native icon status")
            if isinstance(appearance, dict):
                for field in ICON_FIELDS:
                    if field in value:
                        appearance[field] = value[field]
            value = dict(value, appearance=validate_appearance(appearance, canonical=True))
        if "config_api" in value:
            if type(value["config_api"]) is not int or value["config_api"] != 1 or "image_path" not in value:
                raise Refused("invalid native configuration API")
            native_values(value)
        return value

    def native(self, action, image=None):
        args = ("hyprveil", action) + ((str(image),) if image is not None else ())
        value = self.live_status(self.query(*args))
        expected = "image" if action == "image" else action
        if action != "status" and value["mode"] != expected:
            raise Refused("plugin did not enter the requested mode")
        if action == "image" and value.get("image_status") not in ("pending", "ready"):
            raise Refused("plugin rejected the replacement image")
        return value

    def native_appearance(self, value=None):
        args = ("hyprveil", "appearance")
        previous = None
        if value is not None:
            value = validate_appearance(value)
            before = self.native("status")
            previous = before["mode"]
            args += (value["variant"], value["color"], str(value["grain"]), str(value["speed"]),
                     str(value["darkness"]), "1" if value["eye"] else "0", str(value["eye_size"]))
            if ICON_FIELDS <= before.keys():
                args += (value["icon"], str(value["icon_opacity"]))
            elif any(value[field] != DEFAULT_APPEARANCE[field] for field in ICON_FIELDS):
                raise Refused("this loaded release does not support icon customization")
        status = self.live_status(self.query(*args))
        actual = validate_appearance(status.get("appearance"), canonical=True)
        if value is not None and (actual != value or status["mode"] != previous):
            raise Refused("plugin did not acknowledge the requested appearance without changing mode")
        return status

    def native_configure(self, patch, expected):
        before = native_values(expected)
        patch = dict(patch)
        if "variant" in patch and isinstance(patch["variant"], str):
            patch["variant"] = APPEARANCE_ALIASES.get(patch["variant"], patch["variant"])
        if "color" in patch and isinstance(patch["color"], str):
            patch["color"] = patch["color"].lower()
        if patch.keys() & ICON_FIELDS and not ICON_FIELDS <= expected.keys():
            raise Refused("this loaded release does not support icon customization")
        candidate = dict(before, **patch)
        # Validation also bounds every emitted literal and admits only fixed
        # keys. The compare and partial commit execute in one compositor turn.
        lua_settings_block(candidate)
        if patch.keys() - set(CONFIG_FIELDS):
            raise Refused("unsupported native configuration patch")
        def literal(value):
            return lua_string(value) if isinstance(value, str) else ("true" if value else "false") if type(value) is bool else str(value)
        assertions = []
        for field, value in before.items():
            if field in ICON_FIELDS and field not in expected:
                continue
            accessor = "s." + field if field in ("mode", "image_path", *ICON_FIELDS) else "s.appearance." + field
            assertions.append(accessor + " == " + literal(value))
        entries = ",".join(key + "=" + literal(value) for key, value in patch.items())
        code = ('local p=hl.plugin.hyprveil; assert(p,"Hyprveil unavailable"); local s=p.status(); '
                'assert(s and ' + " and ".join(assertions) + ',"Hyprveil settings changed; retry"); '
                'local v,e=p.configure({' + entries + '}); assert(v,e)')
        if self.raw("eval", code) != "ok":
            raise Refused("atomic native configuration was refused; retry with current settings")
        status = self.native("status")
        if native_values(status) != candidate:
            raise Refused("native settings changed before acknowledgement; retry")
        return status

    def window_action(self, action):
        calls = {"toggle": 'p.toggle()', "reset-sharing": 'p.reset_sharing()',
                 "hide": 'p.set_hidden(s.address,s.stable_id,true)',
                 "show": 'p.set_hidden(s.address,s.stable_id,false)'}
        if action not in calls:
            raise Refused("unknown privacy action")
        code = 'local p=hl.plugin.hyprveil; assert(p,"Hyprveil unavailable"); local s=p.active_privacy(); '
        code += 'local v,e=' + calls[action] + '; assert(v,e)'
        if self.raw("eval", code) != "ok":
            raise Refused("native window privacy action was refused")
        value = self.query("hyprveil", "active-privacy")
        if (not isinstance(value, dict) or set(value) != {"state", "address", "stable_id", "native_private", "inherited"}
                or type(value["native_private"]) is not bool or type(value["inherited"]) is not bool):
            raise Refused("invalid native privacy acknowledgement")
        if value["state"] == "none":
            valid = value["address"] == value["stable_id"] == "" and not value["native_private"] and not value["inherited"]
        else:
            valid = (value["state"] in ("hidden", "visible") and isinstance(value["address"], str)
                     and re.fullmatch(r"0x[0-9a-fA-F]{1,16}", value["address"]) and isinstance(value["stable_id"], str)
                     and re.fullmatch(r"[1-9][0-9]{0,19}", value["stable_id"])
                     and int(value["stable_id"]) <= (1 << 64) - 1
                     and not (value["native_private"] and value["inherited"])
                     and (value["state"] == "hidden") == (value["native_private"] or value["inherited"]))
        if not valid:
            raise Refused("invalid native privacy identity or effective state")
        return value

    def record(self, status):
        atomic_json(self.state_path, {"version": 1, "pid": self.target.pid, "signature": self.signature,
                    "started": self.target.started, "plugin": self.settings["plugin"], "status": status})

    def marker_path(self):
        return self.runtime / f".hyprveil-live-{self.target.pid}"

    def read_marker(self):
        path = self.marker_path()
        try:
            contents = read_private(path, 4096).decode()
        except FileNotFoundError:
            return None
        except UnicodeError as error:
            raise Refused("existing live marker is not valid UTF-8") from error
        # Match the native getline grammar exactly; splitlines() also accepts
        # CRLF and control separators which do not represent valid native consent.
        lines = contents.split("\n")
        if (len(lines) != 6 or lines.pop() != "" or
                lines[:3] != ["hyprveil-live-v1", str(self.target.pid), self.signature] or
                lines[3] not in ("black", "cancelled") or not re.fullmatch(r"[0-9]+", lines[4]) or
                lines[3] == "cancelled" and lines[4] != "0"):
            raise Refused("existing live marker is not this controller's selected session")
        return contents

    def write_marker(self, contents, previous):
        path = self.marker_path()
        directory = check_directory(self.runtime)
        fd = -1
        try:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            fd = os.open(path.name, flags | (os.O_CREAT | os.O_EXCL if previous is None else 0),
                         0o600, dir_fd=directory)
            opened = os.fstat(fd)
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1 or \
               (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino) or \
               (previous is not None and os.read(fd, 4097) != previous.encode()):
                raise Refused("live marker changed before admission update")
            # Rewrite only the validated inode; never replace an unknown path
            # or follow a link. Intermediate short/invalid contents deny native
            # admission. Keep cancelled tombstones until explicit rearming or
            # runtime cleanup so late compositor consent cannot become a normal
            # marker-free Hyprpm load after a controller timeout.
            encoded = contents.encode()
            if os.pwrite(fd, encoded, 0) != len(encoded):
                raise Refused("could not write the complete live marker")
            os.ftruncate(fd, len(encoded))
            os.fsync(fd)
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(directory)

    def remove_marker(self):
        previous = self.read_marker()
        if previous is None:
            return # Standard no-marker loads never create cancellation state.
        cancelled = f"hyprveil-live-v1\n{self.target.pid}\n{self.signature}\ncancelled\n0\n"
        if previous != cancelled:
            self.write_marker(cancelled, previous)

    def create_marker(self, lifetime=120):
        if type(lifetime) is not int or not 1 <= lifetime <= 14400:
            raise Refused("live trial lifetime must be within four hours")
        previous = self.read_marker()
        expires = int(time.time()) + lifetime
        self.write_marker(f"hyprveil-live-v1\n{self.target.pid}\n{self.signature}\nblack\n{expires}\n", previous)
        return expires

    def safe_image(self, path):
        image = Path(path)
        if not image.is_absolute() or any(c in str(image) for c in "\x00\r\n"):
            raise Refused("image path must be absolute")
        fd = os.open(image, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size < 45 or info.st_size > 16 * 1024 * 1024:
                raise Refused("image must be a regular PNG no larger than 16 MiB")
            data = bytearray()
            while chunk := os.read(fd, 1024 * 1024):
                data.extend(chunk)
                if len(data) > 16 * 1024 * 1024:
                    raise Refused("PNG exceeds the image size limit")
        finally:
            os.close(fd)
        if data[:8] != b"\x89PNG\r\n\x1a\n" or data[8:16] != b"\x00\x00\x00\rIHDR":
            raise Refused("replacement must have a PNG IHDR header")
        width, height = struct.unpack(">II", data[16:24])
        if not width or not height or width > 8192 or height > 8192 or width * height > 16 * 1024 * 1024:
            raise Refused("PNG dimensions exceed the safe image limit")
        depth, color, compression, filtering, interlace = data[24:29]
        legal_depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
        if color not in legal_depths or depth not in legal_depths[color] or compression or filtering or interlace not in (0, 1):
            raise Refused("unsupported or invalid PNG image header")
        # Match Cairo's native guard: 16-bit images may become RGBA128F,
        # requiring 16 bytes per pixel even for a grayscale source.
        if width * height * (16 if depth == 16 else 4) > 64 * 1024 * 1024:
            raise Refused("PNG exceeds the 64 MiB decoded image limit")
        offset, has_data, ended, palette, data_ended = 8, False, False, False, False
        alpha, palette_entries = False, 0
        compressed, chunks = [], 0
        while offset + 12 <= len(data):
            chunks += 1
            if chunks > 16384:
                raise Refused("PNG exceeds the 16384 chunk limit")
            size = struct.unpack(">I", data[offset:offset + 4])[0]
            end = offset + 12 + size
            if end > len(data):
                raise Refused("truncated PNG chunk")
            kind = bytes(data[offset + 4:offset + 8])
            if any(not (65 <= item <= 90 or 97 <= item <= 122) for item in kind) or kind[2] & 32:
                raise Refused("invalid PNG chunk type")
            payload = data[offset + 4:end - 4]
            if zlib.crc32(payload) & 0xffffffff != struct.unpack(">I", data[end - 4:end])[0]:
                raise Refused("invalid PNG chunk checksum")
            if kind == b"IHDR" and offset != 8:
                raise Refused("duplicate PNG header")
            if kind not in (b"IHDR", b"PLTE", b"IDAT", b"IEND") and not kind[0] & 32:
                raise Refused("unknown critical PNG chunk")
            if kind == b"PLTE":
                if palette or has_data or not size or size % 3 or size > 768 or color in (0, 4) or color == 3 and size // 3 > 2 ** depth:
                    raise Refused("invalid PNG palette")
                palette = True
                palette_entries = size // 3
            if kind == b"tRNS":
                if alpha or has_data or not size or size > 256 or color == 0 and size != 2 or color == 2 and size != 6 or \
                   color == 3 and (not palette or size > palette_entries) or color not in (0, 2, 3):
                    raise Refused("invalid PNG transparency chunk")
                alpha = True
            if kind == b"IDAT":
                if data_ended:
                    raise Refused("non-contiguous PNG image data")
                has_data = True
                compressed.append(bytes(data[offset + 8:end - 4]))
            elif has_data:
                data_ended = True
            if kind == b"IEND":
                ended = size == 0 and end == len(data)
                break
            offset = end
        if not has_data or not ended or color == 3 and not palette:
            raise Refused("PNG is missing complete image data")
        # Validate the bounded zlib stream without allocating a decoded image.
        # Include Adam7 rows so indexed/interlaced PNGs remain supported.
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
        passes = ((0, 0, 1, 1),) if not interlace else ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
        rows = []
        for x, y, dx, dy in passes:
            columns = max(0, (width - x + dx - 1) // dx)
            count = max(0, (height - y + dy - 1) // dy)
            if columns:
                rows.extend([1 + (columns * channels * depth + 7) // 8] * count)
        row, remaining = 0, rows[0]
        decoder = zlib.decompressobj()
        try:
            for part in compressed:
                if decoder.eof and part:
                    raise Refused("trailing compressed PNG data")
                while True:
                    output = decoder.decompress(part, 65536)
                    part = decoder.unconsumed_tail
                    cursor = 0
                    while cursor < len(output):
                        if row >= len(rows):
                            raise Refused("PNG expands beyond its declared dimensions")
                        if remaining == rows[row] and output[cursor] > 4:
                            raise Refused("invalid PNG scanline filter")
                        size = min(remaining, len(output) - cursor)
                        cursor += size
                        remaining -= size
                        if not remaining:
                            row += 1
                            remaining = rows[row] if row < len(rows) else 0
                    if decoder.unused_data:
                        raise Refused("trailing compressed PNG data")
                    if not part:
                        break
            if not decoder.eof or row != len(rows) or remaining:
                raise Refused("incomplete decoded PNG image")
        except zlib.error as error:
            raise Refused("invalid compressed PNG image") from error
        return image

    def fallback_black(self):
        try:
            if self.active():
                self.native("black")
        except (OSError, Refused):
            pass

    def prepare_lua(self, current, **updates):
        if current.get("config_api") != 1:
            return None
        before = read_lua_settings(self.lua_path)
        text, start, finish, _ = parse_lua_settings(before[0])
        values = dict(native_values(current), **updates)
        values = native_values({"mode": values["mode"], "image_path": values["image_path"],
                                "appearance": validate_appearance({key: values[key] for key in DEFAULT_APPEARANCE})})
        contents = (text[:start] + lua_settings_block(values) + text[finish:]).encode()
        return before, contents, values

    def reload_config(self):
        try:
            self.verify_identity()
            if self.raw("reload") != "ok":
                raise Refused("Hyprland configuration reload was refused")
            if self.raw("configerrors"):
                raise Refused("Hyprland configuration has errors; the mask was set to black")
            status = self.native("status")
            if status.get("config_api") != 1:
                raise Refused("loaded release does not support native configuration")
            self.record(status)
            return status
        except (OSError, Refused):
            self.fallback_black()
            raise

    def persist_lua(self, plan):
        if plan is None:
            return None
        before, contents, expected = plan
        if read_lua_settings(self.lua_path) != before:
            raise Refused("Lua settings changed during this command; retry with the current configuration")
        if contents != before[0]:
            backup = self.startup_path.parent / "last-settings.lua"
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = check_directory(backup.parent)
            os.close(directory)
            atomic_lua_settings(backup, before[0])
            if read_lua_settings(self.lua_path) != before:
                raise Refused("Lua settings changed while saving the backup; retry")
            atomic_lua_settings(self.lua_path, contents, before[1])
        status = self.reload_config()
        if native_values(status) != expected:
            raise Refused("other Lua configuration overrides Hyprveil settings; edit that override or the settings file")
        return status

    def apply_mode(self, mode, image="", persist=False):
        plan = None
        try:
            chosen = self.safe_image(image) if mode == "image" else None
        except (OSError, Refused):
            self.fallback_black()
            raise
        if persist:
            current = self.native("status")
            patch = {"mode": mode}
            if chosen is not None:
                patch["image_path"] = str(chosen)
            plan = self.prepare_lua(current, **patch)
        try:
            status = self.native_configure(patch, current) if plan is not None else self.native(mode, chosen)
        except (OSError, Refused):
            self.fallback_black()
            raise
        try:
            self.record(status)
            if persist:
                status = self.persist_lua(plan) or status
                updated = dict(self.settings, desired_mode=mode)
                if mode == "image":
                    updated["image_path"] = str(chosen)
                if plan is not None:
                    updated.update(appearance=status["appearance"], image_path=status["image_path"])
                atomic_json(self.config, updated)
                self.settings = updated
        except (OSError, Refused):
            self.fallback_black()
            raise
        return status

    def apply_appearance(self, value, persist=False, patch=None, expected=None):
        value = validate_appearance(value)
        current = expected if expected is not None else self.native("status")
        actual_patch = dict(value) if patch is None else patch
        plan = self.prepare_lua(current, **actual_patch) if persist else None
        try:
            status = self.native_configure(actual_patch, current) if current.get("config_api") == 1 else self.native_appearance(value)
            self.record(status)
            if persist:
                status = self.persist_lua(plan) or status
                updated = dict(self.settings, appearance=value)
                if plan is not None:
                    updated.update(desired_mode=status["mode"], image_path=status["image_path"])
                atomic_json(self.config, updated)
                self.settings = updated
        except (OSError, Refused):
            self.fallback_black()
            raise
        return status

    def startup_record(self, phase):
        self.startup_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_json(self.startup_path, {"version": 1, "phase": phase, "pid": self.target.pid,
                    "signature": self.signature, "started": self.target.started,
                    "plugin_sha256": self.settings["plugin_sha256"], "time": int(time.time())})

    def check_pending(self, recovering=False):
        try:
            value = json.loads(read_private(self.startup_path))
        except FileNotFoundError:
            return
        if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1 or value.get("phase") not in ("pending", "ready-black", "healthy", "refused") or \
           type(value.get("pid")) is not int or value["pid"] <= 0 or not isinstance(value.get("signature"), str) or not isinstance(value.get("started"), str) or \
           not isinstance(value.get("plugin_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["plugin_sha256"]) or type(value.get("time")) is not int or value["time"] < 0:
            raise Refused("invalid durable startup state")
        if value["phase"] != "pending" or recovering:
            return
        same = (value["pid"], value["signature"], value["started"]) == (self.target.pid, self.signature, self.target.started)
        if same and self.active():
            # A previous controller can have stopped after native initialization.
            # Attest the mapped module and its status; never repeat that load.
            self.native("status")
            return
        raise Refused("an earlier native load was not confirmed; inspect it, then use enable or start --recover")

    def start(self, recovering=False):
        if not self.settings["enabled"]:
            return {"enabled": False, "action": "skipped"}
        self.connect(retry=True)
        self.version()
        self.check_pending(recovering)
        if not self.active():
            plugin = Path(self.settings["plugin"])
            if plugin.resolve(strict=True) != plugin:
                raise Refused("release path must not contain symlinks")
            if digest_file(plugin, True) != self.settings["plugin_sha256"]:
                raise Refused("release SHA-256 differs from its installation pin")
            if digest_file(Path(f"/proc/{self.target.pid}/exe"), process_executable=True) != self.settings["compositor_sha256"]:
                raise Refused("compositor ELF differs from the tested release")
            if digest_file(Path("/usr/bin/Hyprland")) != self.settings["compositor_sha256"]:
                raise Refused("installed compositor ELF differs from the tested release")
            self.verify_identity()
            self.create_marker()
            try:
                # Installed Lua supplies an exact permission rule during the
                # initial config parse. Runtime eval/reload reports success but
                # cannot add rules in Hyprland 0.56; never claim it grants one.
                self.startup_record("pending")
                if self.raw("plugin", "load", str(plugin)) != "ok":
                    raise Refused("plugin loading was refused")
                if not self.active():
                    raise Refused("plugin load did not produce the configured mapped release")
                admitted = self.native("status")
                if admitted["mode"] != "black":
                    # Plugin loading can trigger a native configuration pass
                    # before this IPC acknowledgement. API 1 may already have
                    # applied the saved style; select and attest black here.
                    # Protected upgrades keep their capture gate held throughout.
                    if (admitted.get("config_api") != 1 or self.native("black")["mode"] != "black"
                            or self.native("status")["mode"] != "black"):
                        raise Refused("newly loaded plugin did not enter black replacement mode")
                self.startup_record("ready-black")
            except (OSError, Refused):
                self.fallback_black()
                # Clear the circuit only when this same compositor still answers
                # and either no plugin was loaded or black masking is confirmed.
                try:
                    self.verify_identity()
                    if not self.active() or self.native("status")["mode"] == "black":
                        self.startup_record("refused")
                except (OSError, Refused):
                    pass
                raise
            finally:
                self.remove_marker()
        else:
            self.native("black")
            self.startup_record("ready-black")
        if self.native("status").get("config_api") == 1:
            # Registered values are applied by the normal Hyprland config pass.
            # JSON now retains admission pins and a migration mirror only.
            read_lua_settings(self.lua_path)
            result = self.reload_config()
        else:
            self.apply_appearance(self.settings["appearance"])
            result = self.apply_mode(self.settings["desired_mode"], self.settings["image_path"])
        try:
            self.startup_record("healthy")
        except (OSError, Refused):
            self.fallback_black()
            raise
        return result

    def run(self, action, image=None, recover=False, appearance=None):
        with self.locked():
            directory = check_directory(self.config.parent)
            os.close(directory)
            self.settings = manifest(json.loads(read_private(self.config)))
            # Validate patches before IPC, then merge against actual native
            # values below so a Lua/keybinding change is never overwritten.
            requested_appearance = merge_appearance(self.settings["appearance"], appearance if appearance is not None else {}) if action == "configure" else None
            if action in ("start", "enable"):
                if action == "enable":
                    self.settings["enabled"] = True
                    atomic_json(self.config, self.settings)
                return self.start(recovering=recover or action == "enable")
            if action == "disable":
                self.settings["enabled"] = False
                atomic_json(self.config, self.settings)
            self.connect()
            loaded = self.active()
            if action == "status":
                status = self.native("status") if loaded else None
                return {"enabled": self.settings["enabled"], "loaded": loaded,
                        "desired_mode": self.settings["desired_mode"],
                        "appearance": validate_appearance(status.get("appearance"), canonical=True) if loaded else self.settings["appearance"],
                        "config_file": str(self.lua_path), "status": status}
            if action in ("stop", "disable"):
                if loaded:
                    self.native("black")
                    if self.raw("plugin", "unload", self.settings["plugin"]) != "ok" or self.plugins():
                        raise Refused("plugin unload was not confirmed")
                self.remove_marker()
                self.record({"mode": "native", "loaded": False})
                return {"enabled": self.settings["enabled"], "loaded": False, "mode": "native"}
            if not loaded:
                raise Refused("the configured Hyprveil release is not loaded")
            if action in ("toggle", "hide", "show", "reset-sharing"):
                return self.window_action(action)
            if action == "reload-config":
                return self.reload_config()
            if action == "configure":
                current = self.native("status")
                if current.get("config_api") == 1:
                    requested_appearance = merge_appearance(current["appearance"], appearance if appearance is not None else {})
                return self.apply_appearance(requested_appearance, persist=True,
                    patch=appearance if current.get("config_api") == 1 else None, expected=current)
            return self.apply_mode(action, str(image) if image is not None else "", persist=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signature", help="exact Hyprland instance; otherwise use the caller's instance environment")
    parser.add_argument("--config", type=Path, help="owned private installation manifest")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "status", "omit", "black", "spoiler", "stop", "disable", "enable", "reload-config",
                   "toggle", "hide", "show", "reset-sharing"):
        command = sub.add_parser(action)
        if action == "start":
            command.add_argument("--recover", action="store_true", help="explicitly retry an unconfirmed previous native load")
    image = sub.add_parser("image")
    image.add_argument("path", type=Path)
    appearance_arguments(sub.add_parser("configure", help="update saved spoiler appearance without changing the hiding mode"))
    args = parser.parse_args(argv)
    try:
        value = Controller(args.config, args.signature).run(args.action, getattr(args, "path", None), getattr(args, "recover", False),
                                                        appearance_patch(args) if args.action == "configure" else None)
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except (OSError, Refused, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error), "action": args.action}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
