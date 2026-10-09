#pragma once

#include <array>
#include <charconv>
#include <optional>
#include <string>
#include <string_view>

namespace Hyprveil {
struct Appearance {
    inline static constexpr std::array<std::string_view, 10> VARIANTS{
        "prism", "signal", "aurora", "contour", "radar", "matte", "error404", "matrix", "anonymous", "glass"};
    std::string variant = "prism";
    std::string color = "#ffffff";
    int grain = 50;
    int speed = 100;
    int darkness = 50;
    bool eye = true;
    int eyeSize = 80;
    std::string icon = "eye";
    int iconOpacity = 75;
    bool operator==(const Appearance&) const = default;

    static std::string_view canonicalVariant(std::string_view name) {
        if (name == "satin") return "prism";
        if (name == "telegram") return "signal";
        if (name == "grid") return "radar";
        if (name == "404") return "error404";
        if (name == "cmatrix") return "matrix";
        if (name == "anon") return "anonymous";
        if (name == "liquid-glass" || name == "liquidglass") return "glass";
        return name;
    }
    static bool validVariant(std::string_view name) {
        name = canonicalVariant(name);
        for (const auto candidate : VARIANTS)
            if (candidate == name) return true;
        return false;
    }
    int shaderVariant() const {
        for (std::size_t index = 0; index < VARIANTS.size(); ++index)
            if (VARIANTS[index] == canonicalVariant(variant)) return static_cast<int>(index);
        return 0;
    }
    static bool validIcon(std::string_view name) {
        return name == "eye" || name == "lock" || name == "shield" || name == "none";
    }
    bool animated() const { return variant != "matte" && speed > 0 && darkness < 100 && color != "#000000"; }
    std::array<float, 3> tint() const {
        const auto nibble = [](char c) { return c <= '9' ? c - '0' : c - 'a' + 10; };
        return {float(nibble(color[1]) * 16 + nibble(color[2])) / 255.F,
                float(nibble(color[3]) * 16 + nibble(color[4])) / 255.F,
                float(nibble(color[5]) * 16 + nibble(color[6])) / 255.F};
    }
    std::string json() const {
        // All strings are admitted through a fixed variant/hex grammar.
        return "{\"variant\":\"" + variant + "\",\"color\":\"" + color +
               "\",\"grain\":" + std::to_string(grain) + ",\"speed\":" + std::to_string(speed) +
               ",\"darkness\":" + std::to_string(darkness) + ",\"eye\":" + (eye ? "true" : "false") +
               ",\"eye_size\":" + std::to_string(eyeSize) + "}";
    }
    static std::optional<Appearance> parse(std::string_view input) {
        if (input.empty() || input.size() > 160) return std::nullopt;
        // Seven legacy fields or nine including shape/opacity, strictly
        // ASCII-space-delimited. Never partially commit an invalid choice.
        std::array<std::string_view, 9> fields;
        std::size_t count = 0;
        while (!input.empty()) {
            if (count == fields.size()) return std::nullopt;
            const auto next = input.find(' ');
            fields[count++] = input.substr(0, next);
            if (fields[count - 1].empty()) return std::nullopt;
            if (next == std::string_view::npos) break;
            input.remove_prefix(next + 1);
            if (input.empty()) return std::nullopt;
        }
        if (count != 7 && count != 9) return std::nullopt;
        Appearance result;
        if (!validVariant(fields[0])) return std::nullopt;
        result.variant = canonicalVariant(fields[0]);
        if (fields[1].size() != 7 || fields[1][0] != '#') return std::nullopt;
        result.color = fields[1];
        for (std::size_t i = 1; i < result.color.size(); ++i) {
            auto& c = result.color[i];
            if (c >= 'A' && c <= 'F') c += 'a' - 'A';
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return std::nullopt;
        }
        const auto integer = [](std::string_view field, int& value, int minimum, int maximum) {
            if (field.empty() || field.size() > 3 || field.front() < '0' || field.front() > '9') return false;
            const auto [end, error] = std::from_chars(field.data(), field.data() + field.size(), value);
            return error == std::errc{} && end == field.data() + field.size() && value >= minimum && value <= maximum;
        };
        if (!integer(fields[2], result.grain, 0, 100) || !integer(fields[3], result.speed, 0, 200) ||
            !integer(fields[4], result.darkness, 0, 100) || !integer(fields[6], result.eyeSize, 40, 128)) return std::nullopt;
        if (fields[5] != "0" && fields[5] != "1") return std::nullopt;
        result.eye = fields[5] == "1";
        if (count == 9) {
            if (!validIcon(fields[7]) || !integer(fields[8], result.iconOpacity, 0, 100)) return std::nullopt;
            result.icon = fields[7];
        }
        return result;
    }
};
} // namespace Hyprveil
