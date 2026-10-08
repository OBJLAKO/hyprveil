#include <src/version.h>
#include <cstdio>
#include <string>
#include <string_view>

int main() {
    // Same ABI spelling as PluginAPI.hpp v0.56.2, without linking compositor
    // globals/static constructors into a standalone helper.
    const auto stripPatch = [](std::string_view value) {
        return std::string{value.substr(0, value.find_last_of('.'))};
    };
    const auto value = std::string{GIT_COMMIT_HASH} + "_aq_" + stripPatch(AQUAMARINE_VERSION) +
        "_hu_" + stripPatch(HYPRUTILS_VERSION) + "_hg_" + stripPatch(HYPRGRAPHICS_VERSION) +
        "_hc_" + stripPatch(HYPRCURSOR_VERSION) + "_hlg_" + stripPatch(HYPRLANG_VERSION);
    std::puts(value.c_str());
}
