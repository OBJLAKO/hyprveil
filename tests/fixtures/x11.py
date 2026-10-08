#!/usr/bin/python3
"""Three solid XCB windows on an attested FD; no DISPLAY connection fallback."""
from __future__ import annotations

import argparse
import ctypes as C
import json
import os
from pathlib import Path
import select
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from x11_guard import attest_fd


class Cookie(C.Structure):
    _fields_ = [("sequence", C.c_uint32)]


class Screen(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in ("root", "colormap", "white", "black", "masks")] + \
               [(name, C.c_uint16) for name in ("width", "height", "mm_width", "mm_height", "min_maps", "max_maps")] + \
               [("visual", C.c_uint32)] + [(name, C.c_uint8) for name in ("backing", "save", "depth", "depths")]


class Screens(C.Structure):
    _fields_ = [("data", C.POINTER(Screen)), ("remaining", C.c_int), ("index", C.c_int)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-dir", type=Path, required=True)
    parser.add_argument("--xcb-fd", type=int, required=True)
    args = parser.parse_args(argv)
    # Both the supervisor and fixture attest this descriptor before the first
    # X11 byte. Inherited DISPLAY is refused rather than passed to libxcb.
    peer = attest_fd(args.xcb_fd, args.lab_dir)
    xcb = C.CDLL("libxcb.so.1")
    libc = C.CDLL(None)
    libc.free.argtypes = [C.c_void_p]

    def function(name, result, *parameters):
        value = getattr(xcb, name)
        value.restype, value.argtypes = result, list(parameters)
        return value

    connect = function("xcb_connect_to_fd", C.c_void_p, C.c_int, C.c_void_p)
    disconnect = function("xcb_disconnect", None, C.c_void_p)
    error = function("xcb_connection_has_error", C.c_int, C.c_void_p)
    setup = function("xcb_get_setup", C.c_void_p, C.c_void_p)
    roots = function("xcb_setup_roots_iterator", Screens, C.c_void_p)
    generate = function("xcb_generate_id", C.c_uint32, C.c_void_p)
    create = function("xcb_create_window_checked", Cookie, C.c_void_p, C.c_uint8, C.c_uint32, C.c_uint32,
                      C.c_int16, C.c_int16, C.c_uint16, C.c_uint16, C.c_uint16, C.c_uint16, C.c_uint32, C.c_uint32, C.c_void_p)
    request_check = function("xcb_request_check", C.c_void_p, C.c_void_p, Cookie)
    change = function("xcb_change_property", Cookie, C.c_void_p, C.c_uint8, C.c_uint32, C.c_uint32, C.c_uint32, C.c_uint8, C.c_uint32, C.c_void_p)
    delete = function("xcb_delete_property", Cookie, C.c_void_p, C.c_uint32, C.c_uint32)
    map_window = function("xcb_map_window", Cookie, C.c_void_p, C.c_uint32)
    unmap_window = function("xcb_unmap_window", Cookie, C.c_void_p, C.c_uint32)
    destroy_window = function("xcb_destroy_window", Cookie, C.c_void_p, C.c_uint32)
    clear = function("xcb_clear_area", Cookie, C.c_void_p, C.c_uint8, C.c_uint32, C.c_int16, C.c_int16, C.c_uint16, C.c_uint16)
    flush = function("xcb_flush", C.c_int, C.c_void_p)
    poll_event = function("xcb_poll_for_event", C.c_void_p, C.c_void_p)
    focus = function("xcb_get_input_focus", Cookie, C.c_void_p)
    focus_reply = function("xcb_get_input_focus_reply", C.c_void_p, C.c_void_p, Cookie, C.c_void_p)
    connection = connect(args.xcb_fd, None)  # libxcb owns this supplied FD.
    windows = {}
    try:
        if error(connection):
            raise RuntimeError("isolated XCB handshake failed")
        iterator = roots(setup(connection))
        if iterator.remaining != 1:
            raise RuntimeError("expected exactly one synthetic X11 screen")
        screen = iterator.data.contents
        def create_fixture(role, color, rect):
            xid = generate(connection)
            windows[role] = xid
            values = (C.c_uint32 * 2)(color, 32768)  # BACK_PIXEL | EXPOSURE.
            cookie = create(connection, screen.depth, xid, screen.root, *rect, 0, 1, screen.visual, 2 | 2048, values)
            failed = request_check(connection, cookie)
            if failed:
                libc.free(failed)
                raise RuntimeError("synthetic X11 window creation failed")
            name = ("org.hyprveil.fixture.x11." + role).encode()
            wm_class = C.create_string_buffer(name + b"\0" + name + b"\0")
            change(connection, 0, xid, 67, 31, 8, len(wm_class.raw) - 1, wm_class)  # WM_CLASS/STRING.
            title = C.create_string_buffer(("Hyprveil synthetic X11 " + role).encode())
            change(connection, 0, xid, 39, 31, 8, len(title.value), title)
            map_window(connection, xid)
            return xid
        for role, color, rect in (("parent", 0xE84090, (64, 64, 240, 160)),
                                  ("child", 0xE84090, (576, 360, 200, 120)),
                                  ("public", 0xF2CF52, (96, 480, 160, 120))):
            create_fixture(role, color, rect)
        parent_alive = True
        flush(connection)
        print(json.dumps({"ready": True, "windows": windows, "peer": peer}), flush=True)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            while event := poll_event(connection):
                response = C.c_uint8.from_address(event).value & 0x7F
                if response == 12:  # XCB_EXPOSE window at offset 4.
                    xid = C.c_uint32.from_address(event + 4).value
                    clear(connection, 0, xid, 0, 0, 0, 0)
                elif response == 0:
                    libc.free(event)
                    raise RuntimeError("synthetic X11 protocol error")
                libc.free(event)
            flush(connection)
            if error(connection):
                raise RuntimeError("isolated XCB connection closed")
            if not select.select([sys.stdin], [], [], 0.05)[0]:
                continue
            command = sys.stdin.readline().strip()
            if command in ("", "quit"):
                break
            if command in ("private", "public"):
                parent = C.c_uint32(windows["parent" if command == "private" else "public"])
                change(connection, 0, windows["child"], 68, 33, 32, 1, C.byref(parent))  # WM_TRANSIENT_FOR/WINDOW.
            elif command == "unset":
                delete(connection, windows["child"], 68)
            elif command == "parent-hide":
                if not parent_alive:
                    raise RuntimeError("cannot hide a destroyed synthetic parent")
                unmap_window(connection, windows["parent"])
            elif command == "parent-destroy":
                if not parent_alive:
                    raise RuntimeError("cannot destroy a destroyed synthetic parent")
                destroy_window(connection, windows["parent"])
                parent_alive = False
            elif command == "parent-recreate":
                if parent_alive:
                    raise RuntimeError("refusing a duplicate synthetic parent")
                create_fixture("parent", 0xE84090, (64, 64, 240, 160))
                parent_alive = True
            else:
                raise RuntimeError("unknown synthetic fixture command")
            flush(connection)
            reply = focus_reply(connection, focus(connection), None)
            if not reply:
                raise RuntimeError("XCB command roundtrip failed")
            libc.free(reply)
            print(json.dumps({"command": command, "ack": True, "windows": windows,
                              "parent_alive": parent_alive}), flush=True)
        return 0
    finally:
        disconnect(connection)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr, flush=True)
        raise SystemExit(1)
