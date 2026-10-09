# Validation scope

The 0.5.0 candidate was exercised on 9 October 2026 in disposable nested
Hyprland compositors with synthetic content. The parent desktop was not a
capture target and did not load the candidate. These are measured results,
not a claim of universal compatibility, zero defects or completed publication.

## Reviewed build

Hyprland 0.56.2, commit `efb50993780079460b0cbed1363e2166a2de1d9f`, with dependency
ABI `aq_0.15_hu_0.14_hg_0.5_hc_0.1_hlg_0.6` was used. The compositor ELF SHA-256
was `da8fcacf347bcbed83edc40108c6e2298da095e22246bd764e9bb382786cebb2`.
Both build probe and module enforce that reviewed ABI. Matching ABI alone does
not attest identical compositor ELF bytes or every compiler/driver combination.

The tested native plugin SHA-256 is
`89bad8c991c61b78f6eeb0cbc31d92f71af7a856ef0b6098a9d592cc21ac06cb`;
the legacy migration guard is
`34a496c802b51b30616cf5cac2f52e7eef10c30524c3d306cb106cbe15b25fe7`.
These identify local evidence, not portable prebuilt downloads.

## Current runtime results

| Runner | Completed result | Evidence scope |
| --- | --- | --- |
| `hyprpm_smoke.py` | 33/33 | Standard no-marker native admission, disabled live mirror diagnostics, real standalone CLI, automatic settings second pass, partial persistence, canonical aliases/uppercase tint and configurable icon. |
| `native_config_smoke.py` | 204/204 | Eleven typed settings across Lua/registry/IPC, old seven-field command compatibility, icon shape/size/opacity, atomic invalid patches, file reload, focused identity, sharing reset and detached callback teardown. |
| `design_gallery.py` | 724/724 | Ten actual GPU styles, geometric animation, speed-zero freeze/timer disarm, static Matte, opaque replacement, local visibility, black extrema and four icon choices. Fifty-four frames per style supply nine-second public motion galleries; the 404 bitmap also has 105 checked cells with no mismatch. |
| `style_bounds_smoke.py` | 100/100 | Four expressive styles at four small sizes and scales 1/1.25: 32 combinations, 40 opaque private exports, exact frozen repeats and unchanged local visibility. |
| `spoiler_smoke.py --customization --full-size-preview` | 92/92 | Stream damage, normal/fullscreen masks, public overlap, frozen coordinates, grain/tint/size extrema, fractional scaling and inherited surfaces. |
| `spoiler_smoke.py --force-shader-failure` | 6/6 | Real rejected shader compile, one-shot failure latch, opaque black fallback and safe teardown. |
| `popup_smoke.py --inherit-dialog --parent-lifecycle` | 71/71 | Actual GTK ownership and destruction, live-parent and orphan unload promotion, explicit reset and protected direct-window exports. |
| `x11_smoke.py` | 22/22 | Actual X11 transient ownership, pending snapshots, parent changes/destruction and retained privacy. |
| `permission_smoke.py` | Four successful cases | Allocated pending snapshots invalidated before the first approved capture for privacy, black, omit and image. |
| `lock_smoke.py` | 13/13 | Lock acknowledgment ordering, black locked exports and rotated output refusal. |
| `cursor_smoke.py` | 6/6 | Requested/cursor-free monitor and region behavior over the synthetic scene. |
| `fx_smoke.py` | 9/9 | Actual private/public FX transformers stay active while loaded modes exclude the private marker. Mask geometry does not inherit FX visually. |
| `upgrade_smoke.py --watcher ...` | 49/49 | Allowlisted old ELF, held capture gap, non-black/bad-pin refusal, inherited/orphan handoff, minimal atomic watcher and changed-only stream. |
| `smoke.py` | 23/23 | Local/capture distinction, omit/image/black, PNG fallback, foreground stacking and geometry. |
| `portal_smoke.py` | 43 frames, 11 controls | Chromium getDisplayMedia through private XDP/XDPH and PipeWire using SHM. All 37 protected frames had zero private-marker pixels. All 20 required fresh transitions passed, maximum observed transition 0.387 s. |
| `assets/demo/capture.py` | 3/3, 48 frames | Synthetic notes remain visible locally; actual Glass/404 exported frames have no private marker, with 24 distinct frames per style. |

Every successful run stopped its owned compositor/fixtures. Portal wrappers
also confirmed their own browser, D-Bus and media services stopped, with no
processes remaining for those exact runtimes. Generated reports and PNGs live
under ignored `artifacts/`; public media contain synthetic pixels and sanitized
provenance only.

The baseline inherited-dialog unload test reproduced a privacy disclosure:
2.74658203125% of the whole exported frame matched the private fixture color.
The current live-parent/orphan regression excludes it and confirms the promoted
native property survives unload until an explicit action.

Several old test expectations predated native configuration: they expected
omit at initial load or runtime mode to survive an empty-file reload. The
harness now verifies the intended initial/reloaded black state before selecting
omit. The watcher test now records genuine intermediate focus events before
requiring final convergence. The idle animation harness also assumed every
short step crossed a fixed color threshold. Actual damage timestamps and exact
pixel changes proved continuous motion; it now checks every adjacent frame
changes and cumulative perceptible movement. Privacy thresholds were retained.
Original failed reports were retained. An initial
FX fixture build also failed because the system pkg-config pointed to missing
headers; its successful repeat used a process-local pkg-config file pointing
to the reviewed installed headers. No system file was rewritten.

## Offline and resource checks

Core Python: 205 tests passed. Six standalone C++ programs passed normally and
under ASan/UBSan, covering native/session boundaries, PNG decode/upload, ancestry,
appearance, icons and strict configuration. Local ASan used `detect_leaks=0`
because this environment refuses LeakSanitizer's ptrace mechanism; this is not
a leak-freedom result. CI includes a separate ASan/UBSan/LSan job, whose remote
execution is not established by local checks.

A 60.087-second steady-state stress interval completed 172 overlapping pairs
(344 captures) with zero failed pairs. RSS stayed at 188744 KiB and anonymous
RSS at 83488 KiB from the ten-second observation onward; open descriptors
changed from 81 to 82 and then stayed stable. This bounded observation does
not establish long-term leak freedom or a GPU performance budget.

The companion panel has its own offline and offscreen QML evidence, including
SIGTERM-resistant processes, delimiter-free oversized output, missing executables,
all style/icon selectors and runtime-only settings. Its repository records
those results and native asset provenance separately.

## Earlier evidence

The previous 0.4 release had 172 Python tests, six C++ boundary programs and
path-specific native/portal regressions. A same-toolchain second checkout
produced byte-identical native outputs using source-path prefix maps. That
historical reproducibility result is not a new cross-compiler/distro guarantee.

## Remaining gaps and accepted behavior

Privileged hyprpm cache installation/update and a real cold login have not
been run on this host as part of this review. The standard API artifact,
configuration ordering and standalone client were tested independently in a
physical-seat-disabled lab. Follow the cold-login migration instructions.

Direct KMS/DRM recording bypasses this pipeline. Queued frames cannot be
retracted. Fullscreen omission can reveal compositor background rather than
public windows filtered by native fullscreen selection. Locked and unsupported
rotated/mirrored exports deliberately return black.

Physical mirrored hardware, HDR/color management, hotplug, portal DMA-BUF,
continuous X11 transition streams, long GPU soak and arbitrary third-party
renderers remain unverified. On unload, mapped inherited privacy is promoted,
but the native renderer resumes capture and has a measured FX edge limitation.
Stop sharing before updates/reloads. See [REVIEW.md](REVIEW.md) and
[TESTING.md](TESTING.md).
