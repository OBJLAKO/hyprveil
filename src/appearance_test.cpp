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
    for (const auto* invalid : {"", "satin #ffffff 50 100 50 1 80 extra", "satin #fff 50 100 50 1 80", "satin #fff\"ff 50 100 50 1 80",
         "satin #ffffff -1 100 50 1 80", "satin #ffffff 101 100 50 1 80", "satin #ffffff nan 100 50 1 80",
         "satin #ffffff 50 201 50 1 80", "satin #ffffff 50 100 101 1 80", "satin #ffffff 50 100 50 true 80",
         "satin #ffffff 50 100 50 1 39", "satin #ffffff 50 100 50 1 129", "other #ffffff 50 100 50 1 80",
         "satin  #ffffff 50 100 50 1 80", "satin #ffffff 50 100 50 1 80 ", "satin #ffffff 50 100 50 1 8.0"})
        assert(!Hyprveil::Appearance::parse(invalid));
    assert(!Hyprveil::Appearance::parse(std::string(1024, 'x')));
    std::cout << "appearance: defaults, extrema, canonical color, animation and 17 malformed inputs passed\n";
}
