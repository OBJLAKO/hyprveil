#include "NotificationConfig.hpp"
#include <cassert>
#include <limits>

int main() {
    using namespace Hyprveil;
    Notifications::Settings defaults;
    assert(!defaults.enabled && defaults.profile.mode == "omit");
    const auto changed = defaults.patched({{"enabled", true}, {"mode", std::string{"spoiler"}}, {"variant", std::string{"404"}}, {"color", std::string{"#AABBCC"}}});
    assert(changed && changed->profile.appearance.variant == "error404" && changed->profile.appearance.color == "#aabbcc");
    assert(defaults.profile.mode == "omit");
    assert(!defaults.patched({{"enabled", std::int64_t{1}}}));
    assert(!defaults.patched({{"namespace", std::string{".*"}}}));
    assert(!defaults.patched({{"namespace", std::string(129, 'a')}}));
    assert(!defaults.patched({{"rounding", std::int64_t{-2}}}));
    assert(defaults.patched({{"rounding", std::int64_t{64}}}));
    assert(!defaults.patched({{"not_a_field", true}}));
    assert(changed->values().size() == 18 && changed->values(false).contains("mode"));
    using Notifications::Rect;
    const std::vector<Rect> cards{{1500, 50, 380, 100}, {1500, 160, 380, 180}};
    auto safe = Notifications::boundedRegions(cards, 1920, 1080, 1920, 1080, false);
    assert(safe && *safe == cards);
    assert(!Notifications::boundedRegions(cards, 1920, 1080, 1920, 1080, true));
    assert(!Notifications::boundedRegions({{0, 0, 1920, 1080}}, 1920, 1080, 1920, 1080, false));
    assert(!Notifications::boundedRegions({}, 1920, 1080, 1920, 1080, false));
    assert(!Notifications::boundedRegions(std::vector<Rect>(33, {0,0,1,1}),1920,1080,1920,1080,false));
    assert(!Notifications::boundedRegions({{0,0,std::numeric_limits<double>::infinity(),10}},1920,1080,1920,1080,false));
    const auto clipped = Notifications::boundedRegions({{-20,-10,100,100}},1920,1080,1920,1080,false);
    assert(clipped && clipped->front() == Rect(0,0,80,90));
}
