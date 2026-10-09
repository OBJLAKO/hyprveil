# Native Hyprland configuration

Hyprveil 0.5.0 registers its settings in Hyprland's native registry. Lua,
dispatchers, existing `hyprctl hyprveil` commands, the CLI and the Omarchy
panel use the same values. Runtime changes damage the capture scene
immediately, including frozen spoilers in static `copy_with_damage` streams.

Settings use ordinary Lua. You can load the provided managed example at
`~/.config/hypr/hyprveil-settings.lua`, or put this in your own configuration:

```lua
local _, missing = hl.get_config("plugin.hyprveil.mode")
if not missing then
  hl.config({ plugin = { hyprveil = {
    mode = "spoiler",
    image_path = "",
    variant = "prism",
    color = "#ffffff",
    grain = 50,
    speed = 100,
    darkness = 50,
    eye = true,
    eye_size = 80,
    icon = "eye",
    icon_opacity = 75,
  } } })
end
```

The availability check handles the first configuration pass before plugin
loading. Use hyprpm autoload or the optional Omarchy setup adapter; the build
probe and native module enforce the
reviewed compositor/dependency ABI. Native admission starts with black
replacement; a normal configuration reload then applies user settings.

| Setting | Accepted value | Default |
| --- | --- | --- |
| `mode` | `black`, `omit`, `spoiler`, `image` | `black` (native admission) |
| `image_path` | Empty or an absolute PNG path, at most 4096 bytes | `""` |
| `variant` | `prism`, `signal`, `aurora`, `contour`, `radar`, `matte`, `error404`, `matrix`, `anonymous`, `glass` | `prism` |
| `color` | Opaque `#RRGGBB`, normalized to lowercase | `#ffffff` |
| `grain` | Integer 0–100 | 50 |
| `speed` | Integer 0–200; zero freezes animation | 100 |
| `darkness` | Integer 0–100; 100 is black | 50 |
| `eye` | Boolean; legacy visibility switch for any icon | `true` |
| `eye_size` | Icon size, integer 40–128 logical pixels | 80 |
| `icon` | `eye`, `lock`, `shield`, `none` | `eye` |
| `icon_opacity` | Integer 0–100 | 75 |

The older style names `satin`, `telegram` and `grid` are accepted and normalized
to `prism`, `signal` and `radar`. Additional aliases are `404` → `error404`,
`cmatrix` → `matrix`, `anon` → `anonymous`, and `liquid-glass`/`liquidglass` →
`glass`. `icon="none"`, `eye=false` or zero opacity
removes the icon. Icon opacity blends only with the opaque synthetic mask.
The `eye`/`eye_size` names remain compatible with older configuration files.
Update the optional CLI/panel together with the 0.5 core. Historical clients
that hardcode only the old two style names cannot interpret the new canonical
styles, even though native status keeps the API 1 field layout.

Integers reject strings, fractions, booleans and out-of-range values. Enums
and colors reject unsupported spellings; `eye` requires a Lua boolean.
The native wrappers retain validation that this Hyprland version otherwise
loses when converting plugin options to Lua. A malformed atomic update makes
no partial change. PNG decode/resource failure keeps an opaque black
replacement, never an original window surface.

`matte` is always still, including when speed is nonzero. Other variants stop
animating when speed is zero. Animation is scheduled only while a capture is
actively using it; a static private window on the local desktop does not
need a continuously running effect.

## Runtime Lua and native dispatchers

```lua
local p = hl.plugin.hyprveil
if p and p.configure then
  local state, err = p.configure({ variant = "signal", grain = 35, speed = 70 })
  -- Success: config_api, mode, image_path, appearance and icon settings.
  -- Invalid input: nil, err; previous values remain intact.
end
```

`hl.plugin.hyprveil.status()` returns the same minimal configuration table.
For API 1 compatibility, native `appearance` retains its original seven fields;
`icon` and `icon_opacity` are top-level status fields. The optional Python client
and panel combine these into their appearance model. It includes no window
titles, application contents or client list. All public functions disappear
when the plugin unloads.

For a binding in `~/.config/hypr/bindings.lua`, choose an unused key:

```lua
hl.bind("SUPER + ALT + V", function()
  local p = hl.plugin.hyprveil
  if not p or not p.configure then return end
  local next_mode = { black = "spoiler", spoiler = "omit", omit = "black", image = "black" }
  p.configure({ mode = next_mode[p.status().mode] })
end, { description = "Cycle capture privacy style" })
```

Native dispatchers and settings can also be called through IPC:

```sh
hyprctl dispatch 'function() return hl.plugin.hyprveil.mode("spoiler") end'
hyprctl dispatch 'function() return hl.plugin.hyprveil.cycle() end'
hyprctl getoption plugin:hyprveil:grain
hyprctl eval 'hl.plugin.hyprveil.configure({grain=25,speed=0})'
hyprctl eval 'hl.config({plugin={hyprveil={color="#c4b5fd"}}})'
```

The cycle is black → spoiler → omit → black; an image starts at black.
`hyprveil:mode image` uses the configured path. The native
`hl.get_config("plugin.hyprveil.grain")` and `getoption` agree, including
after an older appearance command or panel action.

## Window privacy API

`hl.plugin.hyprveil.active_privacy()` returns only `state`, `address`,
`stable_id`, `native_private` and `inherited`. The stable ID is a decimal
string, preserving the full native identity. No focused window returns
`state = "none"` and empty identities.

`toggle()` hides or shares the focused window. To act on an earlier snapshot,
use `set_hidden(snapshot.address, snapshot.stable_id, boolean)`. The native
setter verifies the same focused window and immutable identity before
changing `no_screen_share`; a closed or reused target is refused. Sharing
cannot override protection inherited from a private ancestor. Successful
actions return the five-field snapshot; rejected actions return `nil, err`.

`reset_sharing()` revokes only temporary sharing created by this API. It
preserves manual hiding, ordinary public windows and later explicit native
property choices. Normal rule reloads preserve the native manual priority.
The plugin also revokes its owned temporary exceptions during unload.
Before removing its capture hooks, it transfers every mapped window's
effective inherited privacy into native `no_screen_share=true`. This also
protects surviving orphan dialogs through hyprpm updates. These native
properties remain protective until an explicit window action changes them.

The CLI exposes `hyprveil toggle`, `hide`, `show` and `reset-sharing`. These
commands use the public native API, without Omarchy helpers or a user module.
Their JSON output is the current minimal privacy snapshot. Choose normal
Lua bindings for `p.toggle()` and `p.reset_sharing()` as shown in the README.

## Persistence and precedence

Runtime Lua and dispatchers affect the current session. Saving the Lua file
persists settings. The CLI and panel update an existing managed literal table, save
a private previous-file backup, reload Hyprland, check configuration errors
and verify native acknowledgement. `hyprveil reload-config` and
**Reload Lua** explicitly reread the configuration. If no managed settings file
exists, changes are explicitly reported as session-only. Custom/unsafe Lua is
refused before mutation; use the CLI's `--runtime` option for an intentional
temporary edit or edit your Lua file directly.

Place the settings `dofile` where you want it in your normal Hyprland config.
Later Lua configuration wins according to Hyprland's parse order. The legacy
installer places its managed loader before user overrides.
The panel shows actual native mode and appearance even after a later override
or binding changes them. Partial CLI updates preserve other actual fields.

The panel updates literals between `BEGIN HYPRVEIL SETTINGS` and
`END HYPRVEIL SETTINGS`. Code outside the block is preserved; the controller
never executes Lua. If you put expressions inside that table, update it
through Lua or move custom code after the block. GUI saving refuses ambiguous
tables, unsafe files and concurrent edits. Install upgrades preserve an
existing settings file byte for byte. A later override that prevents a GUI
value is reported; the command does not claim an effective change.

Only the legacy installer uses `~/.config/hyprveil/config.json` for admission
pins, enable state and a migration mirror. Standard hyprpm loading and the
standalone client do not require it. Its stale snapshot cannot override Lua on startup or
make a valid native change appear unknown. The previous settings backup is
`~/.local/state/hyprveil/last-settings.lua`; installation reports also contain
private backups.

## Rendering scope

All ten styles generate opaque synthetic pixels inside the sanitized capture
scene. The optional icon is drawn over that opaque replacement. Neither the
shader nor the icon samples or blurs protected application pixels. Ordinary
window borders, rounding, local blur and FX mesh deformation are outside the
mask's appearance settings. The glass effect simulates refraction of its own
synthetic scene; it cannot reveal the protected application. Use `eye=false`
or `icon="none"` if you prefer the 404 or Anonymous artwork without an overlaid
icon. KMS/DRM capture bypasses this scene.

The supported Lua configuration format is documented in the
[official Hyprland plugin guide](https://wiki.hypr.land/Plugins/Using-Plugins/).
