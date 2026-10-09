#include "Appearance.hpp"
#include <cassert>
#include <iostream>
int main() {
    const auto base = Hyprveil::Appearance::parse("satin #FFFFFF 50 100 50 1 80");
    assert(base && *base == Hyprveil::Appearance{});
    const auto black = Hyprveil::Appearance::parse("telegram #000000 100 200 0 0 128");
    assert(black && !black->animated() && black->tint() == (std::array<float,3>{0,0,0}));
    const auto stopped = Hyprveil::Appearance::parse("telegram #123456 0 0 100 1 40");
    assert(stopped && !stopped->animated());
    for (const auto alias : {"satin", "telegram", "grid", "404", "cmatrix", "anon", "liquid-glass", "liquidglass"}) {
        const auto parsed = Hyprveil::Appearance::parse(std::string{alias} + " #ffffff 50 100 50 1 80");
        assert(parsed && parsed->variant == Hyprveil::Appearance::canonicalVariant(alias));
    }
    for (const auto shape : {"eye", "lock", "shield", "none"}) {
        for (const auto opacity : {0, 75, 100}) {
            const auto parsed = Hyprveil::Appearance::parse(std::string{"aurora #ffffff 50 100 50 1 80 "} + shape + " " + std::to_string(opacity));
            assert(parsed && parsed->icon == shape && parsed->iconOpacity == opacity);
            // Strict API 1 clients continue to receive their seven fields.
            assert(parsed->json().find("icon") == std::string::npos);
        }
    }
    for (const auto suffix : {"eye", "eye -1", "eye 101", "eye true", "other 50", "eye 50 extra"})
        assert(!Hyprveil::Appearance::parse(std::string{"aurora #ffffff 50 100 50 1 80 "} + suffix));
    for (std::size_t index = 0; index < Hyprveil::Appearance::VARIANTS.size(); ++index) {
        const auto name = Hyprveil::Appearance::VARIANTS[index];
        const auto material = Hyprveil::Appearance::parse(std::string{name} + " #FFFFFF 50 100 50 1 80");
        assert(material && material->variant == name && material->shaderVariant() == static_cast<int>(index));
        assert(material->animated() == (name != "matte"));
        assert(material->json().find("\"variant\":\"" + std::string{name} + "\"") != std::string::npos);
        const auto dark = Hyprveil::Appearance::parse(std::string{name} + " #ffffff 100 200 100 0 128");
        const auto frozen = Hyprveil::Appearance::parse(std::string{name} + " #ffffff 0 0 0 1 40");
        assert(dark && frozen && !dark->animated() && !frozen->animated());
    }
    for (const auto* invalid : {"", "satin #ffffff 50 100 50 1 80 extra", "satin #fff 50 100 50 1 80", "satin #fff\"ff 50 100 50 1 80",
         "satin #ffffff -1 100 50 1 80", "satin #ffffff 101 100 50 1 80", "satin #ffffff nan 100 50 1 80",
         "satin #ffffff 50 201 50 1 80", "satin #ffffff 50 100 101 1 80", "satin #ffffff 50 100 50 true 80",
         "satin #ffffff 50 100 50 1 39", "satin #ffffff 50 100 50 1 129", "other #ffffff 50 100 50 1 80",
         "satin  #ffffff 50 100 50 1 80", "satin #ffffff 50 100 50 1 80 ", "satin #ffffff 50 100 50 1 8.0"})
        assert(!Hyprveil::Appearance::parse(invalid));
    assert(!Hyprveil::Appearance::parse(std::string(1024, 'x')));
    std::cout << "appearance: ten materials, stable shader mapping, static matte, defaults, extrema and malformed inputs passed\n";
}
