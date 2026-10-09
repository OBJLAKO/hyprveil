# Security scope and reporting

Hyprveil protects the compositor-mediated capture scene for selected windows
on its explicitly supported Hyprland ABI. It does not protect direct KMS/DRM
recording, retract queued frames or isolate content from native plugins and
other processes with equivalent privileges. Native plugins execute inside the
compositor; a crash can terminate the desktop session.

Review [the capture design and limits](docs/REVIEW.md) and
[tested coverage](docs/VALIDATION.md) before enabling protection. Keep the
sanitized renderer loaded in black/omit when changing appearance. Before
unloading, the plugin revokes its temporary sharing choices and transfers
effective inherited privacy to native window properties. The native renderer
then owns capture masking, including its measured FX limitation described in
the review. Stop active sharing before a native update/reload. Unsupported ABI
and unrecognized legacy online-upgrade predecessors
are intentionally refused.

Normal installation uses hyprpm. Native admission checks the reviewed ABI
before accessing compositor internals; it no longer depends on a Python
receipt or a temporary live marker. Lab-only mirror diagnostics require the
isolated session guard. ABI matching does not attest identical ELF bytes,
compiler behavior or all third-party native modules. Compatibility evidence
is recorded against the actual tested compositor binary.

## Report a suspected disclosure

Use GitHub's private vulnerability report on this repository if available.
If it is unavailable, open a minimal issue requesting a private contact route,
without publishing an exploit or sensitive capture. Ordinary reproducible
non-security bugs may use normal issues.

Include the source revision, native binary hash, Hyprland version and ABI,
capture protocol/backend, protection ancestry, selected mode and a synthetic
reproduction. Distinguish monitor/region, direct-window, portal and KMS paths.
Describe whether the frame was already queued, a pending-consent snapshot or
freshly rendered after the mutation.

Never attach personal desktop screenshots, window titles, credentials, private
configuration backups or full environment dumps. Reproduce with the isolated
[test harness](docs/TESTING.md), whose fixtures deliberately paint distinguishable
colors. Sanitize host paths and process/session identifiers in generated reports.

The current compatibility target is narrow and version-pinned. A new Hyprland
build requires source/ABI and rendering review; it cannot be supported safely
by removing an admission check alone.
