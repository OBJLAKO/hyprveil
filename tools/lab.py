#!/usr/bin/env python3
"""Launch and address an isolated nested Hyprland, never the inherited session."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
MARKER = ".hyprveil-lab"
CONFIG = '''-- Synthetic test session; no Omarchy bootstrap or autostart.
hl.monitor({ output = "", mode = "1024x768@60", position = "0x0", scale = 1 })
hl.config({
  general = { gaps_in = 0, gaps_out = 0, border_size = 0, layout = "dwindle" },
  decoration = { rounding = 0, shadow = { enabled = false }, blur = { enabled = false } },
  animations = { enabled = false },
  xwayland = { enabled = false },
  misc = { disable_hyprland_logo = true, disable_splash_rendering = true },
  debug = { disable_logs = false },
})
hl.permission("/usr/bin/grim", "screencopy", "allow")
hl.permission("/usr/bin/hyprctl", "plugin", "allow")
hl.window_rule({ match = { tag = "hyprveil-private-test" }, no_screen_share = true })
hl.window_rule({ match = { class = "^org[.]hyprveil[.]fixture[.].*$" },
  float = true, no_anim = true, no_shadow = true, no_blur = true, border_size = 0 })
hl.window_rule({ match = { class = "^org[.]hyprveil[.]fixture[.]background$" },
  size = { 1024, 768 }, move = { 0, 0 } })
hl.window_rule({ match = { class = "^org[.]hyprveil[.]fixture[.]protected$" },
  size = { 320, 240 }, move = { 256, 192 } })
hl.window_rule({ match = { class = "^org[.]hyprveil[.]fixture[.]foreground$" },
  size = { 160, 120 }, move = { 416, 312 } })
'''


def write_json(path, value):
    # Several independent suites publish latest-lab.json concurrently. A
    # shared predictable .tmp can publish another writer's data or be a link.
    # Hold the parent directory and use a new exclusive inode for each write.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    temporary = ".hyprveil-" + uuid.uuid4().hex + ".tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, indent=2)
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


def proc_env(pid):
    return dict(item.split(b"=", 1) for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if b"=" in item)


def base_env():
    # Do not pass desktop services, startup hooks, tokens or unrelated secrets
    # into a process whose experimental plugin may produce a core dump.
    return {key: value for key, value in os.environ.items()
            if key in ("PATH", "LANG", "TZ") or key.startswith("LC_")}


def load_lab(path):
    runtime = Path(path).resolve(strict=True)
    metadata = runtime.stat()
    if runtime.parent != Path("/tmp") or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise RuntimeError("lab must be an owned private directory directly under /tmp")
    if (runtime / MARKER).is_symlink():
        raise RuntimeError("lab marker must not be a symlink")
    data = json.loads((runtime / MARKER).read_text())
    if Path(data["runtime_dir"]).resolve() != runtime or not runtime.name.startswith(("hyprveil-lab-", "hv-")):
        raise RuntimeError("invalid isolated runtime marker")
    pid = int(data["pid"])
    env = proc_env(pid)
    if env.get(b"XDG_RUNTIME_DIR", b"").decode() != str(runtime):
        raise RuntimeError("marked process does not own this runtime")
    if env.get(b"LIBSEAT_BACKEND") != b"hyprveil-disabled":
        raise RuntimeError("marked compositor is not isolated from physical seats")
    if Path(f"/proc/{pid}/exe").resolve().name.lower() != "hyprland":
        raise RuntimeError("marked process is not Hyprland")
    signature = data["signature"]
    display = data["wayland_display"]
    if any(not isinstance(value, str) or not value or "/" in value or value in (".", "..")
           for value in (signature, display)):
        raise RuntimeError("lab socket names must be plain basenames")
    if not (runtime / display).is_socket():
        raise RuntimeError("marked display is not a socket in the isolated runtime")
    if signature == data.get("parent_signature") or not (runtime / "hypr" / signature / ".socket.sock").is_socket():
        raise RuntimeError("refusing a missing or inherited compositor socket")
    return runtime, data


def lab_env(runtime, data):
    env = base_env()
    for key in ("DISPLAY", "WAYLAND_SOCKET", "DBUS_SESSION_BUS_ADDRESS", "HYPRLAND_INSTANCE_SIGNATURE"):
        env.pop(key, None)
    env.update(XDG_RUNTIME_DIR=str(runtime), WAYLAND_DISPLAY=data["wayland_display"],
               HYPRLAND_INSTANCE_SIGNATURE=data["signature"], GDK_BACKEND="wayland",
               HOME=str(runtime / "home"), XDG_CONFIG_HOME=str(runtime / "home/.config"),
               XDG_CACHE_HOME=str(runtime / "home/.cache"), XDG_DATA_HOME=str(runtime / "home/.local/share"),
               HYPRVEIL_LAB_RUNTIME=str(runtime))
    return env


def run(args):
    parent_runtime = Path(args.parent_runtime).resolve(strict=True)
    parent_display = args.parent_display
    if not parent_display or "/" in parent_display:
        raise RuntimeError("parent display must be an explicit socket name")
    parent_socket = parent_runtime / parent_display
    if not parent_socket.is_socket():
        raise RuntimeError("parent Wayland socket does not exist")
    # sockaddr_un is only 108 bytes; Hyprland signatures consume most of it.
    runtime = Path(tempfile.mkdtemp(prefix="hv-", dir="/tmp"))
    runtime.chmod(0o700)
    for directory in ("home/.config", "home/.cache", "home/.local/share"):
        (runtime / directory).mkdir(parents=True, exist_ok=True)
    artifact_dir = PROJECT / "artifacts" / runtime.name
    artifact_dir.mkdir(parents=True)
    config = runtime / "hyprland.lua"
    config.write_text(CONFIG)
    env = base_env()
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "HYPRLAND_INSTANCE_SIGNATURE", "DBUS_SESSION_BUS_ADDRESS"):
        env.pop(key, None)
    env.update(XDG_RUNTIME_DIR=str(runtime), HOME=str(runtime / "home"),
               XDG_CONFIG_HOME=str(runtime / "home/.config"), XDG_CACHE_HOME=str(runtime / "home/.cache"),
               XDG_DATA_HOME=str(runtime / "home/.local/share"), LIBSEAT_BACKEND="hyprveil-disabled",
               HYPRLAND_NO_SD_VARS="1", HYPRLAND_NO_SD_NOTIFY="1", HYPRLAND_NO_RT="1",
               HYPRVEIL_LAB_RUNTIME=str(runtime))
    # Never export a parent session environment or call uwsm/systemd.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as parent:
        parent.connect(str(parent_socket))
        env["WAYLAND_SOCKET"] = str(parent.fileno())
        log = (artifact_dir / "hyprland.log").open("w")
        child = subprocess.Popen(["Hyprland", "--config", str(config)], env=env, pass_fds=(parent.fileno(),),
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    try:
        deadline = time.monotonic() + 25
        signature = display = None
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"nested Hyprland exited {child.returncode}; see {artifact_dir / 'hyprland.log'}")
            signatures = [p.parent.name for p in (runtime / "hypr").glob("*/.socket.sock")]
            displays = [p.name for p in runtime.glob("wayland-*") if p.is_socket()]
            if signatures and displays:
                signature, display = signatures[0], displays[0]
                break
            time.sleep(0.1)
        if not signature:
            raise RuntimeError(f"no isolated compositor socket; see {artifact_dir / 'hyprland.log'}")
        data = {"runtime_dir": str(runtime), "wayland_display": display, "signature": signature,
                "pid": child.pid, "parent_signature": os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"),
                "artifacts": str(artifact_dir), "created": time.time()}
        write_json(runtime / MARKER, data)
        write_json(PROJECT / "artifacts/latest-lab.json", data)
        print(json.dumps({"ready": True, **data}), flush=True)
        stopping_since = None
        def stop(signum, frame):
            nonlocal stopping_since
            if stopping_since is None:
                stopping_since = time.monotonic()
            if child.poll() is None:
                child.terminate()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while True:
            try:
                return child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                if stopping_since is not None and time.monotonic() - stopping_since > 5:
                    child.kill()
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("run")
    start.add_argument("--parent-runtime", required=True)
    start.add_argument("--parent-display", required=True)
    for action in ("ctl", "exec", "stop", "status"):
        command = sub.add_parser(action)
        command.add_argument("--lab-dir", required=True)
        if action in ("ctl", "exec"):
            command.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action == "run":
            return run(args)
        runtime, data = load_lab(args.lab_dir)
        if args.action == "status":
            print(json.dumps(data, indent=2))
            return 0
        if args.action == "stop":
            os.kill(data["pid"], signal.SIGTERM)
            return 0
        argv = args.arguments
        if argv and argv[0] == "--":
            argv = argv[1:]
        if not argv:
            raise RuntimeError("an explicit command is required")
        if args.action == "ctl":
            argv = ["hyprctl", "-i", data["signature"], *argv]
        return subprocess.run(argv, env=lab_env(runtime, data), check=False).returncode
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"lab: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
