# Validation scope

The 0.4.0 configuration and window API were exercised in disposable nested
Hyprland compositors with synthetic content. No personal desktop capture is
needed for these suites. This document records measured coverage, not a claim
of universal compatibility or a particular reader's installed state.

## Reviewed baseline

The reviewed compositor is Hyprland 0.56.2, commit
`efb50993780079460b0cbed1363e2166a2de1d9f`. Its exact supported ABI is listed
in [HOST-SETUP.md](HOST-SETUP.md). Native admission checks running/installed ELF
identity as well as the header ABI; the name 0.56.2 alone does not establish
compatibility.

The publication build has native SHA-256
`2b84936ad299660b1e3b0d677db8453a4e3c55c8fee44f5a454a596af247e8be`
and migration guard SHA-256
`67c54742ffdc6117cdc58aea726b24ab263df2c2f108b8dcc396a9f10fb07379`.
These are evidence pins, not downloadable prebuilt releases or an instruction
to bypass admission. Always attest the binary built from the checkout being
installed; preview releases contain source, not a universal compositor binary.

## Latest targeted runtime results

| Runner | Completed result | Evidence scope |
| --- | --- | --- |
| `native_config_smoke.py` | 126/126 | Native Lua registry/getoption agreement, commands, mode/cycle helpers, file reloads, 30 atomic invalid patches, malformed-file black fallback, actual captures, focused-window identity, sharing/reset ownership and detached callbacks after unload. |
| `upgrade_smoke.py` | 47/47 | Exact reviewed 0.4 predecessor to the baseline; held capture gap, bad-pin and non-black handoff refusal, inherited/orphan transfer, unchanged native flags, selected state restoration, independent GUI watcher identity and changed-only output. |
| `spoiler_smoke.py --customization` | 92/92 | Normal/fullscreen synthetic captures, appearance extrema, frozen animation, static damage streams, local visibility, public overlap and opaque private replacement. |
| `spoiler_smoke.py --force-shader-failure` | 6/6 | Real rejected shader compile, one-shot attempt latch, opaque fallback and safe teardown. |
| `fx_smoke.py` | 9/9 | Actual private/public FX transformers remain active; private fixture pixels are absent in loaded modes. This does not prove that masks inherit FX visually. |
| `lock_smoke.py` | 13/13 | Lock ordering, black locked exports and denied rotated output. |

The configuration and migration suites passed on these exact publication
pins, including the optional independent GUI watcher. The
four renderer suites ran on its earlier 0.4 build
`0426199c06472e4ae262972ea9d13d9fcea11229af55f7274057ce5e370e4196`.
The subsequent reproducibility build
`6b4e8ce6a24f56678c96a118296e530c777dd249123f038bf8844a9d78678641`
changed debug paths; `.text` and `.rodata` were verified byte-identical to that
earlier ELF. Publication then changed only the native plugin and guard author
metadata to OBJLAKO. Render source and policy remained unchanged. The four
renderer results are retained behavioral evidence, not represented as a full
shader/FX rerun on the publication ELF.

Each listed lab stopped its owned processes and recorded no cleanup errors.
Full reports and synthetic PNGs are generated under ignored `artifacts/` by the
runners; they are local development evidence, not repository download links.
A run must have `ok: true`, every required check and successful teardown before
it can be counted as completed.

## Unit checks and reproducibility

All 172 Python unit tests passed. The suite covers controller identity,
atomic persistence, concurrent edits, rollback, PNG verification,
lab/report guards and install/upgrade
selection. Six standalone C++ programs check session admission, PNG boundaries,
privacy ancestry, spoiler math, appearance and native configuration validation.
[TESTING.md](TESTING.md) contains the commands; CI runs these offline checks,
not a compositor or a portal session.

A temporary second checkout, including a path containing spaces, produced
byte-identical native plugin, guard and ABI-probe outputs with the same compiler,
headers and dependencies. `-ffile-prefix-map` and `-fdebug-prefix-map` remove
checkout-path differences. This is a same-toolchain reproducibility result,
not a cross-compiler or cross-distribution reproducible-build guarantee.

## Earlier path-specific evidence

Before the 0.4 API changes, dedicated synthetic runs covered ordinary monitor
and region capture; real GTK parent/dialog role destruction; X11
`WM_TRANSIENT_FOR`, consent-pending snapshot invalidation and orphan lifecycle;
cursor overlays; continuous Chromium `getDisplayMedia` through private portal
services and PipeWire using SHM; and bounded movement/reload/capture stress.
The standalone PNG parser also completed ASan/UBSan/LSan instrumentation.

Those earlier results informed the current guards and reproduction tools.
They are not represented as a full rerun of every integration path on the
latest metadata-only build. A browser portal test is not an external meeting
or a KMS recorder test; overlapping consumers are not proof of simultaneous
protocol copies; bounded RSS/FD measurements do not establish absence of slow
or GPU leaks.

## Unverified combinations and accepted behavior

Direct KMS/DRM recording bypasses the compositor pipeline. Queued frames cannot
be retracted. Protected fullscreen omission may reveal background instead of
public windows filtered out by Hyprland. Rotated/mirrored output capture and
locked capture are deliberately black.

Physical mirrored hardware, HDR/color-management combinations, output hotplug,
portal DMA-BUF transport, a continuous X11 transition stream, long GPU soak,
cold-login recovery and arbitrary third-party renderers remain outside the
established results. Ordinary borders, rounding, blur and FX deformation are
not inherited by the synthetic replacement. Native unload restores underlying
masking and its measured FX limitation. See [REVIEW.md](REVIEW.md).
