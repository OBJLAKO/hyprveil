#include "NativeConfig.hpp"
#include <cassert>
#include <fstream>
#include <iostream>
#include <unistd.h>

using namespace Hyprveil::NativeConfig;
int main() {
    const Settings defaults;
    assert(defaults.mode == "black" && defaults.imagePath.empty() && defaults.values().size() == 11);
    assert(defaults.appearance.icon == "eye" && defaults.appearance.iconOpacity == 75);
    for (const auto shape : {"eye", "lock", "shield", "none"}) {
        const auto icon = defaults.patched({{"icon", std::string{shape}}, {"icon_opacity", std::int64_t{0}}, {"eye", false}});
        assert(icon && icon->appearance.icon == shape && icon->appearance.iconOpacity == 0 && !icon->appearance.eye);
    }
    assert(!defaults.patched({{"icon", std::string{"unknown"}}}));
    assert(!defaults.patched({{"icon_opacity", true}}));
    assert(!defaults.patched({{"icon_opacity", std::int64_t{101}}}));
    for (const auto alias : {"satin", "telegram", "grid", "404", "cmatrix", "anon", "liquid-glass", "liquidglass"})
        assert(defaults.patched({{"variant", std::string{alias}}})->appearance.variant == Hyprveil::Appearance::canonicalVariant(alias));
    const auto configured = defaults.patched({{"mode", std::string{"spoiler"}}, {"color", std::string{"#AaBbCc"}},
        {"grain", std::int64_t{100}}, {"eye", false}});
    assert(configured && configured->appearance.color == "#aabbcc" && configured->appearance.grain == 100 &&
        !configured->appearance.eye && configured->appearance.speed == defaults.appearance.speed);
    const auto before = *configured;
    for (const auto& patch : std::array<Patch, 12>{{
        {{"mode", std::string{"off"}}}, {{"color", std::string{"#ggffff"}}}, {{"grain", true}},
        {{"grain", std::string{"20"}}}, {{"grain", std::int64_t{-1}}}, {{"speed", std::int64_t{201}}},
        {{"eye_size", std::int64_t{39}}}, {{"eye_size", std::int64_t{129}}}, {{"eye", std::int64_t{1}}},
        {{"unknown", true}}, {{"mode", std::string{"image"}}, {"image_path", std::string{}}},
        {{"grain", std::int64_t{0}}, {"eye", std::string{"false"}}}
    }}) {
        assert(!before.patched(patch));
        assert(before == *configured); // Atomic rejection preserves the source.
    }
    for (const auto& path : {std::string{"relative.png"}, std::string{"/tmp/bad\nname"},
         std::string{"/tmp/a\0b", 8}, std::string{"/"} + std::string(4096, 'a')})
        assert(!defaults.patched({{"image_path", path}}));
    const auto extrema = defaults.patched({{"grain", std::int64_t{0}}, {"speed", std::int64_t{200}},
        {"darkness", std::int64_t{100}}, {"eye_size", std::int64_t{128}}, {"variant", std::string{"telegram"}}});
    assert(extrema && !extrema->appearance.animated());
    for (const auto name : Hyprveil::Appearance::VARIANTS) {
        const auto material = defaults.patched({{"variant", std::string{name}}});
        assert(material && material->appearance.variant == name);
        assert(material->appearance.animated() == (name != "matte"));
    }
    assert(!defaults.patched({{"variant", std::string{"Aurora"}}}));
    assert(!defaults.patched({{"variant", std::string{"contour extra"}}}));

    char runtime[] = "/tmp/hyprveil-native-model-XXXXXX";
    assert(mkdtemp(runtime));
    const auto file = std::filesystem::path{runtime} / "image.png";
    std::ofstream(file).put('x');
    const auto link = std::filesystem::path{runtime} / "link.png";
    std::filesystem::create_symlink(file, link);
    assert(defaults.patched({{"image_path", file.string()}})->mode == "black");
    assert(defaults.patched({{"mode", std::string{"image"}}, {"image_path", file.string()}}));
    assert(!defaults.patched({{"mode", std::string{"image"}}, {"image_path", link.string()}}));
    assert(!defaults.patched({{"mode", std::string{"image"}}, {"image_path", std::string{runtime}}}));
    std::filesystem::remove_all(runtime);
    std::cout << "native configuration: strict schema, canonical values, atomic rejection and safe path admission passed\n";
}
