# Rendering compatibility

The supported compositor is Hyprland 0.56.2, commit
`efb50993780079460b0cbed1363e2166a2de1d9f`, with the dependency ABI enforced by
the native module. The replacement scene is rebuilt for compositor capture;
the local desktop continues through Hyprland's ordinary renderer.

## Native window and camera behavior

Masks use the window's current position, size, rounding and rounding power.
True fullscreen windows have square corners. Hidden and fully faded windows
are excluded before replacement, so their masks cannot cover a public
fullscreen window.

Native cursor zoom applies to the complete sanitized monitor scene before
region cropping. Rigid and detached zoom use the compositor's own camera
transform. The renderer never copies an unsanitized monitor mirror to recover
zoom. Native borders, background blur and closing-window snapshots remain
outside the replacement appearance.

## OmaFX window deformation

The integration targets the reviewed `omarchy-fx` 0.4.0 mesh transformer,
[source revision `1faa2b3`](https://github.com/stoneynutcase/omarchy-fx/tree/1faa2b330126f3031007c0cac5efb30c11f2ed8f).
It passes a freshly cleared framebuffer containing only the synthetic mask
and icon to that transformer. The ordinary private surface, blur caches,
motion history and window render events are excluded from this input.

The integration requires the exact mesh-transformer vtable in the loaded
OmaFX module and the reviewed plugin version. Unknown transformers, mixed
transformer chains and unavailable resources keep the ordinary synthetic
mask. This identity check limits accidental compatibility; it does not attest
the bytes of an arbitrary native plugin that advertises the same version.
Native plugins already execute with full compositor privileges.

Public windows keep their ordinary native rendering path. This integration
does not install OmaFX or any other plugin. It does not establish compatibility
with every window transformer or screen postprocessor.

## Glasscope and screen postprocessing

Glasscope 0.7.0 was inspected at
[revision `40690d8`](https://github.com/Horizon0427/Glasscope/tree/40690d820af69591f020eb814b822945afa37e42).
Its effect runs through `RENDER_LAST_MOMENT`. Hyprveil does not replay arbitrary
listeners at that stage while rebuilding a protected capture: a listener can
read compositor textures and caches outside the sanitized framebuffer.

Glasscope's effect in a protected monitor/region recording is therefore not
currently supported. This source review does not establish that Glasscope
stops working on the local desktop. It was not installed or loaded for this
review. A cooperative API that explicitly accepts a sanitized input and keeps
its capture resources separate is needed before this path can be supported.

The native zoom fix is independent of this postprocessing limitation. New
screen effects require a source and capture review; emitting a generic render
event alone does not establish safe compatibility.
