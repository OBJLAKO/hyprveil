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
inline constexpr std::array<const char*, 11> FIELDS{
    "mode", "image_path", "variant", "color", "grain", "speed", "darkness", "eye", "eye_size", "icon", "icon_opacity"};

inline bool pathGrammar(std::string_view path) {
    return path.size() <= 4096 && (path.empty() || path.front() == '/') &&
        std::all_of(path.begin(), path.end(), [](unsigned char c) { return c >= 0x20 && c != 0x7f; });
}

struct Settings {
    std::string mode = "black";
    std::string imagePath;
    Appearance appearance;
    bool operator==(const Settings&) const = default;

    std::expected<void, std::string> set(std::string_view key, const Value& value) {
        const auto* text = std::get_if<std::string>(&value);
        const auto* number = std::get_if<std::int64_t>(&value);
        const auto* boolean = std::get_if<bool>(&value);
        if (key == "mode") {
            if (!text || (*text != "black" && *text != "omit" && *text != "spoiler" && *text != "image"))
                return std::unexpected("mode must be black, omit, spoiler or image");
            mode = *text;
        } else if (key == "image_path") {
            if (!text || !pathGrammar(*text))
                return std::unexpected("image_path must be empty or a bounded absolute path without control characters");
            imagePath = *text;
        } else if (key == "variant") {
            if (!text || !Appearance::validVariant(*text))
                return std::unexpected("variant must be prism, signal, aurora, contour, radar, matte, error404, matrix, anonymous or glass");
            appearance.variant = Appearance::canonicalVariant(*text);
        } else if (key == "icon") {
            if (!text || !Appearance::validIcon(*text))
                return std::unexpected("icon must be eye, lock, shield or none");
            appearance.icon = *text;
        } else if (key == "color") {
            if (!text || text->size() != 7 || text->front() != '#')
                return std::unexpected("color must be #RRGGBB");
            auto canonical = *text;
            for (std::size_t i = 1; i < canonical.size(); ++i) {
                auto& c = canonical[i];
                if (c >= 'A' && c <= 'F') c += 'a' - 'A';
                if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
                    return std::unexpected("color must be #RRGGBB");
            }
            appearance.color = std::move(canonical);
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
        return candidate;
    }

    Patch values() const {
        return {{"mode", mode}, {"image_path", imagePath}, {"variant", appearance.variant}, {"color", appearance.color},
            {"grain", std::int64_t{appearance.grain}}, {"speed", std::int64_t{appearance.speed}},
            {"darkness", std::int64_t{appearance.darkness}}, {"eye", appearance.eye}, {"eye_size", std::int64_t{appearance.eyeSize}},
            {"icon", appearance.icon}, {"icon_opacity", std::int64_t{appearance.iconOpacity}}};
    }
};
} // namespace Hyprveil::NativeConfig
