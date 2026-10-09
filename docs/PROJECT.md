# Project architecture

Hyprveil keeps selected windows visible locally while changing their
compositor-mediated capture representation. It serves screen sharing,
recording and screenshot workflows that need a clear replacement style
without moving a protected window.

## Two independent repositories

- **Hyprveil core** owns capture rendering, privacy inheritance, native Lua
  configuration and per-window actions. Standard hyprpm manages its build,
  installation and loading. The core
  works on supported plain Hyprland installations.
- **[omarchy-hyprveil](https://github.com/OBJLAKO/omarchy-hyprveil)** owns the
  optional bar, panel and first-use terminal installer. Its selective startup
  helper activates only its validated hyprpm-built Hyprveil artifact. It calls the public core API and reports the focused
  window's effective privacy, including inherited protection.

The GUI needs no private user helper module. The core has no dependency on
Omarchy bootstrap functions, Quickshell or the GUI's installed files.

## Rendering and policy

The capture pass rebuilds a sanitized scene. Protected surfaces are omitted
or replaced at their normal stacking position with black, an image or an
opaque synthetic spoiler. The shader has no protected-content sampler.
Public foreground windows stay above the replacement. Local rendering uses
the ordinary compositor path.

Effective privacy includes the native window property and a bounded actual
parent chain. Weak retention protects surviving transient children after a
private ancestor closes. Policy changes invalidate cached capture snapshots
and schedule fresh frames. Locks and unsupported output transforms fail closed.

The style is rectangular. It follows current window/workspace geometry but
does not inherit native borders, rounding, local blur or FX mesh deformation.
Those pipelines would require separately proving that every texture and
history contains only sanitized pixels.

## Native integration

All settings live in Hyprland's typed Lua registry. `hl.config`,
`hl.get_config`, `getoption`, public Lua helpers and controller commands share
values. Atomic partial configuration rejects invalid patches before mutation.
Window actions pin both address and immutable identity; the compositor invokes
no shell for these actions.

The optional CLI manages an existing literal settings block and verifies
native acknowledgement. Without that block, it reports session-only changes.
Both the build probe and native module admit only the reviewed ABI. Standard
hyprpm loading needs no Python receipt or temporary session marker. Legacy
recovery tools retain exact ELF admission and protected handoff for reviewed
predecessors; migration to hyprpm is documented as a cold-login operation.

## Development priorities

Compatibility is narrow because the renderer uses internal hooks. Supporting
a new compositor build requires source/ABI review, boundary tests and isolated
pixel regressions. A successful load or attractive preview alone does not
establish privacy.

New capture routes, output transforms, decorations and rendering plugins need
their own evidence. Protection does not cover direct KMS/DRM capture, a hostile
same-user process or an application copying content into an unrelated public
window. See [REVIEW.md](REVIEW.md), [TESTING.md](TESTING.md) and
[VALIDATION.md](VALIDATION.md).
