# Native Hyprland configuration

Hyprveil registers its settings in Hyprland's native registry. Lua,
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
    image_fit = "stretch",
    mask_color = "#000000",
    shader_path = "",
    icon_path = "",
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
| `mode` | `black`, `color`, `omit`, `spoiler`, `image` | `black` (native admission) |
| `image_path` | Empty or an absolute PNG path, at most 4096 bytes | `""` |
| `image_fit` | `fill`, `fit`, `tile`, `stretch` | `stretch` |
| `mask_color` | Opaque `#RRGGBB` fill, normalized to lowercase | `#000000` |
| `shader_path` | Empty or an absolute `.frag` source path, at most 4096 bytes | `""` |
| `icon_path` | Empty or an absolute custom PNG path, at most 4096 bytes | `""` |
| `variant` | `prism`, `signal`, `aurora`, `contour`, `radar`, `matte`, `error404`, `matrix`, `anonymous`, `glass` | `prism` |
| `color` | Spoiler accent in `#RRGGBB`, normalized to lowercase | `#ffffff` |
| `grain` | Integer 0–100 | 50 |
| `speed` | Integer 0–200; zero freezes animation | 100 |
| `darkness` | Integer 0–100; 100 is black | 50 |
| `eye` | Boolean; legacy visibility switch for any icon | `true` |
| `eye_size` | Icon size, integer 40–128 logical pixels | 80 |
| `icon` | `eye`, `lock`, `shield`, `none`, `custom` | `eye` |
| `icon_opacity` | Integer 0–100 | 75 |

The older style names `satin`, `telegram` and `grid` are accepted and normalized
to `prism`, `signal` and `radar`. Additional aliases are `404` → `error404`,
`cmatrix` → `matrix`, `anon` → `anonymous`, and `liquid-glass`/`liquidglass` →
`glass`. `icon="none"`, `eye=false` or zero opacity
removes the icon. Icon opacity blends only with the opaque synthetic mask.
The `eye`/`eye_size` names remain compatible with older configuration files.
`icon="custom"` uses `icon_path` and the same size, visibility and opacity controls.
Update the optional CLI/panel together with the core. Historical clients
that hardcode only the old two style names cannot interpret the new canonical
styles, even though native status keeps the API 1 field layout.

`color` retains its original role as the spoiler accent. The separate
`mask_color` selects the opaque fill in `mode="color"`, the background behind
transparent PNG pixels or `fit` letterboxing, and the custom-shader fallback.
The legacy `mode="black"` always produces black, regardless of `mask_color`;
safety fallback commands continue to use that mode. Existing Lua tables may
omit the four new fields: they receive the defaults above.

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
  -- Success: config_api, mode, image_path, appearance and rendering settings.
  -- Invalid input: nil, err; previous values remain intact.
end
```

`hl.plugin.hyprveil.status()` returns the same minimal configuration table.
For API 1 compatibility, native `appearance` retains its original seven fields;
`icon` and `icon_opacity` are top-level status fields. `mask_color`, `image_fit`,
`shader_path` and `icon_path` are also top-level fields. The optional Python client
combines the two icon fields into its nine-field appearance model and retains
the other rendering fields separately. CLI `status` exposes these fields and
`image_path` in both its outer envelope and nested native status. It includes no window
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

## Image layout and color fill

```sh
hyprveil color '#18212b'
hyprveil black
hyprveil image /absolute/path/replacement.png --fit fit
hyprveil configure --image-fit fill --mask-color '#18212b'
hyprveil configure --image-path /absolute/path/another.png
```

`color` without a color argument reuses the current `mask_color`, initially
black. `black` remains an independent, strictly black mode. A partial
`configure` command preserves the active mode and every untouched setting;
changing `image_path` while another mode is active prepares the next image.
An empty image path is allowed only while image mode is inactive.

`stretch` fills the window and can change the image's proportions; it preserves
the previous default behavior. `fill` preserves proportions and crops the
centered image to cover the window. `fit` preserves the complete centered image
and fills the remaining area with `mask_color`. `tile` repeats the image at its
native size. Transparent image pixels blend over `mask_color`, never over the
protected application. PNGs must be regular files within the existing bounds:
16 MiB encoded, 8192 pixels per edge, 16,777,216 pixels and 64 MiB decoded.

## Custom spoiler shaders and PNG icons

```sh
hyprveil spoiler --shader-path /absolute/path/effect.frag
hyprveil configure --shader-path ''
hyprveil configure --icon custom --icon-path /absolute/path/icon.png
hyprveil configure --icon none --icon-path ''
```

A nonempty `shader_path` replaces the selected built-in spoiler while
`mode="spoiler"` is active. Clearing it restores the selected built-in variant.
The file must be a nonempty regular UTF-8 `.frag` source of at most 64 KiB.
The shader and custom-icon paths must be absolute and contain no symlinks,
including linked parent directories. Custom icons use the same PNG bounds as
replacement images; their transparent pixels reveal only the opaque mask below.
To clear `icon_path`, select a built-in icon or `none` in the same atomic update.

Provide a function, without `#version`, a separate `main` or resource declarations:

```glsl
vec4 hyprveil(vec2 uv) {
    float wave = 0.5 + 0.5 * sin(uv.x * 12.0 + time);
    return vec4(mix(maskColor, tint, wave * (1.0 - darkness)), 1.0);
}
```

The wrapper supplies `fullSize` (`vec2`, replacement size), `time` (`float`,
bounded seconds scaled by `speed / 100`), `tint` (`vec3`, the spoiler accent),
`grainAmount`, `darkness` (both `float`, normalized to 0–1), and `maskColor`
(`vec3`). `uv` spans 0–1. Zero speed freezes time. A shader whose `time` uniform
is optimized out needs no continuous animation timer.

The wrapper controls rounding, clamps output RGB and forces opaque alpha;
nonfinite RGB uses `mask_color`. It exposes no protected-window or framebuffer
sampler. Preprocessor directives, backslashes, custom uniforms, samplers,
images, atomics and resource buffers are refused outside comments. Invalid
GLSL can still be saved as a configuration choice: compilation or resource
failure renders the opaque `mask_color` fallback. Missing or unsafe newly
selected files may be refused before changing native settings. If a retained
file disappears later, the renderer keeps an opaque replacement and the
`black` command remains available.

Use trusted local shaders. The source-size bound and resource restrictions do
not bound arbitrary GLSL arithmetic or loops; expensive custom code can stall
the GPU. The built-in styles remain the supported option for predictable cost.

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
persists settings. The CLI and panel apply the requested fields through the
native API, update an existing managed literal table, save a private previous-file
backup and verify native acknowledgement. Ordinary saves do not reload Hyprland
or execute custom Lua. `hyprveil reload-config` and
**Reload Lua** explicitly reread the configuration. If no managed settings file
exists, changes are explicitly reported as session-only. Custom/unsafe Lua is
refused before mutation; use the CLI's `--runtime` option for an intentional
temporary edit or edit your Lua file directly.

Place the settings `dofile` where you want it in your normal Hyprland config.
On the next full configuration reload, later Lua configuration wins according
to Hyprland's parse order. The legacy
installer places its managed loader before user overrides.
The panel shows actual native mode and appearance even after a later override
or binding changes them. Partial CLI updates preserve other actual fields.

The panel updates literals between `BEGIN HYPRVEIL SETTINGS` and
`END HYPRVEIL SETTINGS`. Code outside the block is preserved; the controller
never executes Lua. If you put expressions inside that table, update it
through Lua or move custom code after the block. GUI saving refuses ambiguous
tables, unsafe files and concurrent edits. Install upgrades preserve an
existing settings file byte for byte. A save confirms the current native result;
it cannot predict an override that custom code will apply on a future reload.
Use `dofile` as shown in the examples: in the reviewed Hyprland ABI it does not
register this settings file with the automatic configuration watcher. A custom
`require` or wildcard watcher can still trigger Hyprland reloads when files change.

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
window borders and local blur remain outside the mask's appearance settings.
Rounding follows the native window; the reviewed OmaFX mesh can deform the
synthetic replacement. See [rendering compatibility](COMPATIBILITY.md) for
the supported transformer and the Glasscope capture limitation.
The glass effect simulates refraction of its own
synthetic scene; it cannot reveal the protected application. Use `eye=false`
or `icon="none"` if you prefer the 404 or Anonymous artwork without an overlaid
icon. KMS/DRM capture bypasses this scene.

The supported Lua configuration format is documented in the
[official Hyprland plugin guide](https://wiki.hypr.land/Plugins/Using-Plugins/).
