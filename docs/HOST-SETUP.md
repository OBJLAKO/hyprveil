# Installation and desktop integration

Hyprveil is a native Hyprland plugin. The core works without Omarchy. The
optional [Omarchy interface](https://github.com/OBJLAKO/omarchy-hyprveil) adds
the bar indicator and appearance editor through the core's public API.

## Supported compositor

The reviewed implementation targets Hyprland 0.56.2, commit
`efb50993780079460b0cbed1363e2166a2de1d9f`, with this exact header ABI:

```text
efb50993780079460b0cbed1363e2166a2de1d9f_aq_0.15_hu_0.14_hg_0.5_hc_0.1_hlg_0.6
```

Matching the version number alone is insufficient. The plugin hooks internal
compositor functions and checks their module identity. Installation compares
the header ABI, running process and installed compositor ELF before changing
configuration. Unsupported builds are refused. After a package update, log
into the updated compositor and rebuild against its matching headers.

## Install and inspect

Run from the core checkout after installing the dependencies in the README:

```sh
python3 tools/setup.py install
hyprveil status
```

Installation force-rebuilds the plugin, upgrade guard, ABI probe and six C++
boundary test programs. It installs an immutable per-user release, the
`hyprveil` controller and an ordinary Lua loader. First installation registers
the exact plugin permission for the next compositor start. A configuration
reload does not grant a newly added native permission.

If several compositors are running, pass `--signature INSTANCE_SIGNATURE`
to the installer. Inspect its report before resolving a refusal. The installer
does not disable permission enforcement or implicitly accept a different copy.

## Settings and controls

The native registry is authoritative. The managed settings file is
`~/.config/hypr/hyprveil-settings.lua`; see
[CONFIGURATION.md](CONFIGURATION.md) for all nine settings, Lua bindings,
precedence and atomic updates.

```sh
hyprveil spoiler
hyprveil configure --variant telegram --grain 35 --speed 70
hyprveil omit
hyprveil black
hyprveil toggle
hyprveil reset-sharing
```

`omit` removes protected surfaces from the capture scene. `black`, `spoiler`
and `image` replace their normal window rectangles. Local visibility and
geometry remain unchanged. Protected direct-window exports are opaque black.
The spoiler is synthetic; it does not blur protected application pixels.

Window actions pin both address and immutable window ID. Sharing cannot
bypass protection inherited from a private ancestor. `reset-sharing` revokes
only temporary exceptions owned by this API, preserving later explicit native
choices and ordinary public windows.

CLI and panel saves update a bounded literal Lua block, retain a private
previous-file backup, reload and verify the actual native result. Code outside
the block is preserved. A later user override that prevents the requested
result is reported rather than presented as a successful save.

## Updating a loaded release

Only explicitly reviewed predecessor ELF hashes are eligible for an online
update. The temporary guard blocks capture commits before replacement,
snapshots weak identities of effectively private windows and transfers
inherited/orphan retention into the new plugin in verified black mode.
Previous actual mode, configured image path and appearance are restored before
capture is released.

An unrecognized predecessor or ABI is refused. Failed handoff leaves the
capture hold in place; the controller does not unload the guard to recover
capture at the cost of exposing content. Keep the failure report and resolve
the recorded cause before retrying. A remaining guard is not a completed update.

## Capture backend and recovery

Use compositor-mediated monitor or region capture, including a portal client
that ultimately uses Hyprland's capture pipeline. Direct KMS/DRM recording
bypasses Hyprveil and native compositor privacy. A recorder's successful
launch does not establish that it used the supported path.

Use `hyprveil black` or `hyprveil omit` to retain the sanitized renderer while
changing appearance. `stop`, `disable` or native unload restores the underlying
compositor behavior. That native behavior exposed deformed private edges in
the reviewed FX configuration; a privacy flag alone is not a guarantee after
unloading.

These are instructions, not a report of any reader's installed state. See
[VALIDATION.md](VALIDATION.md) for tested scope and
[MANUAL-TRIAL.md](MANUAL-TRIAL.md) for a session-pinned trial.
