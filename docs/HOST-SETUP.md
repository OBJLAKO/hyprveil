# Installation and compatibility

## Native hyprpm installation

Hyprveil is independent of Omarchy. The core needs a C++23 compiler, Make,
pkg-config, matching Hyprland headers, Cairo, zlib and GLES development files.
hyprpm obtains compositor headers and requires its own build tools; see the
[official installation guide](https://wiki.hypr.land/Plugins/Using-Plugins/).
Python, OpenSSL and Lua/luac are not required to build the normal core.

```sh
hyprpm add https://github.com/OBJLAKO/hyprveil
hyprpm enable hyprveil
hyprpm reload
hyprctl hyprveil status
```

The reviewed target is Hyprland 0.56.2, commit
`efb50993780079460b0cbed1363e2166a2de1d9f`, with native dependency ABI
`aq_0.15_hu_0.14_hg_0.5_hc_0.1_hlg_0.6`. Both the build probe and module refuse
an unreviewed ABI, including an otherwise matching local rebuild. This is
ABI-scoped support, not a promise that every compiler, distro, driver or plugin
combination has been tested.

The native module loads through the standard plugin API. It no longer needs
an installation receipt, immutable per-hash release directory, Python startup
controller or temporary live-session marker. It starts with black replacement
and applies available native Lua configuration through the compositor's reload.
Diagnostic capture of the local mirror remains restricted to an explicitly
marked isolated test session.

Legacy manual trials leave a session marker, or a cancellation tombstone after
a timeout/unload. If one exists, it must validate before loading; absence is
the normal hyprpm path. Cold-login migration also clears that legacy runtime
state. Do not remove a pending trial's cancellation record to bypass refusal.

Add hyprpm autoload and permissions to your normal Hyprland Lua configuration:

```lua
hl.permission("^/usr/(bin|local/bin)/hyprpm$", "plugin", "allow")
hl.on("hyprland.start", function() hl.exec_cmd("hyprpm reload") end)
```

Do not duplicate an existing hyprpm startup hook. Permissions are initialized
at compositor startup; a new permission entry may require another login.

## Settings and keys

The [settings example](../examples/hyprveil-settings.lua) contains a literal
eleven-field table that the optional CLI and panel can update safely. Copy it to
`~/.config/hypr/hyprveil-settings.lua`, then include it with:

```lua
dofile(os.getenv("HOME") .. "/.config/hypr/hyprveil-settings.lua")
```

Alternatively, [hyprveil-hyprpm.lua](../examples/hyprveil-hyprpm.lua) combines
autoload, settings inclusion, window toggle and temporary-sharing reset.
Review its shortcuts before using it. Native `hl.config` may also live directly
in your own config; the panel preserves custom Lua and reports temporary edits
when it cannot update a managed literal block.

## Optional CLI and Omarchy panel

From a source checkout, `make install-cli` installs `cli.py` and its support
module under `~/.local/libexec/hyprveil`, plus `~/.local/bin/hyprveil`.
`PREFIX` and `DESTDIR` are supported for packaging. Python 3 is required only
for this client or the companion panel. The client does not load plugins.

```sh
omarchy plugin add https://github.com/OBJLAKO/omarchy-hyprveil.git --enable
```

The first-use setup action opens a floating terminal installer, following the
Omarchy Liquid Glass workflow. It installs the reviewed core via hyprpm and
backs up files before adding fenced Lua startup/settings blocks. An existing
legacy loader requires the cold-login migration below. Removal uses the
installer’s ownership record and checksums. See the companion guide for the
complete setup and rebuild actions. The Omarchy adapter selectively loads
only its validated Hyprveil cache artifact; it does not run a broad plugin
reload during first-use activation. Existing externally managed cores retain
their own loader.

The companion bundles the native API client and does not require `make
install-cli`. Without a managed settings file, it clearly identifies changes
as applying to the current session.

## Migrate from the old installer

Migrate between login sessions. Older releases have an inherited-dialog
unload defect; do not hot-swap them while relying on capture privacy.

1. Stop screen sharing and save your work. Back up the Hyprland configuration.
2. In `~/.config/hypr/hyprland.lua`, replace the exact block between
   `-- BEGIN HYPRVEIL MANAGED AUTOLOAD v2` and its matching `END` marker (or the
   older v1 block) with the settings `dofile` above. Keep
   `hyprveil-settings.lua`; it retains your appearance choices. Remove any
   additional custom startup command that invokes the legacy controller.
3. Add the hyprpm permission and startup hook. Log out and back in so the old
   module is gone and the new permissions take effect.
4. Run the hyprpm installation commands, then verify `hyprctl hyprveil status`
   and the capture behavior with synthetic content.
5. Update the companion panel. If you want a shell client, run `make install-cli`
   to replace the legacy launcher. Keep old release files/backups until you
   have checked the migration; they do not load themselves.

The old `tools/setup.py` and protected upgrade bridge remain for explicitly
reviewed predecessor recovery. They are not the normal installation path.
Do not mix that loader with hyprpm. The protected bridge now freezes both
candidate binaries before touching the old session, so a parallel build
cannot replace them mid-upgrade.

## Troubleshooting

- **Headers missing or wrong:** run `hyprpm update`; inspect its build log.
  Do not bypass the native ABI refusal. A stale system `hyprland.pc` can point
  at a deleted headers cache even when system headers exist.
- **Plugin absent:** inspect `hyprpm list`, `hyprpm reload` and permission
  prompts; `hyprctl plugin list` confirms actual loaded modules.
- **Settings temporary:** create/load the managed example or edit your own
  native Lua config. Runtime-only changes are intentionally not presented as
  durable saves.
- **Black instead of a style:** check native status and config errors. Black
  is the fallback for unavailable resources, bad images, lock and unsupported
  capture transforms.
