-- Add dofile(os.getenv("HOME") .. "/.config/hypr/hyprveil-hyprpm.lua")
-- to your Hyprland Lua configuration. Choose unused bindings below.
hl.permission("^/usr/(bin|local/bin)/hyprpm$", "plugin", "allow")

dofile(os.getenv("HOME") .. "/.config/hypr/hyprveil-settings.lua")

hl.on("hyprland.start", function()
  hl.exec_cmd("hyprpm reload")
end)

hl.bind("SUPER + ALT + H", function()
  local p = hl.plugin.hyprveil
  if p and p.toggle then p.toggle() end
end, { description = "Hide/show focused window in screen sharing" })

hl.bind("SUPER + ALT + SHIFT + H", function()
  local p = hl.plugin.hyprveil
  if p and p.reset_sharing then p.reset_sharing() end
end, { description = "Revoke temporary screen sharing" })
