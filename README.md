<div align="center">

<img src="assets/hero.svg" alt="Hyprveil — keep your windows, choose what you share" width="960">

**Capture privacy for Hyprland. Your workspace stays yours.**

[![Hyprland](https://img.shields.io/badge/Hyprland-0.56.2-adcfc8?style=flat-square&labelColor=161c22)](docs/HOST-SETUP.md)
[![Version](https://img.shields.io/badge/preview-v0.4.0-ccd4de?style=flat-square&labelColor=161c22)](https://github.com/OBJLAKO/hyprveil/releases)
[![Tests](https://github.com/OBJLAKO/hyprveil/actions/workflows/tests.yml/badge.svg)](https://github.com/OBJLAKO/hyprveil/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/license-MIT-ccd4de?style=flat-square&labelColor=161c22)](LICENSE)

[Quick start](#quick-start) · [Lua configuration](docs/CONFIGURATION.md) · [Safety & compatibility](SECURITY.md) · [Omarchy companion](https://github.com/OBJLAKO/omarchy-hyprveil)

</div>

Hyprveil keeps private windows visible on your desktop while changing their appearance in **compositor screen captures**. Hide them completely, cover them with black, or replace them with a shimmering, fully opaque spoiler. Windows stay in place; your layout stays intact.

<div align="center">
<img src="assets/demo.gif" alt="Synthetic Hyprland capture demonstrating an animated privacy spoiler" width="960">
<br><sub>Real Hyprveil rendering in an isolated compositor. Every window in the demo is synthetic.</sub>
</div>

## What you get

- **Four capture modes.** Omission, black mask, animated spoiler, or your own PNG.
- **Two spoiler styles.** Soft Satin shimmer and Telegram-inspired drifting dust, with a centered crossed-out eye.
- **Native Hyprland configuration.** Ordinary Lua settings, window rules and bindings, plus a small CLI.
- **Live customization.** Tint, grain, motion speed, darkness, eye visibility and eye size.
- **Focused-window control.** Hide, share temporarily, or revoke temporary sharing through the public Lua API.
- **Independent core.** Works on Hyprland without Omarchy. The optional panel lives in [its own repository](https://github.com/OBJLAKO/omarchy-hyprveil).

The spoiler is generated from synthetic light and grain. Protected window pixels are never used to make a blurred preview.

## Quick start

**Preview release:** the installer currently supports the exact reviewed **Hyprland 0.56.2 ABI**. It checks compatibility before changing configuration. Other compositor versions are refused. See [requirements and installation](docs/HOST-SETUP.md).

With matching Hyprland development headers, a C++23 compiler, `make`, `pkg-config`, Python 3, Lua/`luac`, and Cairo, OpenSSL, zlib and GLES development libraries installed:

```sh
git clone https://github.com/OBJLAKO/hyprveil.git
cd hyprveil
python3 tools/setup.py install
```

The installer builds the plugin, checks its safety boundaries, computes the required admission pins and saves backups. **First installation activates after logging out and back in.** A supported, reviewed predecessor can update in the running session through a protected capture gate.

Then choose a style:

```sh
hyprveil spoiler
hyprveil configure --variant telegram --color '#adcfc8' --grain 35 --speed 70
```

To return to complete omission:

```sh
hyprveil omit
```

## Make it yours

Edit `~/.config/hypr/hyprveil-settings.lua`, or configure the plugin in your normal Hyprland Lua files:

```lua
local _, missing = hl.get_config("plugin.hyprveil.mode")
if not missing then
  hl.config({ plugin = { hyprveil = {
    mode = "spoiler",
    variant = "satin",       -- "satin" or "telegram"
    color = "#adcfc8",
    grain = 35,              -- 0–100
    speed = 70,              -- 0–200; 0 freezes motion
    darkness = 50,           -- 0–100
    eye = true,
    eye_size = 80,           -- 40–128 logical pixels
  } } })
end
```

Protect an application through a regular window rule:

```lua
hl.window_rule({
  match = { class = "^org[.]example[.]Secret$" },
  no_screen_share = true,
})
```

Choose unused keys for hide/show and temporary-sharing reset:

```lua
hl.bind("SUPER + ALT + H", function()
  local p = hl.plugin.hyprveil
  if p and p.toggle then p.toggle() end
end, { description = "Hide/show focused window in capture" })

hl.bind("SUPER + ALT + SHIFT + H", function()
  local p = hl.plugin.hyprveil
  if p and p.reset_sharing then p.reset_sharing() end
end, { description = "Revoke temporary capture sharing" })
```

CLI changes persist to the managed Lua settings file. Runtime Lua calls affect the current session. Partial updates preserve other actual settings. [Configuration reference →](docs/CONFIGURATION.md)

| Command | Action |
| --- | --- |
| `hyprveil toggle` | Hide or share the focused window |
| `hyprveil hide` / `hyprveil show` | Set an explicit focused-window choice |
| `hyprveil reset-sharing` | Revoke temporary sharing created by Hyprveil |
| `hyprveil spoiler` / `omit` / `black` | Select and save a capture mode |
| `hyprveil configure --eye off --speed 0` | Update appearance without changing mode |
| `hyprveil reload-config` | Reload Lua and verify native settings |
| `hyprveil status` | Inspect the actual loaded state |

## Optional Omarchy panel

[**omarchy-hyprveil**](https://github.com/OBJLAKO/omarchy-hyprveil) adds an appearance editor and a bar eye that shows the focused window's effective capture privacy. Left or middle click toggles privacy; right click opens the panel. It uses Hyprveil's public API and CLI.

## Know the boundary

Hyprveil protects the compositor capture paths covered by its renderer. **Direct KMS/DRM recording bypasses that renderer and is not protected.** Use a compositor or portal capture path for this feature.

The mask is always opaque. Protected direct-window captures, screen locking, unsupported output transforms and shader failure use black replacement. Popup and inherited privacy have dedicated regression coverage. Window decorations and local effects stay with Hyprland; Hyprveil changes the capture scene.

This release has a deliberately narrow ABI support range. Cold login, arbitrary drivers, every capture client and every third-party plugin are not covered by the saved validation. Read the [security policy](SECURITY.md), [technical review](docs/REVIEW.md) and [validation matrix](docs/VALIDATION.md) before relying on it.

## Development

```sh
make
python3 -m unittest discover -s tests -p 'test_*.py'
make test-session-guard test-png-guard test-privacy-policy \
     test-spoiler-pattern test-appearance test-native-config
```

Native capture tests run in explicitly marked isolated Hyprland sessions with synthetic windows. [Testing guide](docs/TESTING.md) · [Contributing](CONTRIBUTING.md)

If Hyprveil improves your screen-sharing setup, a star helps other Hyprland users find it. Bug reports with a compositor version and a synthetic reproduction are especially useful.

[MIT license](LICENSE) · Created by [OBJLAKO](https://github.com/OBJLAKO)
