# Capture security and lifecycle review

This document describes the reviewed design and its limits. It is not a claim
that every capture client, application, driver or native plugin is safe.
[VALIDATION.md](VALIDATION.md) separates current regression results from earlier
path-specific evidence; [TESTING.md](TESTING.md) explains reproduction.

## Threat model

Hyprveil changes pixels exported through Hyprland's compositor capture
pipeline. It does not hide local windows, retract already delivered/queued
frames or isolate application buffers from the compositor process. Direct
KMS/DRM recording, an application capturing itself and a hostile same-user
process are outside this boundary.

A native plugin runs with compositor privileges. Reviewed ABI checks and
controller socket attestation prevent accidental incompatible control;
they are not a security boundary against a compromised same-user process.
Other native plugins can draw arbitrary textures. Evidence for one FX build
cannot establish safety of unrelated render extensions.

## Reviewed safeguards

| Area | Failure addressed | Current mitigation |
| --- | --- | --- |
| Private surfaces | Native black masks missed deformed edges in the reviewed FX scene. | Rebuild a sanitized capture scene; omit protected surfaces or insert synthetic replacement at the normal stacking position. Never delegate a private replacement to its original draw path. |
| Cached frames | Consent-pending or static-stream snapshots can precede the current policy. | Track weak capture-session identity and policy generation; release outdated pending buffers before native copy; deny private/inherited direct-window exports before native cached-return paths. |
| Transient ownership | A child can have an own native flag of false while its parent is private. | Follow the actual parent chain with a 32-node bound; cycles/deeper chains fail closed. Wayland and X11 parent changes invalidate policy. |
| Parent destruction | Native parent links may disappear before a global close notification. | Subscribe to protocol-role destruction and close/unmap; preserve weak privacy retention for surviving descendants before native ownership disappears. |
| Hook arguments | Moving a shared native argument broke a still-mapped parent resource. | Forward parent/property arguments without destructive moves, following the exact installed caller ABI. |
| Background blur | Local blur and cached matte textures can contain private pixels. | Remove pre-blur and disable background blur in sanitized surface, rect, texture and transformed-window passes; discard cached blur/matte references. |
| Locks and outputs | Alternate capture rendering could acknowledge a lock surface or rely on unverified transform bounds. | Return opaque black without rendering/acknowledging lock surfaces; deny rotated and mirrored captures. |
| Auxiliary surfaces | Closing snapshots, drag icons and IME preedit lack reliable private-owner metadata. | Conservatively omit those capture passes; local rendering remains ordinary. |
| Shader portability | Default fragment integer precision truncated packed glyphs; signed-base `pow(x, 2)` has undefined results in GLSL ES. | Explicit high-precision integers, bounded bit shifts and multiplication for signed squares; ES 3 validation plus native small/fractional and 1080p renders. |
| Spoiler resources | Shader assertions, repeated failed allocation or teardown callbacks could terminate the compositor. | Bounded private GL compile/link with required attribute/uniform checks and RAII cleanup; one-shot failure latch to black; remove custom passes before resource/vtable unload with the GL context current; stop animation timers/listeners. |
| Native configuration | Generic plugin conversion lost strict type/range validators; partial failure could restore an unsafe mode. | Typed parser wrappers, atomic validated patches and a registered black fail-safe. Invalid Lua/file settings never authorize original private rendering. |
| Window actions | Delayed actions can target changed focus or a reused address. | Atomically verify focused mapped window, canonical address and immutable decimal ID. Refuse inherited sharing. Track at most 4096 weak temporary-share records with original native-property priority. |
| Share reset | Reset could overwrite later user choices or suppress future privacy rules on public windows. | Record only protected windows shared by this API; public false is a no-op. Any later explicit native setter relinquishes ownership. Restore prior value/absence only while the owned false override still applies. |
| Standard reload | Native fallback does not follow private ancestry after unload. | Before removing hooks, promote every mapped effectively private child/orphan to native `no_screen_share=true` at manual priority; revoke owned temporary shares first. Promotion persists until an explicit window action. |
| Legacy protected update | An old predecessor loses orphan retention and exposes a replacement gap. | Exact predecessor and target pins; freeze both candidate binaries before mutation; block capture commits through handoff; transfer weak identities in black mode; release only after successful adoption. Failure keeps the guard held. |
| Controller/install | Stale state, symlinks or concurrent edits could select another release or discard settings. | Bounded no-follow reads and IPC streams, exact process/socket identity, private atomic files, compare-and-set native configuration, literal-block persistence and rollback limited to files still matching this transaction. Legacy installation additionally attests compositor/release ELF. |
| Delayed legacy consent | Removing a trial marker after timeout could make late consent look like an ordinary hyprpm load. | An existing trial marker always takes the strict admission path. Cancellation leaves a validated tombstone until explicit rearming or runtime cleanup; normal hyprpm loads create no marker. |

### PNG boundary

The decoder reads one bounded regular-file snapshot without reopening its path
in Cairo. The essential-chunk sanitizer checks CRC and stream structure and
strips unused ancillary metadata before decode. Limits are 16 MiB encoded,
8192 pixels per axis, 16,777,216 pixels, 64 MiB decoded and 16,384 chunks.
The decoded-byte check accounts for Cairo's larger 16-bit representation.
Before GPU upload, floating-point/16-bit decoded surfaces are converted to
premultiplied ARGB32, matching the native texture uploader's byte format.
Invalid input produces black replacement. The controller preflight follows
the same bounds.

Synchronous regular-file reading, decoding and texture upload remain possible
frame-time stalls. These allocation limits do not provide a FUSE/disk deadline.
The standalone PNG executable was instrumented in earlier ASan/UBSan/LSan
checks; that does not instrument the compositor or system Cairo.

### Lifecycle and native ownership

Initialization verifies the exact ABI before compositor field access and
checks full ELF hook names against the compositor module. Partial failure
removes hooks, commands and subscriptions before unloading code. Callback
functions retained from the Lua API reject calls after plugin teardown.
Native weak references backed by unique ownership use short-lived `get()` on
the event loop rather than invalid shared-pointer promotion. No worker thread
accesses compositor objects.

Inherited/orphan protection belongs to the loaded plugin and map lifetime.
A late load cannot infer a parent that already disappeared; deliberate
unparenting can end ordinary inheritance. A successful explicit native false
setter releases orphan retention, while a still-private ancestor remains
protective. The legacy protected update preserves retained weak identities.
Ordinary unload promotes mapped effective privacy to native properties before
removing plugin inheritance. Future children created while the plugin is
absent have only Hyprland's native policy. Unload also returns capture to the
native renderer, including its separately measured FX limitation.

## Remaining limits

- Already queued or encoded frames cannot be retracted. Fresh-frame tests wait
  for a new sanitized serial; they do not prove internal process-memory secrecy.
- Omission of a private fullscreen window does not reconstruct public windows
  already filtered out by native fullscreen selection; background is accepted.
- Masks follow current geometry but skip ordinary borders, rounding, local
  blur and FX grouping. FX-active privacy tests do not prove visual inheritance.
- Native unload exposed deformed edges in the reviewed FX scene. Selecting
  `black` or `omit` keeps the sanitized renderer; unload is not a privacy mode.
- Mirrored physical output hardware, HDR/color-management combinations,
  output hotplug, long GPU-allocation soaks, continuous X11 transitions and
  portal DMA-BUF transport are not established by the current results.
- Public native direct-window rendering retains a feedback-suppression timing
  limitation. Protected/inherited/locked direct-window captures return black
  before that path; no private disclosure was established through this residual.
- Transaction rollback does not span sudden power loss or defeat a hostile
  same-user process. Cold-login automation needs separate validation.
- Pixel-color assertions prove absence of the distinguishing synthetic colors
  in the tested scenes. They do not prove absence of every possible disclosure
  or resource leak across applications, drivers and third-party plugins.

## Primary references

The reviewed compositor source is pinned to
`efb50993780079460b0cbed1363e2166a2de1d9f`:

- [Capture frame and pending snapshots](https://github.com/hyprwm/Hyprland/blob/efb50993780079460b0cbed1363e2166a2de1d9f/src/managers/screenshare/ScreenshareFrame.cpp)
- [Window and capture rendering](https://github.com/hyprwm/Hyprland/blob/efb50993780079460b0cbed1363e2166a2de1d9f/src/render/Renderer.cpp)
- [Session lock ordering](https://github.com/hyprwm/Hyprland/blob/efb50993780079460b0cbed1363e2166a2de1d9f/src/managers/SessionLockManager.cpp)
- [Permission and window-rule bindings](https://github.com/hyprwm/Hyprland/blob/efb50993780079460b0cbed1363e2166a2de1d9f/src/config/lua/bindings/LuaBindingsConfigRules.cpp)
- [Plugin teardown](https://github.com/hyprwm/Hyprland/blob/efb50993780079460b0cbed1363e2166a2de1d9f/src/plugins/PluginSystem.cpp)
- [Native hook implementation](https://github.com/hyprwm/Hyprland/blob/efb50993780079460b0cbed1363e2166a2de1d9f/src/plugins/HookSystem.cpp)
- [Stable Lua window identity](https://github.com/hyprwm/Hyprland/blob/efb50993780079460b0cbed1363e2166a2de1d9f/src/config/lua/objects/LuaWindow.cpp)

[Itanium C++ argument conventions](https://itanium-cxx-abi.github.io/cxx-abi/abi.html#non-trivial-parameters)
explain nontrivial forwarding, but the ownership finding also depended on the
exact installed caller. [libpng input-allocation guidance](https://libpng.sourceforge.io/decompression_bombs.html)
explains auxiliary decompression risks. Source review complements actual pixel
regressions; it does not replace them.
