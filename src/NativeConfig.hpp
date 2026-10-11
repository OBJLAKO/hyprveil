#pragma once

#include "Appearance.hpp"
#include <algorithm>
#include <cstdint>
#include <array>
#include <expected>
#include <filesystem>
#include <map>
#include <variant>
#include <sys/stat.h>

namespace Hyprveil::NativeConfig {
using Value = std::variant<std::string, std::int64_t, bool>;
using Patch = std::map<std::string, Value>;
inline constexpr std::array<const char*, 15> FIELDS{
    "mode", "image_path", "variant", "color", "grain", "speed", "darkness", "eye", "eye_size", "icon", "icon_opacity",
    "mask_color", "image_fit", "shader_path", "icon_path"};

inline bool pathGrammar(std::string_view path) {
    return path.size() <= 4096 && (path.empty() || path.front() == '/') &&
        std::all_of(path.begin(), path.end(), [](unsigned char c) { return c >= 0x20 && c != 0x7f; });
}

inline bool regularAsset(std::string_view path, std::uintmax_t limit) {
    if (path.empty() || !pathGrammar(path)) return false;
    const std::filesystem::path target{path};
    // Reject links in every component, including a linked parent directory.
    // Render-time readers must independently open and validate their inode.
    std::filesystem::path prefix;
    for (const auto& component : target) {
        prefix /= component;
        struct stat info {};
        if (lstat(prefix.c_str(), &info) != 0 || S_ISLNK(info.st_mode)) return false;
    }
    struct stat info {};
    return lstat(target.c_str(), &info) == 0 && S_ISREG(info.st_mode) &&
        info.st_size > 0 && static_cast<std::uintmax_t>(info.st_size) <= limit;
}

inline bool fragmentPath(std::string_view path) {
    if (path.size() < 5) return false;
    auto suffix = std::string{path.substr(path.size() - 5)};
    for (auto& c : suffix) if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    return suffix == ".frag";
}

struct Settings {
    std::string mode = "black";
    std::string imagePath;
    std::string maskColor = "#000000";
    std::string imageFit = "stretch";
    std::string shaderPath;
    std::string iconPath;
    Appearance appearance;
    bool operator==(const Settings&) const = default;

    std::expected<void, std::string> set(std::string_view key, const Value& value) {
        const auto* text = std::get_if<std::string>(&value);
        const auto* number = std::get_if<std::int64_t>(&value);
        const auto* boolean = std::get_if<bool>(&value);
        if (key == "mode") {
            if (!text || (*text != "black" && *text != "color" && *text != "omit" && *text != "spoiler" && *text != "image"))
                return std::unexpected("mode must be black, color, omit, spoiler or image");
            mode = *text;
        } else if (key == "image_path") {
            if (!text || !pathGrammar(*text))
                return std::unexpected("image_path must be empty or a bounded absolute path without control characters");
            imagePath = *text;
        } else if (key == "shader_path" || key == "icon_path") {
            if (!text || !pathGrammar(*text) || (key == "shader_path" && !text->empty() && !fragmentPath(*text)))
                return std::unexpected(std::string{key} + " must be empty or a bounded absolute path" +
                    (key == "shader_path" ? " ending in .frag" : "") + " without control characters");
            (key == "shader_path" ? shaderPath : iconPath) = *text;
        } else if (key == "image_fit") {
            if (!text || (*text != "fill" && *text != "fit" && *text != "tile" && *text != "stretch"))
                return std::unexpected("image_fit must be fill, fit, tile or stretch");
            imageFit = *text;
        } else if (key == "variant") {
            if (!text || !Appearance::validVariant(*text))
                return std::unexpected("variant must be prism, signal, aurora, contour, radar, matte, error404, matrix, anonymous or glass");
            appearance.variant = Appearance::canonicalVariant(*text);
        } else if (key == "icon") {
            if (!text || !Appearance::validIcon(*text))
                return std::unexpected("icon must be eye, lock, shield, none or custom");
            appearance.icon = *text;
        } else if (key == "color" || key == "mask_color") {
            if (!text || text->size() != 7 || text->front() != '#')
                return std::unexpected(std::string{key} + " must be #RRGGBB");
            auto canonical = *text;
            for (std::size_t i = 1; i < canonical.size(); ++i) {
                auto& c = canonical[i];
                if (c >= 'A' && c <= 'F') c += 'a' - 'A';
                if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
                    return std::unexpected(std::string{key} + " must be #RRGGBB");
            }
            (key == "color" ? appearance.color : maskColor) = std::move(canonical);
        } else if (key == "eye") {
            if (!boolean) return std::unexpected("eye must be a boolean");
            appearance.eye = *boolean;
        } else if (key == "grain" || key == "speed" || key == "darkness" || key == "eye_size" || key == "icon_opacity") {
            const auto minimum = key == "eye_size" ? 40 : 0;
            const auto maximum = key == "eye_size" ? 128 : key == "speed" ? 200 : 100;
            if (!number || *number < minimum || *number > maximum)
                return std::unexpected(std::string{key} + " must be an integer in " + std::to_string(minimum) + ".." + std::to_string(maximum));
            auto* target = key == "grain" ? &appearance.grain : key == "speed" ? &appearance.speed :
                key == "darkness" ? &appearance.darkness : key == "icon_opacity" ? &appearance.iconOpacity : &appearance.eyeSize;
            *target = static_cast<int>(*number);
        } else return std::unexpected("unknown Hyprveil setting");
        return {};
    }

    std::expected<Settings, std::string> patched(const Patch& patch) const {
        if (patch.size() > FIELDS.size()) return std::unexpected("too many Hyprveil settings");
        Settings candidate = *this;
        for (const auto& [key, value] : patch) {
            const auto result = candidate.set(key, value);
            if (!result) return std::unexpected(result.error());
        }
        if (candidate.mode == "image") {
            struct stat info {};
            if (candidate.imagePath.empty() || lstat(candidate.imagePath.c_str(), &info) != 0 || !S_ISREG(info.st_mode))
                return std::unexpected("image mode requires an existing absolute image_path");
        }
        if (patch.contains("shader_path") && !candidate.shaderPath.empty() && !regularAsset(candidate.shaderPath, 64 * 1024))
            return std::unexpected("shader_path requires a regular .frag file no larger than 64 KiB without symlinks");
        if (patch.contains("icon_path") && !candidate.iconPath.empty() && !regularAsset(candidate.iconPath, 16 * 1024 * 1024))
            return std::unexpected("icon_path requires a regular PNG no larger than 16 MiB without symlinks");
        if ((patch.contains("icon") || patch.contains("icon_path")) && candidate.appearance.icon == "custom" && candidate.iconPath.empty())
            return std::unexpected("custom icon requires icon_path");
        if (patch.contains("icon") && candidate.appearance.icon == "custom" && !regularAsset(candidate.iconPath, 16 * 1024 * 1024))
            return std::unexpected("custom icon requires an existing regular icon_path without symlinks");
        return candidate;
    }

    Patch values() const {
        return {{"mode", mode}, {"image_path", imagePath}, {"variant", appearance.variant}, {"color", appearance.color},
            {"grain", std::int64_t{appearance.grain}}, {"speed", std::int64_t{appearance.speed}},
            {"darkness", std::int64_t{appearance.darkness}}, {"eye", appearance.eye}, {"eye_size", std::int64_t{appearance.eyeSize}},
            {"icon", appearance.icon}, {"icon_opacity", std::int64_t{appearance.iconOpacity}},
            {"mask_color", maskColor}, {"image_fit", imageFit}, {"shader_path", shaderPath}, {"icon_path", iconPath}};
    }
};
} // namespace Hyprveil::NativeConfig
