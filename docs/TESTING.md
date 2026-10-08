# Testing with synthetic windows

Load, capture and teardown tests belong in a disposable compositor. Offline
CI covers boundary logic and controller transactions; it cannot establish
rendering correctness. Test only synthetic content and keep a failed report
failed even if some earlier assertions passed.

## Offline checks

A C++23 compiler, Make, pkg-config, Cairo/zlib development files, Python with
Pycairo and Lua 5.4 are sufficient for these checks. Full native compilation
additionally requires the exact supported Hyprland development environment.

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
make test-session-guard test-png-guard test-privacy-policy test-spoiler-pattern test-appearance test-native-config
```

The GitHub workflow runs those checks on Ubuntu 24.04. It does not load a
Hyprland plugin, exercise GL, start a portal or claim compatibility with an
Ubuntu compositor package.

Before a full test, rebuild all native outputs against current headers:

```sh
make -B all upgrade-guard build/abi-probe
```

## Nested backend

The reviewed Hyprland/Aquamarine builds need a rendering allocator even for
headless outputs. Old wlroots backend environment variables and
`AQ_BACKEND=headless` are not an established software-only startup route.
The harness uses a nested Wayland backend to obtain a render-node allocator,
then creates a synthetic headless output inside that compositor.

The explicitly selected parent Wayland socket is a host canvas. A brief
nested surface and GPU/CPU load are expected, but the harness does not load
Hyprveil into the parent or capture its windows. After startup, it removes
the nested Wayland output and directs the synthetic scene to `HV-TEST`.

Relevant sources: [Hyprland backend setup](https://github.com/hyprwm/Hyprland/blob/efb50993780079460b0cbed1363e2166a2de1d9f/src/Compositor.cpp),
[Aquamarine allocator selection](https://github.com/hyprwm/aquamarine/blob/v0.15.0/src/backend/Backend.cpp),
[Wayland backend](https://github.com/hyprwm/aquamarine/blob/v0.15.0/src/backend/Wayland.cpp)
and [headless outputs](https://github.com/hyprwm/aquamarine/blob/v0.15.0/src/backend/Headless.cpp).

## Isolation contract

The launcher constructs a new child environment:

- Private `0700` runtime and separate HOME/config/cache/data trees; explicit
  minimal Lua configuration without desktop bootstrap or user autostart.
- `LIBSEAT_BACKEND=hyprveil-disabled` prevents physical seat acquisition before
  DRM output enumeration. Preserve this even when testing outside a sandbox.
- No inherited compositor instance, D-Bus activation, session-manager display
  variables or systemd notification socket; `HYPRLAND_NO_SD_VARS`,
  `HYPRLAND_NO_SD_NOTIFY` and `HYPRLAND_NO_RT` are set.
- Exactly one selected parent socket connection, passed as `WAYLAND_SOCKET`
  only to the new compositor. Fixtures and capture tools never inherit it.
- Every subsequent control/capture validates the marked lab runtime, PID,
  signature and socket peer. Host `/run/user/...` runtimes are refused as lab
  mutation/capture destinations.
- XWayland stays disabled except in the guarded X11 suite. Cleanup stops only
  processes the runner owns, never every process named Hyprland.

The seat guard follows [libseat backend selection](https://github.com/kennylevinsen/seatd/blob/0.9.3/libseat/libseat.c)
and [Aquamarine DRM initialization](https://github.com/hyprwm/aquamarine/blob/v0.15.0/src/backend/drm/DRM.cpp).
`lab.py exec` sets the verified lab environment for trusted commands; it is
not a general process sandbox.

## Run a synthetic suite

Full rendering fixtures use GTK4/PyGObject, `grim`, Wayland client development
files and `wayland-scanner`. Suites may need additional dependencies below.
Use the actual parent display name, not an inferred different instance:

```sh
python3 tools/spoiler_smoke.py --parent-runtime "$XDG_RUNTIME_DIR" --parent-display "$WAYLAND_DISPLAY" --customization
python3 tools/native_config_smoke.py --parent-runtime "$XDG_RUNTIME_DIR" --parent-display "$WAYLAND_DISPLAY"
python3 tools/lock_smoke.py --parent-runtime "$XDG_RUNTIME_DIR" --parent-display "$WAYLAND_DISPLAY"
```

These runners create and stop their own labs. For the original smoke and
portal runners, start a separate lab in one terminal:

```sh
python3 tools/lab.py run --parent-runtime "$XDG_RUNTIME_DIR" --parent-display "$WAYLAND_DISPLAY"
```

Use the printed lab directory in another terminal; replace the placeholder:

```sh
python3 tools/smoke.py --lab-dir /tmp/hv-EXAMPLE
python3 tools/lab.py stop --lab-dir /tmp/hv-EXAMPLE
```

The lab-only `hyprveil dump-local` diagnostic reads the previous local mirror
before rebuilding a sanitized capture. Both local and exported images belong
to the synthetic output. This diagnostic is refused in live admission before
readback. Never crop the personal desktop to obtain a local test reference.

## Runtime matrix

| Runner | Scope and extra requirements |
| --- | --- |
| `smoke.py` | Local/capture distinction, monitor/region, omit/image/black, PNG fallback, foreground stacking and geometry. Requires an existing marked lab. |
| `spoiler_smoke.py` | Opaque animation, persistent `copy_with_damage`, frozen settings, fullscreen and appearance extrema. `--force-shader-failure` exercises the black latch. |
| `native_config_smoke.py` | All nine native fields and mutation routes, invalid atomic patches, bad-file black fallback, focused identity, sharing reset and callback teardown. |
| `popup_smoke.py --inherit-dialog --parent-lifecycle` | Real GTK parent/dialog and protocol-role destruction, retained privacy and direct-window export. |
| `x11_smoke.py` | Actual X11 transient ownership, pending consent and parent lifetime; needs XWayland/XCB. See [X11-TESTING.md](X11-TESTING.md). |
| `permission_smoke.py --case privacy` | Allocated native pending snapshot invalidated before the first approved capture. Repeat with black, omit and image. |
| `portal_smoke.py` | Continuous Chromium readbacks through private XDP/XDPH, PipeWire and a private D-Bus session. Requires an existing marked lab and those services. |
| `lock_smoke.py` | Lock acknowledgment ordering, black locked export and rotated output refusal. |
| `cursor_smoke.py` | Requested/cursor-free/region cursor behavior over the synthetic scene. |
| `fx_smoke.py --fx-plugin /absolute/path/omarchy-fx.so` | Actual private/public transformers and pixel safety. Requires a reviewed compatible FX build. Does not test visual mask inheritance. |
| `stress.py` | Movement, resize, scale, reload, unload/reload and bounded repeated captures. |
| `upgrade_smoke.py --old-plugin /absolute/path/reviewed-old.so` | Exact allowlisted predecessor, held gap and weak retention handoff. An optional `--watcher` tests the independent GUI stream. |

Read each runner's `--help` before adding options. An unavailable dependency,
refused parent or failed allocator is a setup failure, not a rendering pass.
An unknown predecessor cannot be substituted to make an upgrade test run.

## Evidence and acceptance

Reports and PNGs are generated under ignored `artifacts/`. Record source and
binary SHA-256, compositor version/ABI, protocol, output dimensions, mode and
fixture geometry alongside each result. Publish only synthetic evidence;
remove host paths, process identifiers and unrelated environment metadata.

A passing report requires `ok: true`, all expected assertions, confirmed lab
shutdown and an empty cleanup-error list. Pixel checks verify private-color
absence over the whole export, opacity where required, correct public content,
local private visibility and unchanged geometry/native flags. A property
query, successful command or plugin-list entry cannot substitute for pixels.

For stream policy changes, establish a new frame serial after the mutation.
For pending-consent tests, prove a snapshot actually existed and inspect the
first accepted frame. For FX tests, establish attached transformers rather
than relying on plugin membership. Unload assertions measure state separately
from native pixel privacy.

Failures stay in their original reports; later readback-trigger captures must
not replace a failed first frame. Stop owned lab processes in `finally`, check
for remaining descendants, and retain the failure for diagnosis. Bounded
resource observations do not establish absence of all CPU/GPU leaks. See
[VALIDATION.md](VALIDATION.md) for completed results and
[REVIEW.md](REVIEW.md) for accepted behavior and remaining gaps.
