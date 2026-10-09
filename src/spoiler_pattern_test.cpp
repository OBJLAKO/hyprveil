#include "SpoilerPattern.hpp"
#include <cassert>
#include <limits>
int main() {
    const auto shader = std::string_view{Hyprveil::Spoiler::FRAGMENT};
    // The 28-bit bitmap columns need ES highp integers. Mesa can mask this
    // regression because its mediump implementation often uses 32 bits.
    assert(shader.find("precision highp int;") != std::string_view::npos);
    // GLSL pow is undefined for negative bases even with exponent 2.0.
    // Keep signed squares as multiplication; only clamped powers are allowed.
    for (auto power = shader.find("pow("); power != std::string_view::npos; power = shader.find("pow(", power + 4))
        assert(shader.substr(power).starts_with("pow(clamp("));
    assert(Hyprveil::Spoiler::seconds(3600000) == 0.0F);
    const auto longUptime = Hyprveil::Spoiler::seconds(std::numeric_limits<unsigned long long>::max());
    assert(longUptime >= 0 && longUptime < 3600);
    auto* eye = Hyprveil::Spoiler::eye();
    assert(eye && cairo_surface_status(eye) == CAIRO_STATUS_SUCCESS);
    assert(cairo_image_surface_get_width(eye) == Hyprveil::Spoiler::EYE_SIZE);
    assert(cairo_image_surface_get_height(eye) == Hyprveil::Spoiler::EYE_SIZE);
    const auto* pixels = reinterpret_cast<const unsigned*>(cairo_image_surface_get_data(eye));
    assert(pixels[64 * Hyprveil::Spoiler::EYE_SIZE] == 0); // no opaque badge
    cairo_surface_destroy(eye);
    for (const auto shape : {"eye", "lock", "shield"}) {
        auto* icon = Hyprveil::Spoiler::icon(shape);
        assert(icon && cairo_surface_status(icon) == CAIRO_STATUS_SUCCESS);
        const auto* data = reinterpret_cast<const unsigned*>(cairo_image_surface_get_data(icon));
        int drawn = 0;
        for (int index = 0; index < 128 * 128; ++index)
            drawn += (data[index] >> 24) != 0;
        assert(drawn > 250 && drawn < 4000);
        assert(data[0] == 0 && data[127 * 128 + 127] == 0);
        if (std::string_view{shape} == "eye") {
            // A straight symmetric glyph cannot regress to the uneven slash.
            unsigned difference = 0;
            for (int y = 0; y < 128; ++y)
                for (int x = 0; x < 128; ++x) {
                    const auto a = static_cast<int>(data[y * 128 + x] >> 24);
                    const auto b = static_cast<int>(data[y * 128 + 127 - x] >> 24);
                    difference += a > b ? a - b : b - a;
                }
            assert(difference < 10000); // Small subpixel raster rounding only.
        }
        cairo_surface_destroy(icon);
    }
    assert(!Hyprveil::Spoiler::icon("none"));
    assert(!Hyprveil::Spoiler::icon("unknown"));
}
