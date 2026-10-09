# Legacy session-pinned manual trial

For ordinary installation use [hyprpm](HOST-SETUP.md). This helper is retained
for explicitly reviewed development/recovery trials; do not combine its loader
with hyprpm in the same session.

The manual helper loads a copied, pinned release into one selected Hyprland
process. It adds no autoload, keybinding or shell plugin and does not rewrite
desktop configuration. Use the isolated tests first: native plugins share the
compositor process and a defect can end that session.

Do not prepare a second release while an installed Hyprveil copy is loaded.
Use its managed controller or a clean disposable compositor. A managed online
update needs the capture guard rather than a manual unload/load sequence.

## Prepare the exact session

Build against the supported installed headers:

```sh
make -B all build/abi-probe
hyprctl -j instances
hyprctl -i INSTANCE_SIGNATURE -j version
```

Replace the placeholders below with the selected instance's actual PID,
signature and `abiHash`. Run from the checkout:

```sh
python3 tools/session.py prepare --pid PID --signature INSTANCE_SIGNATURE --abi-hash ABI_HASH
python3 tools/session.py load
python3 tools/session.py status
```

The helper validates PID/start time, instance signature, Unix socket peer,
runtime ownership and mapped release before control. New loading also checks
the header ABI and running/installed compositor ELF. Snapshots and the immutable
release copy remain in ignored local `artifacts/`; private state is mode `0600`.

When a trial marker exists, native admission requires the owned `0700` runtime
and a single-link, non-symlink `0600` marker for that exact process and instance.
Normal hyprpm loading needs no marker. The helper's
two-hour marker authorizes admission only: expiry does not unload an already
loaded plugin or choose a different effect.

The exact release needs permission from initial configuration or explicit
compositor consent. Runtime `hl.permission` evaluation is not treated as a
grant. A refused or timed-out load removes admission so a delayed consent
response cannot load it later. Cancellation leaves an owned `cancelled/0`
tombstone, rather than deleting the marker. A new explicit prepare/start can
rearm it; compositor runtime cleanup removes it. The helper does not weaken
permissions.

Manual loading requires an acknowledged black initial mode. Existing Lua
settings selecting another mode during load reparse can cause this strict
check to refuse; it requests black on failure. Managed installation handles
native configuration loading separately.

## Change the capture effect

```sh
python3 tools/session.py spoiler
python3 tools/session.py configure --variant aurora --grain 35 --speed 70
python3 tools/session.py omit
python3 tools/session.py image /absolute/path/replacement.png
python3 tools/session.py black
python3 tools/session.py status
```

Protected windows remain visible locally at the same position and size.
Spoilers are opaque procedural masks with a configurable eye, lock or shield. Private
popups are omitted; direct protected-window exports are black. Malformed
images or failed shader resources fall back to black.

Collect evidence with synthetic content. Compare a compositor monitor/region
export with the scene's local reference, including public foreground overlap,
transient dialogs and a live stream. The manual helper provides no capture
command. Native `dump-local` is refused before mirror readback in live sessions.

Direct KMS/DRM capture bypasses protection. Selecting a portal-capable recorder
is insufficient unless its actual capture route is established. Already
queued or delivered frames cannot be retracted after a privacy change.

## End the trial

```sh
python3 tools/session.py black
python3 tools/session.py unload
```

`black` retains the sanitized scene. `unload` removes the custom renderer and
cancels its trial marker. The current module promotes effective inherited
privacy to native properties before unloading, while older releases did not.
Plugin inheritance no longer applies to future windows while unloaded. Native
masking showed deformed-edge disclosure with the reviewed FX plugin, so
unloading is not equivalent to selecting black or omit.

Artifacts remain locally. After compositor restart, the helper refuses the
old identity and does not reload anything automatically. See
[REVIEW.md](REVIEW.md) for lifecycle and capture limits.
