#pragma once
#include "NativeConfig.hpp"
#include <cmath>
#include <vector>

namespace Hyprveil::Notifications {
inline constexpr std::string_view PREFIX = "notification_";
inline constexpr std::array<const char*, 3> EXTRA_FIELDS{"enabled", "namespace", "rounding"};
inline constexpr std::size_t MAX_REGIONS = 32;

struct Settings {
    bool enabled = false;
    std::string nameSpace = "omarchy-notifications";
    int rounding = -1;
    NativeConfig::Settings profile{.mode="omit"};
    bool operator==(const Settings&) const = default;

    std::expected<void, std::string> set(std::string_view key, const NativeConfig::Value& value) {
        if (key == "enabled") {
            const auto* boolean = std::get_if<bool>(&value);
            if (!boolean) return std::unexpected("notification_enabled must be boolean");
            enabled = *boolean;
        } else if (key == "namespace") {
            const auto* text = std::get_if<std::string>(&value);
            if (!text || text->empty() || text->size() > 128 || !std::all_of(text->begin(), text->end(), [](unsigned char c) {
                return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.';
            })) return std::unexpected("notification_namespace must be one exact bounded namespace, without regex");
            nameSpace = *text;
        } else if (key == "rounding") {
            const auto* number = std::get_if<std::int64_t>(&value);
            if (!number || *number < -1 || *number > 64) return std::unexpected("notification_rounding must be -1 or 0..64");
            rounding = static_cast<int>(*number);
        } else return profile.set(key, value);
        return {};
    }

    std::expected<Settings, std::string> patched(const NativeConfig::Patch& patch) const {
        if (patch.size() > NativeConfig::FIELDS.size() + EXTRA_FIELDS.size()) return std::unexpected("too many notification settings");
        Settings candidate = *this;
        NativeConfig::Patch render;
        for (const auto& [key, value] : patch) {
            const auto result = candidate.set(key, value);
            if (!result) return std::unexpected(result.error());
            if (key != "enabled" && key != "namespace" && key != "rounding") render.emplace(key, value);
        }
        if (!render.empty()) {
            const auto checked = profile.patched(render);
            if (!checked) return std::unexpected(checked.error());
            candidate.profile = *checked;
        }
        return candidate;
    }

    NativeConfig::Patch values(bool prefixed = true) const {
        auto values = profile.values();
        values.emplace("enabled", enabled);
        values.emplace("namespace", nameSpace);
        values.emplace("rounding", std::int64_t{rounding});
        if (!prefixed) return values;
        NativeConfig::Patch result;
        for (auto& [key, value] : values) result.emplace(std::string{PREFIX} + key, std::move(value));
        return result;
    }
};

struct Rect {
    double x = 0, y = 0, width = 0, height = 0;
    bool operator==(const Rect&) const = default;
};

// Input regions are replacement envelopes, never a source of private pixels.
// The original notification layer is omitted in full, including pixels outside
// its input region. Unknown/infinite/full-screen geometry yields omission.
inline std::expected<std::vector<Rect>, std::string> boundedRegions(
    const std::vector<Rect>& input, double surfaceWidth, double surfaceHeight,
    double monitorWidth, double monitorHeight, bool infinite) {
    const auto finitePositive = [](double value) { return std::isfinite(value) && value > 0; };
    if (infinite || !finitePositive(surfaceWidth) || !finitePositive(surfaceHeight) ||
        !finitePositive(monitorWidth) || !finitePositive(monitorHeight) || input.empty() || input.size() > MAX_REGIONS)
        return std::unexpected("notification input geometry unavailable");
    std::vector<Rect> result;
    double area = 0;
    for (const auto& rectangle : input) {
        if (!std::isfinite(rectangle.x) || !std::isfinite(rectangle.y) ||
            !finitePositive(rectangle.width) || !finitePositive(rectangle.height))
            return std::unexpected("invalid notification input rectangle");
        const double left = std::max(0.0, rectangle.x), top = std::max(0.0, rectangle.y);
        const double right = std::min(surfaceWidth, rectangle.x + rectangle.width);
        const double bottom = std::min(surfaceHeight, rectangle.y + rectangle.height);
        if (right <= left || bottom <= top) continue;
        Rect clipped{left, top, right - left, bottom - top};
        if (clipped.width > monitorWidth * .8 || clipped.height > monitorHeight * .9)
            return std::unexpected("notification envelope is too large");
        area += clipped.width * clipped.height;
        if (!std::isfinite(area) || area > monitorWidth * monitorHeight * .4)
            return std::unexpected("notification envelope cannot cover the monitor");
        result.push_back(clipped);
    }
    if (result.empty()) return std::unexpected("empty notification envelope");
    return result;
}
} // namespace Hyprveil::Notifications
