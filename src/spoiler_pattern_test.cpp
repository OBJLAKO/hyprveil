#include "SpoilerPattern.hpp"
#include <cassert>
#include <limits>
int main() {
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
}
