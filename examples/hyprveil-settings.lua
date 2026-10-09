-- Hyprveil: standard Hyprland configuration. Edit the literal values below.
-- The Omarchy panel updates this block; custom Lua may follow it.
-- BEGIN HYPRVEIL SETTINGS v1
local hyprveil_settings = {
  mode = "spoiler",
  image_path = "",
  variant = "prism",
  color = "#a7c4d9",
  grain = 35,
  speed = 70,
  darkness = 50,
  eye = true,
  eye_size = 80,
  icon = "eye",
  icon_opacity = 75,
}
-- END HYPRVEIL SETTINGS v1

local _, missing = hl.get_config("plugin.hyprveil.mode")
if not missing then
  hl.config({ plugin = { hyprveil = hyprveil_settings } })
end
