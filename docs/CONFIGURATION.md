# Native Hyprland configuration

Hyprveil 0.4.0 registers its settings in Hyprland's native registry. Lua,
dispatchers, existing `hyprctl hyprveil` commands, the CLI and the Omarchy
panel use the same values. Runtime changes damage the capture scene
immediately, including frozen spoilers in static `copy_with_damage` streams.

The installed file `~/.config/hypr/hyprveil-settings.lua` is ordinary Lua:

```lua
local _, missing = hl.get_config("plugin.hyprveil.mode")
if not missing then
  hl.config({ plugin = { hyprveil = {
    mode = "spoiler",
    image_path = "",
    variant = "satin",
    color = "#ffffff",
    grain = 50,
    speed = 100,
    darkness = 50,
    eye = true,
    eye_size = 80,
  } } })
end
```

The availability check handles the first configuration pass before plugin
loading. Release loading remains subject to exact compositor ABI/ELF and
release pins. Live native admission starts with black replacement; a normal
configuration reload then applies user settings. Keep the guarded loader.

| Setting | Accepted value | Default |
| --- | --- | --- |
| `mode` | `black`, `omit`, `spoiler`, `image` | `black` (native admission) |
| `image_path` | Empty or an absolute PNG path, at most 4096 bytes | `""` |
| `variant` | `satin`, `telegram` | `satin` |
| `color` | Opaque `#RRGGBB`, normalized to lowercase | `#ffffff` |
| `grain` | Integer 0–100 | 50 |
| `speed` | Integer 0–200; zero freezes animation | 100 |
| `darkness` | Integer 0–100; 100 is black | 50 |
| `eye` | Boolean | `true` |
| `eye_size` | Integer 40–128, logical pixels | 80 |

Integers reject strings, fractions, booleans and out-of-range values. Enums
and colors reject unsupported spellings; `eye` requires a Lua boolean.
The native wrappers retain validation that this Hyprland version otherwise
loses when converting plugin options to Lua. A malformed atomic update makes
no partial change. PNG decode/resource failure keeps an opaque black
replacement, never an original window surface.

## Runtime Lua and native dispatchers

```lua
local p = hl.plugin.hyprveil
if p and p.configure then
  local state, err = p.configure({ variant = "telegram", grain = 35, speed = 70 })
  -- Success: config_api, mode, image_path and the seven-field appearance.
  -- Invalid input: nil, err; previous values remain intact.
end
```

`hl.plugin.hyprveil.status()` returns the same minimal configuration table.
It includes no window titles, application contents or client list. All public functions disappear when the plugin unloads.

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

The CLI exposes `hyprveil toggle`, `hide`, `show` and `reset-sharing`. These
commands use the public native API, without Omarchy helpers or a user module.
Their JSON output is the current minimal privacy snapshot. Choose normal
Lua bindings for `p.toggle()` and `p.reset_sharing()` as shown in the README.

## Persistence and precedence

Runtime Lua and dispatchers affect the current session. Saving the Lua file
persists settings. The CLI and panel update its managed literal table, save
a private previous-file backup, reload Hyprland, check configuration errors
and verify native acknowledgement. `hyprveil reload-config` and
**Перечитать Lua** explicitly reread the configuration.

On plain Hyprland the managed loader is placed before user configuration.
When an Omarchy bootstrap exists, it is placed just after that bootstrap.
Both arrangements allow normal user overrides later in the file. Later Lua configuration wins according to Hyprland's parse order.
The panel shows actual native mode and appearance even after a later override
or binding changes them. Partial CLI updates preserve other actual fields.

The panel updates literals between `BEGIN HYPRVEIL SETTINGS` and
`END HYPRVEIL SETTINGS`. Code outside the block is preserved; the controller
never executes Lua. If you put expressions inside that table, update it
through Lua or move custom code after the block. GUI saving refuses ambiguous
tables, unsafe files and concurrent edits. Install upgrades preserve an
existing settings file byte for byte. A later override that prevents a GUI
value is reported; the command does not claim an effective change.

The old `~/.config/hyprveil/config.json` retains admission pins, enable state
and a migration mirror. Its stale snapshot cannot override Lua on startup or
make a valid native change appear unknown. The previous settings backup is
`~/.local/state/hyprveil/last-settings.lua`; installation reports also contain
private backups.

## Rendering scope

This update changes configuration integration. The mask retains the existing
synthetic satin/dust shader and eye in the sanitized capture scene. Ordinary
window borders, rounding, local blur and Omarchy FX decoration are unchanged.
The frosted appearance uses synthetic light/dust; it does not sample or blur
protected application pixels. KMS/DRM capture still bypasses this scene.

The supported Lua configuration format is documented in the
[official Hyprland plugin guide](https://wiki.hypr.land/Plugins/Using-Plugins/).
