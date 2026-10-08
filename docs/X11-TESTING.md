# Guarded X11 transient regression

`tools/x11_smoke.py` creates a new synthetic lab through the existing
`PermissionSmoke`/`Stress` launcher. It tests a child whose own native
`no_screen_share` is false, a separate protected parent, and a public parent.
The child initially has no `WM_TRANSIENT_FOR`; the first capture is held at
real native consent while the fixture attaches it to the protected parent.

The parent supplied to the launcher can be the actual desktop Wayland socket.
It is a **host canvas, not an isolated parent**. The launcher connects once,
passes that connection as `WAYLAND_SOCKET` to a fresh nested Hyprland, and sets
`LIBSEAT_BACKEND=hyprveil-disabled`. A temporary nested surface is the intended
visible desktop effect; the test also consumes GPU/CPU resources. It does not
change the host compositor configuration or load a plugin into it.

After startup, every compositor command and capture uses `load_lab` to verify
the new private runtime, marked compositor PID and disabled physical seats.
The nested Wayland output is removed and `HV-TEST` is the synthetic headless
capture target. The launcher uses a new lab HOME/config/cache/data tree.
Desktop display, instance, session-bus and service-manager variables are
removed; basic PATH/locale variables remain, including the private consent
wrapper directory used only by this test.

The XCB fixture never calls `xcb_connect` or resolves `DISPLAY`. Its supervisor
skips global X11 locks unless their owning PID is the marked lab or its
Xwayland descendant, then verifies the Unix socket's `SO_PEERCRED`. The fixture
verifies the inherited connected FD again before loading libxcb or sending
an X11 handshake. `DISPLAY` must be unset. No X11 authentication or configuration
is read from the user's desktop HOME.

The generated initial lab config enables Xwayland, registers synthetic rules,
asks for the first `grim` capture and permits the copied lab plugin. These
rules are separate from the user's configuration. The gated dialog wrapper
checks that its runtime is the one allowed marked lab before responding.
After the pending-snapshot assertion, the runner disables permission
enforcement only inside that disposable compositor for its fresh-frame checks.

`tests/test_x11_guard.py` exercises inherited-DISPLAY refusal before FD access,
peer PID/UID mismatch, skipping the main X11 lock without a connection, closing
a mismatching socket before its X11 handshake, and fixture refusal before
libxcb loading. These are offline mocks, not rendering evidence.

The native source confirms that deleting `WM_TRANSIENT_FOR` in Hyprland 0.56.2
retains the cached parent. The regression checks that actual behavior and uses
a new public parent to test release of effective inherited privacy. The exact
`CXWM::readProp` hook observes before/after effective privacy for this property;
an actual change increments the existing policy generation and damages the
capture scene. The test must separately prove an allocated pending snapshot,
first accepted PNG redaction, snapshot invalidation, local child visibility,
and parent/public reparent behavior.

Fresh-frame/pending-consent evidence does not establish a persistent browser
`copy_with_damage` stream for the X11 transitions. The runner records
`static_stream_tested=false`. The prepared lifetime cases unmap the protected
parent, destroy it with the independently mapped child surviving, and verify
retained child protection before an explicit `no_screen_share=false` reset.
It then creates a fresh parent XID, reattaches the same public/reset child,
and destroys that still mapped parent without a prior unmap request. This
separate case requires a new policy generation and retained child redaction;
the previous unmap's latch cannot satisfy its initial public-child state.
Each capture saves its local mirror, child ROI and policy generation. These
cases count as tested only when the matching artifact report passes.

An earlier reviewed release completed all 22 assertions, including the first
approved pending-consent PNG, parent unmap, destroy after unmap, a fresh
parent's direct mapped destroy and both explicit orphan resets. The child kept
its own native flag false, unchanged geometry and visible local magenta pixels.
Teardown recorded no cleanup errors. This is path-specific historical evidence;
it is not a claim of a complete X11 rerun on every later API/metadata build.
See [VALIDATION.md](VALIDATION.md) for the current validation boundary.

Failed reports remain failed. Their first accepted PNGs must not be replaced
by later local-readback trigger captures. Actual rendering evidence belongs to
the runner's generated report, rather than this description of its assertions.

The runner owns its new compositor, XCB/Wayland fixtures and permission helper.
Its `finally` cleanup stops those processes and records `lab_stopped` and
`cleanup_errors`. It leaves artifact PNGs and private runtime files for
debugging. A successful assertion report alone is insufficient without
confirmed lab teardown.
