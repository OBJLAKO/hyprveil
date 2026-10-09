#include <src/version.h>
#include <cstdio>
#include <string>
#include <string_view>
#include "SessionGuard.hpp"

int main(int argc, char** argv) {
    if (argc > 2 || (argc == 2 && std::string_view{argv[1]} != "--check")) {
        std::fputs("usage: abi-probe [--check]\n", stderr);
        return 2;
    }
    // Same ABI spelling as PluginAPI.hpp v0.56.2, without linking compositor
    // globals/static constructors into a standalone helper.
    const auto stripPatch = [](std::string_view value) {
        return std::string{value.substr(0, value.find_last_of('.'))};
    };
    const auto value = std::string{GIT_COMMIT_HASH} + "_aq_" + stripPatch(AQUAMARINE_VERSION) +
        "_hu_" + stripPatch(HYPRUTILS_VERSION) + "_hg_" + stripPatch(HYPRGRAPHICS_VERSION) +
        "_hc_" + stripPatch(HYPRCURSOR_VERSION) + "_hlg_" + stripPatch(HYPRLANG_VERSION);
    if (argc == 2) {
        try { Hyprveil::requireReviewedAbi(value, value); }
        catch (const std::runtime_error& error) {
            std::fprintf(stderr, "%s\nHeader ABI: %s\n", error.what(), value.c_str());
            return 1;
        }
    }
    std::puts(value.c_str());
    return 0;
}
