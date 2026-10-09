#!/usr/bin/env python3
"""Prove animated spoiler privacy using synthetic windows in an owned lab.

Only the explicit parent Wayland socket is used to start nested Hyprland.
All control, test windows and captures then use its marked private sockets.
The suite includes a persistent wlr-screencopy copy_with_damage client, so
success requires animation to advance on an otherwise idle desktop.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time

from lab import PROJECT, load_lab
from stress import GEOMETRY_FIELDS, Stress
from service import DEFAULT_APPEARANCE, validate_appearance

sys.path.insert(0, str(PROJECT / "tests"))
from verify_capture import Image, absence_report, close, color_fraction, differing_fraction, opacity_fraction

PRIVATE = (232, 64, 144)
PUBLIC = (242, 207, 82)


def outside_private_pixels(image: Image, excluded_rect) -> int:
    x, y, width, height = excluded_rect
    return sum(close(pixel, PRIVATE, 8) and not (x <= index % image.width < x + width and
               y <= index // image.width < y + height)
               for index, pixel in enumerate(image.pixels((0, 0, image.width, image.height))))


# The installed Hyprland headers specify this v3 wire interface. Keep this
# minimal client definition local: the system package does not ship its XML.
SCREENCOPY_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<protocol name="wlr_screencopy_unstable_v1">
  <copyright>
    Copyright © 2018 Simon Ser
    Copyright © 2019 Andri Yngvason
    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the "Software"),
    to deal in the Software without restriction, including without limitation
    the rights to use, copy, modify, merge, publish, distribute, sublicense,
    and/or sell copies of the Software, and to permit persons to whom the
    Software is furnished to do so, subject to the following conditions:
    The above copyright notice and this permission notice shall be included
    in all copies or substantial portions of the Software.
    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
    OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
    THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
    FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
    DEALINGS IN THE SOFTWARE.
  </copyright>
  <interface name="zwlr_screencopy_manager_v1" version="3">
    <request name="capture_output">
      <arg name="frame" type="new_id" interface="zwlr_screencopy_frame_v1"/>
      <arg name="overlay_cursor" type="int"/>
      <arg name="output" type="object" interface="wl_output"/>
    </request>
    <request name="capture_output_region">
      <arg name="frame" type="new_id" interface="zwlr_screencopy_frame_v1"/>
      <arg name="overlay_cursor" type="int"/>
      <arg name="output" type="object" interface="wl_output"/>
      <arg name="x" type="int"/><arg name="y" type="int"/>
      <arg name="width" type="int"/><arg name="height" type="int"/>
    </request>
    <request name="destroy" type="destructor"/>
  </interface>
  <interface name="zwlr_screencopy_frame_v1" version="3">
    <request name="copy"><arg name="buffer" type="object" interface="wl_buffer"/></request>
    <request name="destroy" type="destructor"/>
    <request name="copy_with_damage" since="2"><arg name="buffer" type="object" interface="wl_buffer"/></request>
    <event name="buffer">
      <arg name="format" type="uint"/><arg name="width" type="uint"/>
      <arg name="height" type="uint"/><arg name="stride" type="uint"/>
    </event>
    <event name="flags"><arg name="flags" type="uint"/></event>
    <event name="ready"><arg name="tv_sec_hi" type="uint"/><arg name="tv_sec_lo" type="uint"/><arg name="tv_nsec" type="uint"/></event>
    <event name="failed"/>
    <event name="damage" since="2">
      <arg name="x" type="uint"/><arg name="y" type="uint"/>
      <arg name="width" type="uint"/><arg name="height" type="uint"/>
    </event>
    <event name="linux_dmabuf" since="3"><arg name="format" type="uint"/><arg name="width" type="uint"/><arg name="height" type="uint"/></event>
    <event name="buffer_done" since="3"/>
  </interface>
</protocol>
'''


SCREENCOPY_CLIENT = r'''// Persistent capture client, confined to an explicit marked synthetic lab.
#include "spoiler-screencopy-client.h"
#include <wayland-client.h>
#include <cairo.h>
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

namespace {
wl_display* display;
wl_shm* shm;
wl_output* output;
zwlr_screencopy_manager_v1* manager;
wl_buffer* buffer;
void* memory = MAP_FAILED;
uint32_t width, height, stride, format, flags;
size_t length;
bool ready, failed;
unsigned damageEvents;
uint64_t timestamp;
std::string outputName;

void geometry(void*, wl_output*, int32_t, int32_t, int32_t, int32_t, int32_t, const char*, const char*, int32_t) {}
void mode(void*, wl_output*, uint32_t, int32_t, int32_t, int32_t) {}
void done(void*, wl_output*) {}
void scale(void*, wl_output*, int32_t) {}
void named(void*, wl_output*, const char* name) { outputName = name; }
void description(void*, wl_output*, const char*) {}
const wl_output_listener outputListener{geometry, mode, done, scale, named, description};
void global(void*, wl_registry* registry, uint32_t name, const char* interface, uint32_t version) {
    if (std::strcmp(interface, "wl_shm") == 0)
        shm = static_cast<wl_shm*>(wl_registry_bind(registry, name, &wl_shm_interface, 1));
    else if (std::strcmp(interface, "wl_output") == 0) {
        if (output || version < 4) throw std::runtime_error("expected one v4 isolated output");
        output = static_cast<wl_output*>(wl_registry_bind(registry, name, &wl_output_interface, 4));
        wl_output_add_listener(output, &outputListener, nullptr);
    } else if (std::strcmp(interface, "zwlr_screencopy_manager_v1") == 0) {
        if (version < 3) throw std::runtime_error("screencopy v3 required");
        manager = static_cast<zwlr_screencopy_manager_v1*>(wl_registry_bind(registry, name, &zwlr_screencopy_manager_v1_interface, 3));
    }
}
void globalRemoved(void*, wl_registry*, uint32_t) {}
const wl_registry_listener registryListener{global, globalRemoved};
void onBuffer(void*, zwlr_screencopy_frame_v1*, uint32_t newFormat, uint32_t w, uint32_t h, uint32_t s) {
    if (buffer || (newFormat != WL_SHM_FORMAT_ARGB8888 && newFormat != WL_SHM_FORMAT_XRGB8888) ||
        !w || !h || w > 8192 || h > 8192 || s < w * 4 || s > 32768 || uint64_t{s} * h > 67108864)
        throw std::runtime_error("invalid bounded screencopy shm format/dimensions");
    width = w; height = h; stride = s; format = newFormat; length = size_t{s} * h;
    const int fd = memfd_create("hyprveil-spoiler-capture", MFD_CLOEXEC);
    if (fd < 0) throw std::runtime_error("capture memfd failed");
    if (ftruncate(fd, length) != 0) { close(fd); throw std::runtime_error("capture allocation failed"); }
    memory = mmap(nullptr, length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (memory == MAP_FAILED) { close(fd); throw std::runtime_error("capture mmap failed"); }
    auto* pool = wl_shm_create_pool(shm, fd, length);
    buffer = wl_shm_pool_create_buffer(pool, 0, width, height, stride, format);
    wl_shm_pool_destroy(pool);
    close(fd);
}
void onFlags(void*, zwlr_screencopy_frame_v1*, uint32_t value) { flags = value; }
void onReady(void*, zwlr_screencopy_frame_v1*, uint32_t hi, uint32_t lo, uint32_t ns) {
    timestamp = ((uint64_t{hi} << 32) | lo) * 1000000000ULL + ns;
    ready = true;
}
void onFailed(void*, zwlr_screencopy_frame_v1*) { failed = true; }
void onDamage(void*, zwlr_screencopy_frame_v1*, uint32_t, uint32_t, uint32_t, uint32_t) { ++damageEvents; }
void onDmabuf(void*, zwlr_screencopy_frame_v1*, uint32_t, uint32_t, uint32_t) {}
void onBufferDone(void*, zwlr_screencopy_frame_v1* frame) {
    if (!buffer) throw std::runtime_error("missing shm capture buffer");
    zwlr_screencopy_frame_v1_copy_with_damage(frame, buffer);
}
const zwlr_screencopy_frame_v1_listener frameListener{onBuffer, onFlags, onReady, onFailed, onDamage, onDmabuf, onBufferDone};

void guard(const std::string& runtime, const std::string& displayName) {
    const auto envEquals = [](const char* key, const std::string& value) {
        const char* environment = std::getenv(key);
        return environment && value == environment;
    };
    const std::filesystem::path dir{runtime};
    struct stat info{}, marker{};
    const auto path = dir / ".hyprveil-lab";
    if (dir.parent_path() != "/tmp" || !dir.filename().string().starts_with("hv-") ||
        lstat(runtime.c_str(), &info) != 0 || !S_ISDIR(info.st_mode) || info.st_uid != getuid() || (info.st_mode & 0777) != 0700 ||
        lstat(path.c_str(), &marker) != 0 || !S_ISREG(marker.st_mode) || marker.st_uid != getuid() ||
        !envEquals("XDG_RUNTIME_DIR", runtime) || !envEquals("HYPRVEIL_LAB_RUNTIME", runtime) ||
        !envEquals("WAYLAND_DISPLAY", displayName) || displayName.empty() || displayName.find('/') != std::string::npos ||
        std::getenv("DISPLAY") || std::getenv("WAYLAND_SOCKET"))
        throw std::runtime_error("refusing capture outside explicit marked lab");
    std::ifstream file{path};
    const std::string contents{std::istreambuf_iterator<char>{file}, std::istreambuf_iterator<char>{}};
    if (contents.find("\"runtime_dir\": \"" + runtime + "\"") == std::string::npos ||
        contents.find("\"wayland_display\": \"" + displayName + "\"") == std::string::npos)
        throw std::runtime_error("capture marker identity mismatch");
}

void dispatchUntilReady() {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds{4};
    while (!ready && !failed) {
        while (wl_display_prepare_read(display) != 0)
            if (wl_display_dispatch_pending(display) < 0) throw std::runtime_error("capture display closed");
        if (ready || failed) { wl_display_cancel_read(display); break; }
        if (wl_display_flush(display) < 0 && errno != EAGAIN) {
            wl_display_cancel_read(display); throw std::runtime_error("capture flush failed");
        }
        pollfd fd{wl_display_get_fd(display), POLLIN, 0};
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now()).count();
        const int result = remaining > 0 ? poll(&fd, 1, static_cast<int>(remaining)) : 0;
        if (result <= 0) {
            wl_display_cancel_read(display);
            if (result < 0 && errno == EINTR) continue;
            throw std::runtime_error("idle copy_with_damage frame did not arrive within four seconds");
        }
        if (wl_display_read_events(display) < 0 || wl_display_dispatch_pending(display) < 0)
            throw std::runtime_error("capture dispatch failed");
    }
    if (failed) throw std::runtime_error("screencopy frame failed");
}
}

int main(int argc, char** argv) {
    try {
        if (argc != 3) throw std::runtime_error("usage: spoiler-capture LAB_RUNTIME LAB_DISPLAY");
        guard(argv[1], argv[2]);
        umask(077);
        display = wl_display_connect(argv[2]);
        if (!display) throw std::runtime_error("marked Wayland connection failed");
        auto* registry = wl_display_get_registry(display);
        wl_registry_add_listener(registry, &registryListener, nullptr);
        if (wl_display_roundtrip(display) < 0 || wl_display_roundtrip(display) < 0 ||
            !shm || !manager || !output || outputName != "HV-TEST")
            throw std::runtime_error("isolated HV-TEST capture interfaces missing");
        for (unsigned index = 0; index < 6; ++index) {
            ready = failed = false; flags = damageEvents = 0;
            auto* frame = zwlr_screencopy_manager_v1_capture_output(manager, 0, output);
            zwlr_screencopy_frame_v1_add_listener(frame, &frameListener, nullptr);
            dispatchUntilReady();
            std::vector<unsigned char> image(length);
            const auto* source = static_cast<const unsigned char*>(memory);
            for (uint32_t row = 0; row < height; ++row)
                std::memcpy(image.data() + row * stride, source + ((flags & 1) ? height - row - 1 : row) * stride, stride);
            auto* surface = cairo_image_surface_create_for_data(image.data(),
                format == WL_SHM_FORMAT_XRGB8888 ? CAIRO_FORMAT_RGB24 : CAIRO_FORMAT_ARGB32, width, height, stride);
            const auto path = std::filesystem::path{argv[1]} / ("spoiler-continuous-" + std::to_string(index) + ".png");
            if (std::filesystem::exists(path)) throw std::runtime_error("capture output already exists");
            if (cairo_surface_status(surface) != CAIRO_STATUS_SUCCESS ||
                cairo_surface_write_to_png(surface, path.c_str()) != CAIRO_STATUS_SUCCESS)
                throw std::runtime_error("capture PNG write failed");
            cairo_surface_destroy(surface);
            std::cout << "{\"frame\":" << index << ",\"timestamp_ns\":" << timestamp
                      << ",\"damage_events\":" << damageEvents << "}\n" << std::flush;
            zwlr_screencopy_frame_v1_destroy(frame);
            wl_buffer_destroy(buffer); buffer = nullptr;
            munmap(memory, length); memory = MAP_FAILED;
        }
        wl_display_disconnect(display);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        if (memory != MAP_FAILED) munmap(memory, length);
        if (display) wl_display_disconnect(display);
        return 1;
    }
}
'''


class SpoilerSmoke(Stress):
    def __init__(self, args):
        args.suite = "spoiler-shader-failure" if args.force_shader_failure else "spoiler"
        super().__init__(args)
        self.parent_address = None
        self.report.update(pixel_matching="whole-frame private RGB absence, tolerance 8; opaque exported pixels",
                           continuous_capture="six copy_with_damage frames on one client; static GTK fixtures")

    def assert_check(self, name, ok, **details):
        self.record(name, ok, **details)
        if not ok:
            raise RuntimeError("assertion failed: " + name)

    def protected(self):
        matches = [item for item in self.clients() if item.get("title") == "Hyprveil fixture: protected popup parent"]
        if len(matches) != 1 or not matches[0].get("mapped"):
            raise RuntimeError("expected one mapped synthetic popup parent")
        return matches[0]

    def geometry(self):
        # Transient GTK dialogs may acquire their parent's app-id. Retain each
        # address separately so a duplicate class cannot hide a geometry change.
        return {item["address"]: {field: item[field] for field in GEOMETRY_FIELDS}
                for item in self.clients() if item.get("class", "").startswith("org.hyprveil.fixture.")}

    def capture_image(self, name):
        path = self.artifacts / (name + ".png")
        self.capture(path)
        return Image(path)

    def local_image(self, name):
        self.ctl("hyprveil", "dump-local")
        # The request saves the next rendered local frame; it is asynchronous.
        # A synthetic export forces that frame before we inspect the PNG.
        self.capture_image(name + "-export")
        path = self.artifacts / (name + ".png")
        shutil.copyfile(self.runtime / "hyprveil-local.png", path)
        return Image(path)

    def wait_until(self, predicate, description, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for process in self.fixtures:
                if process.poll() is not None:
                    raise RuntimeError("synthetic fixture exited during " + description)
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        raise RuntimeError("timed out waiting for " + description)

    def popup_state(self):
        path = self.runtime / "popup-ready.json"
        return json.loads(path.read_text()) if path.exists() else None

    def popup_control(self, name, predicate):
        load_lab(self.runtime)
        path = self.runtime / "popup-ready.json"
        previous = path.stat().st_mtime_ns
        fd = os.open(self.runtime / "popup-control.fifo", os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.write(fd, (name + "\n").encode("ascii"))
        finally:
            os.close(fd)
        def updated():
            current = self.popup_state()
            if current and current.get("error"):
                raise RuntimeError("popup control failed: " + current["error"])
            return current if path.stat().st_mtime_ns > previous and predicate(current) else None
        return self.wait_until(updated, name)

    def popup_fixture(self):
        log = (self.artifacts / "popup.log").open("w")
        self.logs.append(log)
        load_lab(self.runtime)
        process = subprocess.Popen([sys.executable, str(PROJECT / "tests/fixtures/popup.py"),
                                    "--lab-dir", str(self.runtime), "--wayland-display", self.state["wayland_display"],
                                    "--start-popup", "hide"], env=self.env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT)
        self.fixtures.append(process)
        self.wait_until(lambda: (value if (value := self.popup_state()) and value.get("ready") and
                                value["main"]["mapped"] else None), "static popup parent")
        self.wait_until(lambda: self.protected(), "popup parent mapped")
        time.sleep(0.3)

    def frame_check(self, name, image, before, roi, mode, underlay, public_rect=None):
        absence = absence_report(image, PRIVATE, tolerance=8)
        opaque = opacity_fraction(image, (0, 0, image.width, image.height))
        geometry_unchanged = self.geometry() == before
        private = self.privacy()
        underlay_difference = differing_fraction(image, roi, underlay, roi, 3)
        black = color_fraction(image, roi, (0, 0, 0), 3)
        foreground = color_fraction(image, public_rect, PUBLIC, 8) if public_rect else None
        effect_correct = (underlay_difference <= 0.001 if mode == "omit" else black >= 0.99 if mode == "black"
                          else underlay_difference > 0.95 and black < 0.99)
        self.assert_check(name, absence["ok"] and opaque == 1 and geometry_unchanged and private and effect_correct and
                          (foreground is None or foreground >= 0.99),
                          forbidden_fraction=absence["forbidden_fraction"], opaque_fraction=opaque,
                          capture_geometry_unchanged=geometry_unchanged, native_privacy_retained=private,
                          mode=mode, underlay_difference=underlay_difference, black_fraction=black,
                          public_foreground_fraction=foreground, roi=roi)

    def compile_continuous_client(self):
        xml = self.artifacts / "spoiler-screencopy.xml"
        header = self.artifacts / "spoiler-screencopy-client.h"
        protocol = self.artifacts / "spoiler-screencopy-protocol.c"
        object_file = self.artifacts / "spoiler-screencopy-protocol.o"
        source = self.artifacts / "spoiler-capture.cpp"
        binary = self.artifacts / "spoiler-capture"
        xml.write_text(SCREENCOPY_XML)
        source.write_text(SCREENCOPY_CLIENT)
        self.command(["wayland-scanner", "client-header", str(xml), str(header)])
        self.command(["wayland-scanner", "private-code", str(xml), str(protocol)])
        self.command(["cc", "-c", str(protocol), "-o", str(object_file)])
        flags = shlex.split(self.command(["pkg-config", "--cflags", "--libs", "cairo", "wayland-client"]))
        self.command(["c++", "-std=c++23", "-Wall", "-Wextra", "-I", str(self.artifacts),
                      str(source), str(object_file), *flags, "-o", str(binary)], timeout=60)
        return binary

    def continuous_capture(self, before, roi, underlay):
        binary = self.compile_continuous_client()
        config = self.runtime / "hyprland.lua"
        with config.open("a") as handle:
            handle.write('\nhl.permission(' + json.dumps("^" + re.escape(str(binary)) + "$") + ', "screencopy", "allow")\n')
        self.ctl("reload")
        if self.ctl("configerrors"):
            raise RuntimeError("continuous capture permission configuration errors")
        self.ctl("hyprveil", "spoiler")
        self.ctl("dismissnotify", "-1")
        self.dispatch('hl.dsp.cursor.move({x=16,y=16})')
        time.sleep(0.4)
        # No compositor control calls or new grim clients while this persistent
        # client is waiting. The fixtures draw once and remain unchanged.
        observed = [json.loads(line) for line in self.command([str(binary), str(self.runtime),
                    self.state["wayland_display"]], timeout=30).splitlines()]
        self.assert_check("persistent idle copy_with_damage completed six frames", len(observed) == 6 and
                          all(item["damage_events"] > 0 for item in observed) and
                          all(second["timestamp_ns"] > first["timestamp_ns"] for first, second in zip(observed, observed[1:])),
                          protocol_frames=observed)
        frames = []
        for index in range(6):
            destination = self.artifacts / f"continuous-{index}.png"
            shutil.copyfile(self.runtime / f"spoiler-continuous-{index}.png", destination)
            frames.append(Image(destination))
            self.frame_check(f"continuous frame {index}: opaque spoiler preserves privacy", frames[-1], before,
                             roi, "spoiler", underlay)
        differences = [differing_fraction(first, roi, second, roi, 2) for first, second in zip(frames, frames[1:])]
        exact_differences = [differing_fraction(first, roi, second, roi, 0) for first, second in zip(frames, frames[1:])]
        cumulative_differences = [differing_fraction(frames[0], roi, image, roi, 2) for image in frames[1:]]
        # Low-frequency geometry can change by less than three RGB levels in
        # one short frame interval. Require fresh content on every frame and
        # perceptible movement over the whole burst, independent of its phase.
        self.assert_check("idle damage-driven spoiler frames visibly animate",
                          min(exact_differences) > 0 and cumulative_differences[-1] > 0.05,
                          successive_private_roi_differences=differences,
                          successive_exact_private_roi_differences=exact_differences,
                          cumulative_private_roi_differences=cumulative_differences,
                          observation_span_seconds=(observed[-1]["timestamp_ns"] - observed[0]["timestamp_ns"]) / 1e9)

    def direct_and_region_capture(self, before):
        parent = self.protected()
        direct_path = self.artifacts / "spoiler-direct-window.png"
        self.command(["grim", "-T", parent["stableId"], str(direct_path)])
        direct = Image(direct_path)
        full_rect = (0, 0, direct.width, direct.height)
        self.assert_check("spoiler direct-window export keeps opaque native black denial",
                          absence_report(direct, PRIVATE, 8)["ok"] and
                          color_fraction(direct, full_rect, (0, 0, 0), 0) == 1 and
                          opacity_fraction(direct, full_rect) == 1 and
                          [direct.width, direct.height] == parent["size"] and self.privacy() and
                          self.geometry() == before, captured_size=[direct.width, direct.height],
                          expected_size=parent["size"], stable_id=parent["stableId"])
        px, py = parent["at"]
        pw, ph = parent["size"]
        geometry = f"{px-16},{py-16} {pw+32}x{ph+32}"
        path = self.artifacts / "spoiler-cropped-region.png"
        self.command(["grim", "-g", geometry, str(path)])
        region = Image(path)
        private_roi = (32, 32, pw - 32, ph - 32)
        margin_roi = (0, 0, pw + 32, 8)
        self.assert_check("cropped spoiler export preserves privacy and mask coordinates",
                          absence_report(region, PRIVATE, 8)["ok"] and
                          opacity_fraction(region, (0, 0, region.width, region.height)) == 1 and
                          color_fraction(region, private_roi, (36, 132, 196), 3) == 0 and
                          color_fraction(region, private_roi, (0, 0, 0), 3) < 0.99 and
                          color_fraction(region, margin_roi, (36, 132, 196), 3) >= 0.99 and
                          self.geometry() == before and self.privacy(), crop_geometry=geometry,
                          cropped_private_roi=private_roi, captured_size=[region.width, region.height])

    def timer_lifecycle(self):
        self.capture_image("timer-arm")
        armed = json.loads(self.ctl("hyprveil", "status"))
        self.assert_check("spoiler capture arms its bounded animation timer", armed["spoiler_animation_armed"],
                          status=armed)
        idle_start = time.monotonic()
        time.sleep(0.75)
        stopped = json.loads(self.ctl("hyprveil", "status"))
        idle_elapsed = time.monotonic() - idle_start
        self.assert_check("spoiler animation timer stops within one second without capture",
                          not stopped["spoiler_animation_armed"] and idle_elapsed <= 1 and
                          stopped["policy_generation"] == armed["policy_generation"],
                          observation_after_idle_seconds=idle_elapsed, status=stopped,
                          animation_did_not_change_privacy_policy=stopped["policy_generation"] == armed["policy_generation"])
        self.capture_image("timer-rearm")
        rearmed = json.loads(self.ctl("hyprveil", "status"))
        black = json.loads(self.ctl("hyprveil", "black"))
        self.assert_check("switch to black immediately disarms spoiler animation",
                          rearmed["spoiler_animation_armed"] and black["mode"] == "black" and
                          not black["spoiler_animation_armed"], before=rearmed, after=black)
        self.ctl("hyprveil", "spoiler")

    @staticmethod
    def observed_appearance(status):
        return validate_appearance(dict(status["appearance"],
            icon=status.get("icon", DEFAULT_APPEARANCE["icon"]),
            icon_opacity=status.get("icon_opacity", DEFAULT_APPEARANCE["icon_opacity"])), canonical=True)

    def configure(self, value):
        wanted = validate_appearance(value)
        previous = json.loads(self.ctl("hyprveil", "status"))["mode"]
        status = json.loads(self.ctl("hyprveil", "appearance", wanted["variant"], wanted["color"], str(wanted["grain"]),
                        str(wanted["speed"]), str(wanted["darkness"]), "1" if wanted["eye"] else "0", str(wanted["eye_size"]),
                        wanted["icon"], str(wanted["icon_opacity"])))
        if status.get("mode") != previous or self.observed_appearance(status) != wanted:
            raise RuntimeError("native appearance acknowledgement mismatched or changed mode")
        return status

    def appearance_frame(self, name, before, roi):
        image = self.capture_image(name)
        self.assert_check(name + " stays opaque, private and in place",
                          absence_report(image, PRIVATE, 8)["ok"] and
                          opacity_fraction(image, (0, 0, image.width, image.height)) == 1 and
                          self.geometry() == before and self.privacy())
        return image

    def customization_tests(self, before, roi, underlay):
        # Native configuration reloads restore the file's/default mode. This
        # suite explicitly selects spoiler before comparing visual parameters.
        self.ctl("hyprveil", "spoiler")
        current = json.loads(self.ctl("hyprveil", "appearance"))
        self.assert_check("native appearance query exposes canonical defaults", self.observed_appearance(current) == DEFAULT_APPEARANCE)
        presets = {}
        for variant in ("prism", "signal"):
            self.configure(dict(DEFAULT_APPEARANCE, variant=variant))
            first = self.capture_image(variant + "-animated-first")
            self.frame_check(variant + " preset masks original private pixels", first, before, roi, "spoiler", underlay)
            time.sleep(0.15)
            second = self.capture_image(variant + "-animated-second")
            self.frame_check(variant + " animated preset retains opacity and placement", second, before, roi, "spoiler", underlay)
            difference = differing_fraction(first, roi, second, roi, 2)
            self.assert_check(variant + " preset visibly animates", difference > 0.01, changed_fraction=difference)
            self.configure(dict(DEFAULT_APPEARANCE, variant=variant, speed=0, eye=False))
            frozen = self.appearance_frame(variant + "-speed-zero-first", before, roi)
            time.sleep(0.25)
            repeat = self.appearance_frame(variant + "-speed-zero-second", before, roi)
            stopped = json.loads(self.ctl("hyprveil", "status"))
            difference = differing_fraction(frozen, roi, repeat, roi, 0)
            self.assert_check(variant + " speed zero freezes every private pixel and disarms timer",
                              difference == 0 and not stopped["spoiler_animation_armed"], changed_fraction=difference)
            presets[variant] = frozen
            self.configure(dict(DEFAULT_APPEARANCE, variant=variant, speed=0, eye=False, grain=0))
            grain_zero = self.appearance_frame(variant + "-grain-zero", before, roi)
            self.configure(dict(DEFAULT_APPEARANCE, variant=variant, speed=0, eye=False, grain=100))
            grain_full = self.appearance_frame(variant + "-grain-full", before, roi)
            difference = differing_fraction(grain_zero, roi, grain_full, roi, 2)
            self.assert_check(variant + " grain endpoints visibly affect only the private replacement",
                              difference > 0.10, changed_fraction=difference)
        difference = differing_fraction(presets["prism"], roi, presets["signal"], roi, 2)
        self.assert_check("prism and signal presets have distinguishable frozen appearances", difference > 0.50,
                          changed_fraction=difference)

        vivid = dict(DEFAULT_APPEARANCE, variant="matte", color="#3b82f6", grain=100, speed=200, darkness=0, eye=False)
        self.configure(vivid)
        tinted = self.appearance_frame("signal-blue-bright-fast", before, roi)
        means = [sum(pixel[channel] for pixel in tinted.pixels(roi)) / (roi[2] * roi[3]) for channel in range(3)]
        self.assert_check("hex tint changes the opaque replacement hue", means[2] > means[1] > means[0], mean_rgb=means)
        self.configure(dict(vivid, darkness=100))
        dark = self.appearance_frame("signal-darkness-full", before, roi)
        status = json.loads(self.ctl("hyprveil", "status"))
        self.assert_check("darkness 100 with eye disabled gives solid opaque black and no animation",
                          color_fraction(dark, roi, (0, 0, 0), 0) == 1 and not status["spoiler_animation_armed"])
        self.configure(dict(vivid, color="#000000"))
        zero_tint = self.appearance_frame("signal-black-tint", before, roi)
        status = json.loads(self.ctl("hyprveil", "status"))
        self.assert_check("black tint gives solid opaque black and no animation",
                          color_fraction(zero_tint, roi, (0, 0, 0), 0) == 1 and not status["spoiler_animation_armed"])

        self.configure(dict(DEFAULT_APPEARANCE, speed=0, eye=False))
        eye_off = self.appearance_frame("privacy-eye-disabled", before, roi)
        eye_counts = {}
        for size in (40, 128):
            self.configure(dict(DEFAULT_APPEARANCE, speed=0, eye=True, eye_size=size))
            image = self.appearance_frame(f"privacy-eye-{size}", before, roi)
            eye_counts[size] = round(differing_fraction(eye_off, roi, image, roi, 2) * roi[2] * roi[3])
        self.assert_check("eye toggle and both size endpoints change centered icon pixels",
                          eye_counts[40] > 50 and eye_counts[128] > eye_counts[40] * 2, changed_pixels=eye_counts)

        self.configure(dict(DEFAULT_APPEARANCE, variant="signal", speed=0, eye=False))
        full = self.capture_image("signal-frozen-full-coordinate-reference")
        parent = self.protected()
        px, py = parent["at"]
        pw, ph = parent["size"]
        cropped_path = self.artifacts / "signal-frozen-cropped.png"
        self.command(["grim", "-g", f"{px-16},{py-16} {pw+32}x{ph+32}", str(cropped_path)])
        cropped = Image(cropped_path)
        crop_roi = (32, 32, pw - 32, ph - 32)
        difference = differing_fraction(full, roi, cropped, crop_roi, 0)
        self.assert_check("frozen signal crop has exact full-frame physical grain coordinates",
                          difference == 0 and absence_report(cropped, PRIVATE, 8)["ok"] and
                          opacity_fraction(cropped, (0, 0, cropped.width, cropped.height)) == 1 and
                          self.geometry() == before and self.privacy(), changed_fraction=difference)

        baseline = json.loads(self.ctl("hyprveil", "status"))
        fields = ["signal", "#ffffff", "50", "100", "50", "1", "80"]
        invalid_fields = []
        for index, values in ((0, ("blur",)), (1, ("#fff", "#ffffff00", "#fffffz")),
                              (2, ("NaN", "true", "101", "-1", "1.0")), (3, ("201",)),
                              (4, ("101",)), (5, ("true", "2")), (6, ("39", "129"))):
            for value in values:
                item = fields.copy()
                item[index] = value
                invalid_fields.append(item)
        invalid_fields.extend((fields[:-1], fields + ["unexpected"]))
        rejections = []
        for item in invalid_fields:
            reply = json.loads(self.ctl("hyprveil", "appearance", *item))
            rejections.append(bool(reply.get("error")))
        after = json.loads(self.ctl("hyprveil", "appearance"))
        self.assert_check("malformed native appearance updates are atomic and cannot change privacy or mode",
                          all(rejections) and after["appearance"] == baseline["appearance"] and
                          after["mode"] == baseline["mode"] and after["policy_generation"] == baseline["policy_generation"] and
                          self.geometry() == before and self.privacy(), rejected_inputs=len(rejections))
        local = self.local_image("customization-local")
        self.assert_check("all customization changes leave original local pixels visible", color_fraction(local, roi, PRIVATE, 3) >= 0.99)
        self.configure(dict(DEFAULT_APPEARANCE, variant="signal"))
        self.fractional_scale(before)
        self.configure(DEFAULT_APPEARANCE)

    def fractional_scale(self, before):
        config = self.runtime / "hyprland.lua"
        with config.open("a") as handle:
            handle.write('\nhl.monitor({output="HV-TEST",mode="1280x800@60",position="0x0",scale=1.25})\n')
        self.ctl("reload")
        time.sleep(0.4)
        monitor = self.monitor()
        self.assert_check("fractional output scale applied in disposable lab", monitor["scale"] == 1.25 and
                          not self.ctl("configerrors"), monitor=monitor)
        scaled_geometry = self.geometry()
        scaled_roi = self.roi()
        self.ctl("hyprveil", "omit")
        underlay = self.capture_image("fractional-omit-underlay")
        self.ctl("hyprveil", "spoiler")
        local = self.local_image("fractional-spoiler-local")
        self.assert_check("fractional-scale spoiler keeps original window local",
                          color_fraction(local, scaled_roi, PRIVATE, 3) >= 0.99, private_roi=scaled_roi)
        first = self.capture_image("fractional-spoiler-first")
        self.frame_check("fractional-scale spoiler exports opaque private replacement", first, scaled_geometry,
                         scaled_roi, "spoiler", underlay)
        time.sleep(0.15)
        second = self.capture_image("fractional-spoiler-second")
        self.frame_check("fractional-scale second frame retains privacy and geometry", second, scaled_geometry,
                         scaled_roi, "spoiler", underlay)
        difference = differing_fraction(first, scaled_roi, second, scaled_roi, 2)
        self.assert_check("fractional-scale spoiler visibly animates", difference > 0.05,
                          private_roi_difference=difference)
        with config.open("a") as handle:
            handle.write('\nhl.monitor({output="HV-TEST",mode="1024x768@60",position="0x0",scale=1})\n')
        self.ctl("reload")
        time.sleep(0.4)
        self.assert_check("scale restoration preserves original fixture geometry and privacy",
                          self.monitor()["scale"] == 1 and not self.ctl("configerrors") and
                          before == self.geometry() and self.privacy())

    def full_size_preview(self, before):
        """Create a 1920x1080 design preview entirely from the pink fixture."""
        config = self.runtime / "hyprland.lua"
        with config.open("a") as handle:
            handle.write('\nhl.monitor({output="HV-TEST",mode="1920x1080@60",position="0x0",scale=1})\n')
        self.ctl("reload")
        self.dispatch('hl.dsp.window.fullscreen({window=' + json.dumps(self.address()) +
                      ',action="set",mode="fullscreen",layout_aware=false})')
        time.sleep(0.4)
        self.ctl("dismissnotify", "-1")
        self.ctl("hyprveil", "spoiler")
        monitor = self.monitor()
        self.assert_check("full-size synthetic preview uses a protected 1920x1080 fullscreen fixture",
                          monitor["width"] == 1920 and monitor["height"] == 1080 and
                          self.protected()["fullscreen"] == 2 and self.privacy() and not self.ctl("configerrors"),
                          output_size=[monitor["width"], monitor["height"]])
        geometry = self.geometry()
        rect = (16, 16, monitor["width"] - 32, monitor["height"] - 32)
        local = self.local_image("preview-1920-local")
        self.assert_check("large preview keeps real synthetic content visible locally",
                          color_fraction(local, rect, PRIVATE, 3) >= 0.99)
        first = self.capture_image("preview-1920-spoiler")
        time.sleep(0.35)
        second = self.capture_image("preview-1920-spoiler-second")
        for index, image in enumerate((first, second)):
            absence = absence_report(image, PRIVATE, 8)
            self.assert_check(f"full-size preview frame {index} is opaque and contains no synthetic secret pixels",
                              absence["ok"] and opacity_fraction(image, (0, 0, image.width, image.height)) == 1 and
                              color_fraction(image, rect, (0, 0, 0), 3) < 0.99 and
                              self.geometry() == geometry and self.privacy(), forbidden_fraction=absence["forbidden_fraction"])
        difference = differing_fraction(first, rect, second, rect, 2)
        self.assert_check("large synthetic spoiler preview visibly animates", difference > 0.01,
                          private_roi_difference=difference)
        self.report["design_preview"] = str(self.artifacts / "preview-1920-spoiler.png")
        self.report["design_previews"] = {"prism": self.report["design_preview"]}
        if self.args.customization:
            self.configure(dict(DEFAULT_APPEARANCE, variant="signal"))
            signal = self.capture_image("preview-1920-signal")
            time.sleep(0.15)
            repeat = self.capture_image("preview-1920-signal-second")
            for index, image in enumerate((signal, repeat)):
                self.assert_check(f"full-size signal preview frame {index} is opaque and private",
                                  absence_report(image, PRIVATE, 8)["ok"] and
                                  opacity_fraction(image, (0, 0, image.width, image.height)) == 1 and
                                  color_fraction(image, rect, (0, 0, 0), 3) < 0.99 and
                                  self.geometry() == geometry and self.privacy())
            self.assert_check("full-size signal preview animates without exposing content",
                              differing_fraction(signal, rect, repeat, rect, 2) > 0.01)
            self.report["design_previews"]["signal"] = str(self.artifacts / "preview-1920-signal.png")
            self.configure(DEFAULT_APPEARANCE)
        self.dispatch('hl.dsp.window.fullscreen({window=' + json.dumps(self.address()) +
                      ',action="unset",mode="fullscreen",layout_aware=false})')
        with config.open("a") as handle:
            handle.write('\nhl.monitor({output="HV-TEST",mode="1024x768@60",position="0x0",scale=1})\n')
        self.ctl("reload")
        time.sleep(0.4)
        self.assert_check("large preview restores fixture geometry and native privacy",
                          self.monitor()["width"] == 1024 and self.monitor()["height"] == 768 and
                          self.geometry() == before and self.privacy() and not self.ctl("configerrors"))

    def shader_failure_tests(self):
        self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        if self.ctl("configerrors"):
            raise RuntimeError("forced shader failure lab configuration errors")
        self.fixture("background")
        self.popup_fixture()
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        self.assert_check("forced failure fixture has native capture protection", self.privacy())
        self.load()
        self.ctl("dismissnotify", "-1")
        self.dispatch('hl.dsp.cursor.move({x=16,y=16})')
        self.ctl("hyprveil", "spoiler")
        before = self.geometry()
        rect = self.roi()
        local = self.local_image("forced-failure-local")
        self.assert_check("shader failure keeps original content visible locally", color_fraction(local, rect, PRIVATE, 3) >= 0.99)
        statuses = []
        for index in range(2):
            image = self.capture_image(f"forced-shader-failure-{index}")
            status = json.loads(self.ctl("hyprveil", "status"))
            statuses.append(status)
            absence = absence_report(image, PRIVATE, 8)
            self.assert_check(f"failed shader capture {index} remains opaque black and private",
                              absence["ok"] and opacity_fraction(image, (0, 0, image.width, image.height)) == 1 and
                              color_fraction(image, rect, (0, 0, 0), 0) == 1 and status["mode"] == "spoiler" and
                              status["spoiler_status"] == "black-fallback" and status["spoiler_shader_attempts"] == 1 and
                              not status["spoiler_animation_armed"] and self.privacy() and self.geometry() == before,
                              status=status, forbidden_fraction=absence["forbidden_fraction"])
        self.assert_check("failed shader remains latched safely across subsequent captures",
                          all(status["spoiler_status"] == "black-fallback" and status["spoiler_shader_attempts"] == 1 and
                              not status["spoiler_animation_armed"] for status in statuses), statuses=statuses)
        self.unload()
        image = self.capture_image("forced-shader-failure-unloaded")
        self.assert_check("shader failure unload leaves native opaque black protection and stable geometry",
                          absence_report(image, PRIVATE, 8)["ok"] and color_fraction(image, rect, (0, 0, 0), 0) == 1 and
                          opacity_fraction(image, (0, 0, image.width, image.height)) == 1 and self.privacy() and
                          self.geometry() == before)
        self.report["compositor"] = json.loads(self.ctl("-j", "version"))

    def tests(self):
        self.ctl("output", "create", "headless", "HV-TEST")
        for monitor in json.loads(self.ctl("-j", "monitors")):
            if monitor["name"].startswith("WAYLAND-"):
                self.ctl("output", "remove", monitor["name"])
        config = self.runtime / "hyprland.lua"
        with config.open("a") as handle:
            handle.write('\nhl.window_rule({match={title="^Hyprveil fixture: transient dialog$"}, '
                         'size={180,120},move={760,520}})\n')
        self.ctl("reload")
        if self.ctl("configerrors"):
            raise RuntimeError("spoiler lab configuration errors")
        self.fixture("background")
        self.ctl("dismissnotify", "-1")
        self.dispatch('hl.dsp.cursor.move({x=16,y=16})')
        underlay = self.capture_image("underlay")
        self.popup_fixture()
        roi = self.roi()
        visible = self.capture_image("visible-before-protection")
        self.assert_check("private fixture visibly exposes exact test pixels before protection",
                          color_fraction(visible, roi, PRIVATE, 3) >= 0.99)
        self.dispatch('hl.dsp.window.tag({window=' + json.dumps(self.address()) + ',tag="+hyprveil-private-test"})')
        self.assert_check("native privacy enabled before plugin load", self.privacy())
        self.load()
        self.ctl("dismissnotify", "-1")
        before = self.geometry()
        self.ctl("hyprveil", "spoiler")
        status = json.loads(self.ctl("hyprveil", "status"))
        self.assert_check("native spoiler mode reports active", status.get("mode") == "spoiler", status=status)
        active = json.loads(self.ctl("-j", "activewindow"))
        identity = json.loads(self.ctl("hyprveil", "active-privacy"))
        self.assert_check("atomic active privacy exposes decimal stable identity without window metadata",
                          identity.keys() == {"state", "address", "stable_id", "native_private", "inherited"} and
                          identity["address"] == self.protected()["address"] == active["address"] and
                          identity["stable_id"] == str(int(active["stableId"], 16)) and
                          identity["state"] == "hidden" and identity["native_private"] is True and
                          identity["inherited"] is False and self.privacy(), identity=identity)
        local = self.local_image("spoiler-local")
        self.assert_check("spoiler retains full original local window content", color_fraction(local, roi, PRIVATE, 3) >= 0.99,
                          local_private_fraction=color_fraction(local, roi, PRIVATE, 3))
        first = self.capture_image("spoiler-first")
        self.frame_check("first spoiler capture replaces private pixels", first, before, roi, "spoiler", underlay)
        time.sleep(0.35)
        second = self.capture_image("spoiler-second")
        self.frame_check("second spoiler capture replaces private pixels", second, before, roi, "spoiler", underlay)
        difference = differing_fraction(first, roi, second, roi, 2)
        self.assert_check("successive spoiler captures visibly animate", difference > 0.05, private_roi_difference=difference)
        self.continuous_capture(before, roi, underlay)
        self.direct_and_region_capture(before)
        self.timer_lifecycle()
        self.fractional_scale(before)
        if self.args.customization:
            self.customization_tests(before, roi, underlay)
        if self.args.full_size_preview:
            self.full_size_preview(before)
        for index, mode in enumerate(("spoiler", "omit", "black", "spoiler")):
            self.ctl("hyprveil", mode)
            image = self.capture_image(f"transition-{index}-{mode}")
            self.frame_check("live transition " + mode, image, before, roi, mode, underlay)

        self.fixture("foreground")
        foreground = next(item for item in self.clients() if item.get("class") == "org.hyprveil.fixture.foreground")
        x, y = foreground["at"]
        width, height = foreground["size"]
        public_rect = (x + 8, y + 8, width - 16, height - 16)
        px, py = self.protected()["at"]
        pw, _ = self.protected()["size"]
        unoccluded_roi = (px + 16, py + 16, pw - 32, y - py - 32)
        before_foreground = self.geometry()
        for mode in ("spoiler", "omit", "black", "spoiler"):
            self.ctl("hyprveil", mode)
            self.frame_check(mode + " preserves overlapping public foreground", self.capture_image("stacked-" + mode),
                             before_foreground, unoccluded_roi, mode, underlay, public_rect)

        self.popup_control("popup-show", lambda value: value.get("ready") and value["popup"]["mapped"])
        popup_local = self.local_image("popup-local")
        parent = self.protected()
        main_rect = (*parent["at"], *parent["size"])
        external = outside_private_pixels(popup_local, main_rect)
        self.assert_check("protected popup has local private pixels outside its parent", external > 1000,
                          local_private_pixels_outside_parent=external)
        popup_capture = self.capture_image("spoiler-protected-popup")
        self.assert_check("spoiler protects transient popup pixels outside parent rectangle",
                          absence_report(popup_capture, PRIVATE, 8)["ok"] and self.privacy() and
                          self.geometry() == before_foreground and color_fraction(popup_capture, public_rect, PUBLIC, 8) >= 0.99)
        self.popup_control("dialog-show", lambda value: value.get("ready") and value["dialog"]["mapped"])
        dialog = self.wait_until(lambda: next((item for item in self.clients() if
                                item.get("title") == "Hyprveil fixture: transient dialog"), None), "inherited dialog mapping")
        dialog_address = "address:" + dialog["address"]
        self.assert_check("dialog native flag unset proves ancestor policy is exercised",
                          self.ctl("getprop", dialog_address, "no_screen_share") == "false")
        dialog_local = self.local_image("dialog-local")
        dx, dy = dialog["at"]
        dw, dh = dialog["size"]
        dialog_roi = (dx + 8, dy + 8, dw - 16, dh - 16)
        self.assert_check("inherited dialog remains locally visible", color_fraction(dialog_local, dialog_roi, PRIVATE, 3) >= 0.99)
        before_dialog = self.geometry()
        for mode in ("spoiler", "omit", "black", "spoiler"):
            self.ctl("hyprveil", mode)
            image = self.capture_image("dialog-" + mode)
            self.assert_check(mode + " exports no inherited popup or dialog content",
                              absence_report(image, PRIVATE, 8)["ok"] and self.privacy() and
                              self.ctl("getprop", dialog_address, "no_screen_share") == "false" and
                              self.geometry() == before_dialog,
                              dialog_native_flag=False, private_roi=dialog_roi)
        self.unload()
        fallback = self.capture_image("unloaded-native-fallback")
        self.assert_check("plugin unload preserves parent's native capture protection",
                          self.privacy() and color_fraction(fallback, unoccluded_roi, (0, 0, 0), 3) >= 0.99 and
                          self.geometry() == before_dialog)
        self.report["compositor"] = json.loads(self.ctl("-j", "version"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--parent-runtime", required=True)
    parser.add_argument("--parent-display", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so")
    parser.add_argument("--full-size-preview", action="store_true",
                        help="also capture a fully synthetic protected fullscreen 1920x1080 design preview")
    parser.add_argument("--force-shader-failure", action="store_true",
                        help="exercise opaque black fallback using the marked-lab-only shader failure hook")
    parser.add_argument("--customization", action="store_true",
                        help="also verify both spoiler presets, parameter endpoints, frozen captures and atomic rejection")
    args = parser.parse_args()
    if args.force_shader_failure and args.full_size_preview:
        parser.error("shader failure and full-size design preview are separate suites")
    wrapper_directory = None
    if args.force_shader_failure:
        # lab.run intentionally filters arbitrary inherited environments. This
        # fixed, private test launcher adds only the dedicated lab failure flag.
        wrapper_directory = tempfile.TemporaryDirectory(prefix="hv-spoiler-launcher-", dir="/tmp")
        entry = Path(wrapper_directory.name) / "lab-entry.py"
        entry.write_text("import sys\n" + f"sys.path.insert(0, {str(PROJECT / 'tools')!r})\n" +
                         "import lab\noriginal_environment = lab.base_env\n" +
                         'lab.base_env = lambda: dict(original_environment(), HYPRVEIL_LAB_SPOILER_SHADER_FAILURE="1")\n' +
                         "raise SystemExit(lab.main())\n")
        args.lab_entry = entry
    run = SpoilerSmoke(args)
    def interrupted(signum, frame):
        raise KeyboardInterrupt("spoiler smoke interrupted")
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        run.start()
        if args.force_shader_failure:
            run.shader_failure_tests()
        else:
            run.tests()
        run.report["ok"] = bool(run.checks) and all(item["ok"] for item in run.checks)
    except (Exception, KeyboardInterrupt) as error:
        run.report["error"] = str(error)
        print(json.dumps({"event": "error", "error": str(error)}), flush=True)
    finally:
        run.cleanup()
        if wrapper_directory:
            wrapper_directory.cleanup()
    print(json.dumps({"ok": run.report["ok"], "report": str(run.artifacts / "report.json") if run.artifacts else None,
                      "lab_stopped": run.report.get("lab_stopped")}), flush=True)
    return 0 if run.report["ok"] and run.report.get("lab_stopped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
