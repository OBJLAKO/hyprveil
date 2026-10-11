# Changelog

## 0.6.0 — 2026-10-11

Restore native cursor zoom in monitor and region captures, including rigid
and detached cameras. Match native fullscreen/hidden/fully faded selection
and window rounding before drawing a replacement.

Add opaque Color masks, four PNG placement modes (`fill`, `fit`, `tile`,
`stretch`), trusted local procedural GLSL and custom PNG icons. Shader errors
fall back to the chosen opaque mask color. Restore the sensitive-content slash
on the Eye icon. Existing Black mode remains strictly black, and existing
PNG settings retain Stretch until explicitly changed.

Pass synthetic replacements through the reviewed OmaFX 0.4.0 mesh transformer.
Unknown or mixed transformer chains keep the plain mask. See
[rendering compatibility](docs/COMPATIBILITY.md) for the supported path and
the remaining Glasscope capture limitation. No additional plugin is required.

Extend the typed native API and optional client with additive settings while
retaining API 1 and old-client compatibility. The Omarchy panel exposes the new
controls with partial autosave; appearance changes do not reload Hyprland.
See [0.6.0 validation](docs/VALIDATION-0.6.md) for measured rendering results.

## 0.5.0 — 2026-10-09

Install the native core through hyprpm on the reviewed Hyprland 0.56.2 ABI.
Omarchy and the optional Python client are no longer core loading dependencies.
The companion panel bundles its client and identifies session-only edits.

Choose Prism, Signal, Aurora, Contour, Radar, static Matte, 404, Matrix,
Anonymous or Liquid Glass. Animated styles
freeze at speed zero and schedule work only during capture. Select a redrawn
eye, lock, shield or no icon, with independent size and opacity. Old style names
and eye settings remain accepted by the new release.

Preserve mapped inherited privacy on unload, refuse unreviewed native ABIs,
normalize Cairo floating-point PNG surfaces before GPU upload, freeze legacy
upgrade candidates before mutation and retain explicit cancelled trial state
against delayed permission responses. Explicit glyph integer precision and valid
GLSL arithmetic remove two cross-driver shader hazards. Controller output and subprocess lifetime
are now bounded. See [validation scope](docs/VALIDATION.md) and
[migration instructions](docs/HOST-SETUP.md#migrate-from-the-old-installer).
