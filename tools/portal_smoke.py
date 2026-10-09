#!/usr/bin/env python3
"""Real browser ScreenCast on a private bus and PipeWire, in a marked lab only."""
from __future__ import annotations
import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
import time
import struct
import zlib
import cairo
from lab import PROJECT, CONFIG, load_lab, lab_env

PAGE = b'''<!doctype html><meta charset="utf-8"><title>Hyprveil isolated portal test</title>
<style>body{background:#10202c;color:#eee;font:20px sans-serif}video{width:640px}button{font:inherit}</style>
<h1>Hyprveil: synthetic ScreenCast preview</h1><button onclick="start()">Start test</button>
<p id="status">Private lab only. No microphone or camera requested.</p><video muted autoplay></video>
<script>
const video=document.querySelector('video'); let stream;
let frameSerial=0,lastFrame={mediaTime:null,presentedFrames:null};
const presented=(now,metadata)=>{frameSerial++;lastFrame=metadata;video.requestVideoFrameCallback(presented)};
video.requestVideoFrameCallback(presented);
window.start=async()=>{
 stream=await navigator.mediaDevices.getDisplayMedia({video:{displaySurface:'monitor',frameRate:10},audio:false});
 video.srcObject=stream; await video.play();
 document.querySelector('#status').textContent='ScreenCast active';
 return {settings:stream.getVideoTracks()[0].getSettings(), audio:stream.getAudioTracks().length};
};
window.stop=()=>{stream?.getTracks().forEach(t=>t.stop());return true};
window.snapshot=async()=>{
 await new Promise(r=>setTimeout(r,150));
 if(!video.videoWidth||!video.videoHeight) throw Error('no video frames');
 const c=document.createElement('canvas');c.width=video.videoWidth;c.height=video.videoHeight;
 const ctx=c.getContext('2d',{willReadFrequently:true});ctx.drawImage(video,0,0);
 const d=ctx.getImageData(0,0,c.width,c.height).data;
 const close=(i,r,g,b)=>Math.abs(d[i]-r)<=8&&Math.abs(d[i+1]-g)<=8&&Math.abs(d[i+2]-b)<=8;
 let magenta=0;for(let i=0;i<d.length;i+=4)if(close(i,232,64,144))magenta++;
 const fraction=(x,y,w,h,r,g,b)=>{let n=0;for(let yy=y;yy<y+h;yy++)for(let xx=x;xx<x+w;xx++)
  if(close((yy*c.width+xx)*4,r,g,b))n++;return n/(w*h)};
 return {width:c.width,height:c.height,magenta,frameSerial,mediaTime:lastFrame.mediaTime,
 presentedFrames:lastFrame.presentedFrames,black:fraction(272,208,288,208,0,0,0),
 blue:fraction(272,208,288,208,36,132,196),private:fraction(272,208,288,208,232,64,144),
 green:fraction(272,208,64,48,46,191,166),alphaGreen:fraction(384,208,64,48,41,162,181),
 alphaBlue:fraction(496,208,48,48,36,132,196),gold:fraction(272,208,288,208,242,207,82),
 png:c.toDataURL('image/png')};
};
</script>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-dir", required=True)
    parser.add_argument("--plugin", type=Path, default=PROJECT / "build/hyprveil.so",
                        help="immutable candidate binary; copied into this marked lab's artifacts")
    args = parser.parse_args()
    runtime, state = load_lab(args.lab_dir)
    env = lab_env(runtime, state)
    artifacts = Path(state["artifacts"]) / "portal"
    artifacts.mkdir(exist_ok=True)
    processes = []
    def interrupted(signum, frame):
        raise KeyboardInterrupt("portal test interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    loaded = False
    results = []
    controls = []
    report = {"ok": False, "lab": state, "frames": results, "controls": controls}
    return_code = 1
    plugin = artifacts / "hyprveil.so"
    shutil.copyfile(args.plugin, plugin)
    plugin.chmod(0o500)
    plugin_sha256 = hashlib.sha256(plugin.read_bytes()).hexdigest()

    def spawn(name, argv):
        log = (artifacts / (name + ".log")).open("w")
        process = subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        processes.append((name, process, log))
        return process

    def command(argv, timeout=15):
        load_lab(runtime)
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"{argv[0]} failed: {result.stdout[-1000:]} {result.stderr[-1000:]}")
        return result.stdout.strip()

    def ctl(*argv):
        value = command(["hyprctl", "-i", state["signature"], *argv])
        if value.startswith(("err", "Invalid", "unknown", "Couldn")):
            raise RuntimeError(f"hyprctl {argv}: {value}")
        return value

    def wait(predicate, description, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for name, process, _ in processes:
                if process.poll() is not None:
                    raise RuntimeError(f"{name} exited {process.returncode}; see {artifacts / (name + '.log')}")
            if predicate():
                return
            time.sleep(0.1)
        raise RuntimeError("timeout: " + description)

    def bus_names():
        return command(["busctl", "--address=" + env["DBUS_SESSION_BUS_ADDRESS"], "--no-pager", "--no-legend", "list"])

    def fixture(role):
        spawn(role, [sys.executable, str(PROJECT / "tests/fixtures/client.py"), "--lab-dir", str(runtime),
                     "--wayland-display", state["wayland_display"], "--role", role])
        def mapped():
            return any(c["class"] == "org.hyprveil.fixture." + role and min(c["size"]) > 0
                       for c in json.loads(ctl("-j", "clients")))
        wait(mapped, role + " mapped")
        time.sleep(0.3)

    class Server(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)
        def log_message(self, *unused):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    page_url = f"http://127.0.0.1:{server.server_port}/"
    profile = Path(tempfile.mkdtemp(prefix="browser-profile-", dir=runtime))
    port_file = profile / "DevToolsActivePort"

    def evaluate(expression):
        return json.loads(command(["node", str(PROJECT / "tools/browser_cdp.mjs"),
                                   str(port_file), page_url, expression], timeout=40))

    def frame(mode, index=0, after_serial=None, pending_public=False):
        started = time.monotonic()
        expected = ["green", "alphaGreen", "alphaBlue"] if mode == "alpha" else ["green" if mode == "image" else mode]
        samples = []
        while True:
            data = evaluate("snapshot()")
            if (data["width"], data["height"]) != (1024, 768):
                raise RuntimeError("unexpected browser frame size: " + json.dumps({k:v for k,v in data.items() if k != "png"}))
            if data["magenta"] and mode != "private" and not pending_public:
                raise RuntimeError("private pixels in protected browser stream: " + json.dumps({k:v for k,v in data.items() if k != "png"}))
            samples.append({k:v for k,v in data.items() if k != "png"})
            fresh = after_serial is None or data["frameSerial"] > after_serial
            content_matches = all(data[key] >= 0.99 for key in expected)
            privacy_matches = mode == "private" or data["magenta"] == 0
            if (fresh and content_matches and privacy_matches) or time.monotonic() - started > 5:
                break
        png = data.pop("png")
        (artifacts / f"{mode}-{index}.png").write_bytes(base64.b64decode(png.split(",", 1)[1]))
        data.update(update_latency_seconds=round(time.monotonic() - started, 3),
                    after_serial=after_serial, sampled_frames=len(samples))
        if not (fresh and content_matches and privacy_matches):
            (artifacts / f"{mode}-{index}.failed-samples.json").write_text(json.dumps(samples, indent=2) + "\n")
            raise RuntimeError("browser privacy assertion failed: " + json.dumps(data))
        results.append({"mode": mode, **data})
        print(json.dumps(results[-1]), flush=True)
        return data["frameSerial"]

    def native_tag(enabled):
        # No effect command, cursor movement, application repaint or other
        # intentional monitor damage accompanies this static-scene transition.
        previous = evaluate("snapshot()")
        sign = "+" if enabled else "-"
        ctl("eval", 'hl.dispatch(hl.dsp.window.tag({window=' + json.dumps(address) +
            ',tag="' + sign + 'hyprveil-private-test"}))')
        actual = ctl("getprop", address, "no_screen_share") == "true"
        if actual != enabled:
            raise RuntimeError("native privacy tag did not reach the requested state")
        return previous["frameSerial"]

    def native_property(enabled):
        previous = evaluate("snapshot()")
        value = "1" if enabled else "0"
        response = ctl("eval", 'hl.dispatch(hl.dsp.window.set_prop({window=' + json.dumps(address) +
                       ',prop="no_screen_share",value="' + value + '"}))')
        actual_value = ctl("getprop", address, "no_screen_share")
        actual = actual_value == "true"
        controls.append({"name": "direct native property transition", "ok": actual == enabled,
                         "requested": enabled, "actual_getprop": actual_value,
                         "dispatch_response": response, "before_serial": previous["frameSerial"]})
        if actual != enabled:
            raise RuntimeError("direct native property did not reach requested state")
        return previous["frameSerial"]

    def image_command(path, mode, index):
        previous = evaluate("snapshot()")
        value = json.loads(ctl("hyprveil", "image", str(path)))
        if value.get("error"):
            raise RuntimeError("image command rejected test file: " + value["error"])
        frame(mode, index, after_serial=previous["frameSerial"])
        if ctl("getprop", address, "no_screen_share") != "true":
            raise RuntimeError("PNG command changed native privacy state")

    def write_header_png(path, width, height):
        payload = struct.pack("!IIBBBBB", width, height, 8, 6, 0, 0, 0)
        checksum = zlib.crc32(b"IHDR" + payload)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack("!I", len(payload)) + b"IHDR" + payload + struct.pack("!I", checksum))

    try:
        # Persist permissions/rules so native plugin loading cannot erase them.
        config = CONFIG + '''
hl.monitor({ output="HV-TEST", mode="1024x768@60", position="0x0", scale=1 })
hl.monitor({ output="HV-CONTROL", mode="1024x768@60", position="1024x0", scale=1 })
hl.window_rule({match={class="^(chromium|hyprveil-browser|chrome-.*)$"}, float=true,
 size={800,600},move={76,50},monitor="HV-CONTROL"})
hl.window_rule({match={title="^http://127[.]0[.]0[.]1:.* is sharing your screen[.]$"},
 float=true,move={76,650},monitor="HV-CONTROL"})
hl.window_rule({match={class="^org[.]hyprveil[.]fixture[.].*$"},monitor="HV-TEST"})
hl.permission("/usr/lib/xdg-desktop-portal-hyprland", "screencopy", "allow")
'''
        (runtime / "hyprland.lua").write_text(config)
        ctl("reload")
        monitors = json.loads(ctl("-j", "monitors"))
        for name in ("HV-TEST", "HV-CONTROL"):
            if not any(m["name"] == name for m in monitors):
                ctl("output", "create", "headless", name)
        for monitor in monitors:
            if monitor["name"].startswith("WAYLAND-"):
                ctl("output", "remove", monitor["name"])
        if ctl("configerrors"):
            raise RuntimeError("invalid lab config: " + ctl("configerrors"))
        # No service activation dirs and no systemd activation on this bus.
        bus_config = runtime / "portal-bus.conf"
        bus_config.write_text(f'''<busconfig><type>session</type><listen>unix:path={runtime}/bus</listen>
<auth>EXTERNAL</auth><policy context="default"><allow send_destination="*"/><allow own="*"/>
<allow eavesdrop="true"/></policy></busconfig>''')
        env.update(DBUS_SESSION_BUS_ADDRESS=f"unix:path={runtime}/bus", XDG_CURRENT_DESKTOP="Hyprland",
                   XDG_SESSION_TYPE="wayland", PIPEWIRE_RUNTIME_DIR=str(runtime), PIPEWIRE_REMOTE="pipewire-0",
                   DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={runtime}/no-system-bus", DISABLE_RTKIT="1",
                   GTK_USE_PORTAL="0", QT_QPA_PLATFORM="wayland", GSETTINGS_BACKEND="memory")
        spawn("dbus", ["dbus-daemon", "--config-file=" + str(bus_config), "--nofork", "--nopidfile"])
        wait(lambda: (runtime / "bus").is_socket(), "private bus")
        pw_config = runtime / "home/.config/pipewire/pipewire.conf.d"
        pw_config.mkdir(parents=True, exist_ok=True)
        (pw_config / "hyprveil.conf").write_text('context.properties = { module.rt = false module.x11.bell = false module.jackdbus-detect = false }\n')
        spawn("pipewire", ["pipewire"])
        wait(lambda: (runtime / "pipewire-0").is_socket(), "private PipeWire")
        spawn("wireplumber", ["wireplumber", "--profile=policy"])
        picker = runtime / "portal-picker"
        picker.write_text(f'''#!{sys.executable}
import os, pathlib, sys
r=pathlib.Path({str(runtime)!r})
if os.environ.get("XDG_RUNTIME_DIR") != str(r) or os.environ.get("WAYLAND_DISPLAY") != {state["wayland_display"]!r} or not (r/".hyprveil-lab").is_file():
 sys.exit(1)
print("[SELECTION]/screen:HV-TEST")
''')
        picker.chmod(0o700)
        portal_config = runtime / "home/.config/hypr"
        portal_config.mkdir(exist_ok=True)
        (portal_config / "xdph.conf").write_text(f'''screencopy {{
 custom_picker_binary = {picker}
 max_fps = 10
 force_shm = 1
 cursor_mode = 1
 allow_token_by_default = 0
}}
''')
        preferred = runtime / "home/.config/xdg-desktop-portal"
        preferred.mkdir(exist_ok=True)
        (preferred / "portals.conf").write_text('[preferred]\ndefault=none\norg.freedesktop.impl.portal.ScreenCast=hyprland\n')
        spawn("xdph", ["/usr/lib/xdg-desktop-portal-hyprland", "--verbose"])
        wait(lambda: "org.freedesktop.impl.portal.desktop.hyprland" in bus_names(), "Hyprland portal")
        spawn("xdp", ["/usr/lib/xdg-desktop-portal", "--verbose"])
        wait(lambda: "org.freedesktop.portal.Desktop " in bus_names(), "public desktop portal")
        fixture("background")
        fixture("protected")
        protected = next(c for c in json.loads(ctl("-j", "clients")) if c["class"] == "org.hyprveil.fixture.protected")
        address = "address:" + protected["address"]
        ctl("eval", 'hl.dispatch(hl.dsp.window.tag({window=' + json.dumps(address) + ',tag="+hyprveil-private-test"}))')
        spawn("chromium", ["chromium", "--ozone-platform=wayland", "--class=hyprveil-browser",
              "--auto-select-screen-capture-source",
              "--enable-features=WebRTCPipeWireCapturer", "--remote-debugging-port=0",
              "--remote-debugging-address=127.0.0.1", "--user-data-dir=" + str(profile),
              "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
              "--disable-component-update", "--disable-default-apps", "--disable-sync",
              "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
              "--password-store=basic", "--disable-features=MediaRouter", "--app=" + page_url])
        wait(port_file.exists, "Chromium debug endpoint", timeout=20)
        time.sleep(1)
        print("starting genuine browser ScreenCast on private portal", flush=True)
        session = evaluate("start()")
        (artifacts / "clients.active.json").write_text(ctl("-j", "clients") + "\n")
        (artifacts / "session.json").write_text(json.dumps(session, indent=2) + "\n")
        frame("black")
        if ctl("plugin", "load", str(plugin)) != "ok":
            raise RuntimeError("plugin load failed")
        loaded = True
        ctl("hyprveil", "omit")
        if ctl("getprop", address, "no_screen_share") != "true":
            raise RuntimeError("native privacy rule lost")
        for index in range(5):
            frame("blue", index)
        ctl("dismissnotify", "-1")
        time.sleep(0.5)
        # The fixture never repaints here. Dynamic native privacy, independent
        # of effect-mode commands, must update this already active consumer.
        geometry_before = {c["address"]: {key: c[key] for key in
            ("at", "size", "workspace", "monitor", "floating", "fullscreen")}
            for c in json.loads(ctl("-j", "clients")) if c["class"].startswith("org.hyprveil.fixture.")}
        for index in range(3):
            previous = native_tag(False)
            frame("private", index, after_serial=previous)
            previous = native_tag(True)
            frame("blue", 20 + index, after_serial=previous, pending_public=True)
            frame("blue", 30 + index)
        previous = native_tag(False)
        frame("private", 9, after_serial=previous)
        for index in range(3):
            previous = native_property(True)
            frame("blue", 50 + index, after_serial=previous, pending_public=True)
            frame("blue", 60 + index)
            if index != 2:
                previous = native_property(False)
                frame("private", 10 + index, after_serial=previous)
        geometry_after = {c["address"]: {key: c[key] for key in
            ("at", "size", "workspace", "monitor", "floating", "fullscreen")}
            for c in json.loads(ctl("-j", "clients")) if c["class"].startswith("org.hyprveil.fixture.")}
        if geometry_before != geometry_after:
            raise RuntimeError("native privacy transitions changed fixture geometry")
        controls.append({"name": "native privacy static-stream transitions preserve geometry", "ok": True})
        mask = artifacts / "mask.png"
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 240)
        paint = cairo.Context(surface)
        paint.set_source_rgb(0.10, 0.24, 0.32)
        paint.paint()
        paint.set_source_rgb(46/255, 191/255, 166/255)
        paint.rectangle(0, 0, 160, 240)
        paint.fill()
        surface.write_to_png(str(mask))
        ctl("hyprveil", "image", str(mask))
        for index in range(5):
            frame("image", index)
        # Alpha can reveal only the sanitized public underlay, never the
        # protected source. The three independently sampled stripes are
        # opaque green, 50% green over blue, and fully transparent over blue.
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 320, 240)
        paint = cairo.Context(surface)
        paint.set_source_rgba(46/255, 191/255, 166/255, 1)
        paint.rectangle(0, 0, 106, 240)
        paint.fill()
        paint.set_source_rgba(46/255, 191/255, 166/255, 0.5)
        paint.rectangle(106, 0, 107, 240)
        paint.fill()
        surface.write_to_png(str(mask))
        image_command(mask, "alpha", 0)
        # Reusing an identical pathname is an explicit reload, not a cache hit.
        paint.set_operator(cairo.OPERATOR_SOURCE)
        paint.set_source_rgb(242/255, 207/255, 82/255)
        paint.paint()
        surface.write_to_png(str(mask))
        image_command(mask, "gold", 0)
        mask.unlink()
        previous = evaluate("snapshot()")
        ctl("reload")
        # Registered native settings follow the configuration file on reload.
        # This lab file contains no appearance/mode override, so its safe black
        # defaults supersede the preceding runtime-only image selection.
        reloaded = json.loads(ctl("hyprveil", "status"))
        if reloaded.get("mode") != "black" or reloaded.get("image_path") != "":
            raise RuntimeError("native configuration reload did not restore safe lab defaults")
        frame("black", 1, after_serial=previous["frameSerial"])
        missing = json.loads(ctl("hyprveil", "image", str(mask)))
        if not missing.get("error"):
            raise RuntimeError("missing mask file was not rejected")
        frame("black", 4)
        controls.append({"name": "native config reload restores black defaults; missing-path command rejected",
                         "ok": True, "native_mode": reloaded["mode"], "native_image_path": reloaded["image_path"],
                         "before_serial": previous["frameSerial"]})
        # These valid PNG headers declare hostile allocation sizes. No large
        # decoded surface is constructed by the fixture or the test oracle.
        for index, (width, height) in enumerate(((8193, 1), (8192, 4096), (0, 240))):
            invalid = artifacts / f"hostile-ihdr-{index}.png"
            write_header_png(invalid, width, height)
            image_command(invalid, "black", 10 + index)
            status = json.loads(ctl("hyprveil", "status"))
            if status.get("image_status") == "ready":
                raise RuntimeError("hostile PNG unexpectedly became a ready texture")
            controls.append({"name": "hostile PNG header rejected before allocation", "ok": True,
                             "declared_size": [width, height], "image_status": status.get("image_status")})
        encoded = artifacts / "oversized-encoded.png"
        write_header_png(encoded, 320, 240)
        with encoded.open("r+b") as file:
            file.truncate(17 * 1024 * 1024)
        image_command(encoded, "black", 13)
        controls.append({"name": "encoded PNG above 16 MiB yields black fallback", "ok": True,
                         "encoded_bytes": encoded.stat().st_size})
        ctl("hyprveil", "omit")
        frame("blue", 5)
        ctl("hyprveil", "black")
        frame("black", 2)
        ctl("hyprveil", "omit")
        frame("blue", 6)
        final_status = json.loads(ctl("hyprveil", "status"))
        ctl("plugin", "unload", str(plugin))
        loaded = False
        frame("black", 1)
        # A native fallback stream remains usable and a fresh plugin instance
        # can safely restore omission without changing native protection.
        previous = evaluate("snapshot()")
        if ctl("plugin", "load", str(plugin)) != "ok":
            raise RuntimeError("plugin reload failed")
        loaded = True
        ctl("hyprveil", "omit")
        frame("blue", 40, after_serial=previous["frameSerial"])
        ctl("plugin", "unload", str(plugin))
        loaded = False
        frame("black", 3)
        evaluate("stop()")
        (artifacts / "pipewire-graph.json").write_text(command(["pw-dump"]) + "\n")
        report = {"ok": True, "lab": state, "session": session, "frames": results, "controls": controls,
                  "plugin_status": final_status,
                  "plugin_sha256": plugin_sha256,
                  "transition_timeout_seconds": 5,
                  "freshness": "requestVideoFrameCallback serial; protected transition may drain an already queued public frame before expected sanitized content",
                  "path": "Chromium getDisplayMedia -> private XDP -> XDPH -> wlroots screencopy -> private PipeWire",
                  "selector": "lab-only deterministic picker of HV-TEST; genuine video capture, no fake media"}
        return_code = 0
    except (Exception, KeyboardInterrupt) as error:
        report.update(ok=False, error=str(error), plugin_sha256=plugin_sha256)
        print("portal smoke failed: " + str(error), file=sys.stderr, flush=True)
    finally:
        cleanup_errors = []
        if loaded:
            try:
                ctl("plugin", "unload", str(plugin))
            except Exception as error:
                cleanup_errors.append("plugin unload: " + str(error))
        for _, process, log in reversed(processes):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=3)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
            log.close()
        server.shutdown()
        server.server_close()
        report["cleanup_errors"] = cleanup_errors
        report["owned_services_stopped"] = all(process.poll() is not None for _, process, _ in processes)
        report["lab_lifecycle"] = "caller owns compositor; suite stops its own fixtures and private services"
        if cleanup_errors or not report["owned_services_stopped"]:
            report["ok"] = False
            return_code = 1
        (artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"ok": report["ok"], "report": str(artifacts / "report.json"),
                          "owned_services_stopped": report["owned_services_stopped"]}), flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
