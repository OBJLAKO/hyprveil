# Validation: 0.6.0 rendering update

These checks ran on 11 October 2026 in disposable, marked nested Hyprland
compositors with the physical seat disabled. The parent desktop hosted their
Wayland outputs; its windows were not captured and its configuration, plugin
manager and installed plugins were not changed. PNGs, shaders, private markers
and the mirror-buffer sentinel were generated test fixtures.

## Reviewed build and scope

The native source targets Hyprland 0.56.2, commit
`efb50993780079460b0cbed1363e2166a2de1d9f`, dependency ABI
`aq_0.15_hu_0.14_hg_0.5_hc_0.1_hlg_0.6`.
The final local plugin SHA-256 is
`e5d8e7ea6ec81ac42036b18081de6bcb9dca041de2cc5e3f57a7a1ab944d3a6a`.
This identifies the measured local build, not a portable binary download or
a guarantee for other compilers, drivers or compositor ABIs.

## Rendering results

| Runner | Result | Evidence |
| --- | --- | --- |
| `render_geometry_smoke.py` | 20/20 | Native rounded corners, cursor zoom 2×, rigid/detached 1.5× cameras, final-image region cropping, unrelated public fullscreen occlusion and fully transparent window admission. |
| `render_assets_smoke.py` | 31/31 | Chosen opaque color, legacy pure black, Stretch/Fill/Fit/Tile, PNG orientation, procedural GLSL, independent custom animation, zero-alpha shader output, compile/resource failure, one-shot retry latch, symlink replacement and custom PNG icon size/aspect/opacity. |
| `fx_smoke.py` | 35/35 | Actual OmaFX 0.4.0 private/public mesh transformers, native zoom 1×/2×, nonzero-origin region capture, unknown mixed-transformer fallback and spare mirrored-buffer exclusion. |

The FX suite compared 19 exported frames against the corresponding synthetic
local scene. All tested exports were opaque and contained no private-marker
RGB within tolerance 8. Minimum private contour intersection-over-union was
0.99745; minimum public contour intersection-over-union was 0.96388. The public
effect remained active, and expanded private edges were replaced.

The framebuffer regression first attached a new fixture-owned solid sentinel
texture to a spare native work buffer. With a live OmaFX private transformer,
the earlier candidate changed 450,080 sentinel pixels; the corrected candidate
changed zero. The check reads only that generated sentinel, not a native
monitor mirror. The renderer retains mirror-attached spare buffers so OmaFX
cannot select them as work output; it does not detach or rewrite their state.

The reviewed OmaFX source is version 0.4.0, revision
`1faa2b330126f3031007c0cac5efb30c11f2ed8f`. An existing installed binary was
copied into the owned test compositor; no additional plugin was installed.
The synthetic replacement alone is passed through the identified mesh
transformer. This is not universal third-party effect compatibility.

Original unsuccessful reports were retained. They include an incorrect initial
plugin-symbol lookup caught by the FX positive control, a custom-shader uniform
reflection requirement caught by the minimal shader, and test regions that
became invalid when the parent resized nested outputs. Geometry assertions
were corrected to sample the protected window's center, where the older
baseline also fails, rather than an unrelated bottom strip. One nested capture
timed out before any checks; its child compositor was stopped. These are not
counted as successful validation.

## Offline boundaries and panel

The core passed 225 Python tests. Seven standalone C++ programs passed normally
and with ASan/UBSan: session admission, PNG bounds/upload format, privacy
ancestry, styles, appearance, native configuration and custom asset resources.
Local LeakSanitizer was disabled because its ptrace mechanism is unavailable;
these checks do not instrument Hyprland, Cairo or the GPU driver. CI separately
requests ASan/UBSan/LSan.

The companion panel records its own unit, actual offscreen QML, process failure
and installer-transition results in its reviewer notes. Its new rendering
controls require explicit native capabilities; client defaults do not enable
them on 0.5.0. Built-in styles, icons, Black and legacy Image remain usable
with the older native module. New source pins and bundled-client hashes are
recorded in the panel's release/provenance files.

## Limits

Glasscope 0.7.0 was reviewed from source only and was not installed or loaded.
Its late screen postprocessing is not replayed in protected monitor/region
capture. Native cursor zoom is fixed independently; see
[compatibility](COMPATIBILITY.md) for the cooperative API requirement.

After Hyprveil unload, capture returns to Hyprland's native renderer. With an
active OmaFX deformation, the tested native fallback exposed 0.1429% of the
frame as private-marker pixels beyond the original black rectangle. Keep the
plugin loaded in Black/Omit mode during sharing; stop sharing before a native
update or unload. This pre-existing boundary is not fixed by this update.

The older portal, permission, transient-window, lock and stress results are
retained in [historical validation](VALIDATION.md); they were not all repeated
for this update. Real cold login, a live installation of 0.6.0, physical mirror
hardware, HDR, DMA-BUF portal transport, arbitrary screen effects and a long
GPU soak remain unverified. Trusted local GLSL can still be expensive enough
to stall the GPU. These bounded results do not establish zero defects or
universal privacy.
