"""Attest an X11 FD belonging to one marked synthetic Hyprland lab."""
from __future__ import annotations

import os
from pathlib import Path
import re
import socket
import stat
import struct

from lab import load_lab, proc_env


def lab_peer(runtime, pid):
    _, data = load_lab(runtime)
    if pid == data["pid"]:
        return True  # Hyprland may own the inherited lazy Xwayland listen FD.
    try:
        process = Path(f"/proc/{pid}")
        if process.stat().st_uid != os.getuid() or Path(os.readlink(process / "exe")).name != "Xwayland":
            return False
        if proc_env(pid).get(b"XDG_RUNTIME_DIR", b"").decode() != str(runtime):
            return False
        current = pid
        for _ in range(16):
            current = int(Path(f"/proc/{current}/stat").read_text().rsplit(") ", 1)[1].split()[1])
            if current == data["pid"]:
                return True
            if current <= 1:
                break
    except (OSError, ValueError, IndexError):
        pass
    return False


def attest_fd(fd, runtime):
    runtime, data = load_lab(runtime)
    if os.environ.get("DISPLAY") or os.environ.get("XDG_RUNTIME_DIR") != str(runtime) or \
       os.environ.get("WAYLAND_DISPLAY") != data["wayland_display"]:
        raise RuntimeError("X11 fixture requires the explicit lab environment and no inherited DISPLAY")
    duplicate = os.dup(fd)
    try:
        endpoint = socket.socket(fileno=duplicate)
    except BaseException:
        os.close(duplicate)
        raise
    with endpoint:
        if endpoint.family != socket.AF_UNIX or endpoint.type != socket.SOCK_STREAM:
            raise RuntimeError("X11 fixture requires a connected Unix stream FD")
        pid, uid, _ = struct.unpack("3i", endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid() or not lab_peer(runtime, pid):
        raise RuntimeError("X11 FD peer is not the marked lab or its Xwayland descendant")
    return {"peer_pid": pid, "peer_uid": uid, "lab_pid": data["pid"]}


def connect_lab(runtime):
    """Never send an X11 handshake before lock ownership and peer attestation."""
    runtime, data = load_lab(runtime)
    candidates = list(Path("/tmp").glob(".X*-lock"))
    if len(candidates) > 128:
        raise RuntimeError("too many global X11 locks for a bounded lab lookup")
    for lock in candidates:
        match = re.fullmatch(r"\.X([0-9]+)-lock", lock.name)
        if not match:
            continue
        fd = -1
        try:
            fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > 64:
                continue
            pid = int(os.read(fd, 65).strip())
            if not lab_peer(runtime, pid):
                continue
        except (OSError, ValueError):
            continue
        finally:
            if fd >= 0:
                os.close(fd)
        path = Path("/tmp/.X11-unix") / ("X" + match[1])
        if path.is_symlink() or not path.is_socket():
            continue
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            endpoint.settimeout(2)
            endpoint.connect(str(path))
            pid, uid, _ = struct.unpack("3i", endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid() or not lab_peer(runtime, pid):
                raise RuntimeError("X11 socket peer does not match its lab-owned lock")
            return endpoint, {"display": ":" + match[1], "peer_pid": pid, "lab_pid": data["pid"]}
        except BaseException:
            endpoint.close()
            raise
    raise RuntimeError("no X11 lock/socket owned by the marked lab")
