# Notification privacy

Notification protection uses an independent profile. Private windows can keep
404 while notification cards use a color, image, built-in material, or trusted
local GLSL. Protection affects captures; local notifications remain usable.
It is disabled by default.

```sh
hyprveil notifications --enabled on --mode omit
hyprveil notifications --enabled on --mode spoiler --variant matrix --icon none
hyprveil notifications --mode color --mask-color '#000000'
hyprveil notifications --mode image --image-path /absolute/background.png --image-fit fill
hyprveil notifications --mode spoiler --shader-path /absolute/local.frag
```

The same fields are available through native Lua. Prefix the existing rendering
fields with `notification_`; additionally set `notification_enabled`,
`notification_namespace` and `notification_rounding` (-1 inherits Hyprland,
otherwise 0–64). Updates are partial and preserve the window profile.

```lua
hl.plugin.hyprveil.configure({
  notification_enabled = true,
  notification_mode = "spoiler",
  notification_variant = "matrix",
  notification_eye = false,
})
```

The default exact layer-shell namespace is `omarchy-notifications`. Other
notification daemons require their own exact namespace and a bounded input
region. This matches a layer, not individual application names. History
panels with a different namespace are outside this profile.

The complete source layer, its subsurfaces and popups are excluded from the
protected scene. Replacement envelopes come from committed input rectangles;
source pixels outside those rectangles are excluded too. Empty, infinite,
non-finite, overly large, or more than 32 input rectangles cause omission,
never a full-monitor fill. An invalid image or shader uses the profile's opaque
`mask_color`. Capture status exposes `notifications.geometry_status` and asset
statuses for diagnosis.

Card geometry depends on the notification daemon's input mask. Omarchy's
current built-in manager exposes the toast column, so its narrow inter-card
gaps are included in the replacement. A manager exposing individual card
regions gets individual replacements and untouched gaps. No new notification
daemon is required, and the plugin never reads notification text or history.

Image sizing supports fill, fit, tile and stretch. Shader/icon assets use the
same bounded admission as window assets. Shaders are trusted local programs:
arbitrarily expensive GLSL can stall the GPU. Stop sharing before unloading or
upgrading the native module; protection requires the module to remain loaded.
