#pragma once

#include <array>
#include <charconv>
#include <optional>
#include <string>
#include <string_view>

namespace Hyprveil {
struct Appearance {
    std::string variant = "satin";
    std::string color = "#ffffff";
    int grain = 50;
    int speed = 100;
    int darkness = 50;
    bool eye = true;
    int eyeSize = 80;
    bool operator==(const Appearance&) const = default;

    bool animated() const { return speed > 0 && darkness < 100 && color != "#000000"; }
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
        if (input.empty() || input.size() > 128) return std::nullopt;
        // Exactly seven ASCII-space-delimited fields; no partial native update.
        std::array<std::string_view, 7> fields;
        for (std::size_t i = 0; i < fields.size(); ++i) {
            const auto next = input.find(' ');
            if (i + 1 == fields.size()) {
                if (next != std::string_view::npos) return std::nullopt;
                fields[i] = input;
            } else {
                if (next == std::string_view::npos || next == 0) return std::nullopt;
                fields[i] = input.substr(0, next);
                input.remove_prefix(next + 1);
            }
        }
        Appearance result;
        if (fields[0] != "satin" && fields[0] != "telegram") return std::nullopt;
        result.variant = fields[0];
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
        return result;
    }
};
} // namespace Hyprveil
