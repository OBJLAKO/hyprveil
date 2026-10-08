#pragma once

#include <algorithm>
#include <array>
#include <cstddef>

namespace Hyprveil::Privacy {
// Protocol ancestry is client-controlled. Matching a protected ancestor or
// encountering a malformed/deeper hierarchy always denies capture. Keeping
// this walk bounded also lets close-time inheritance safely inspect every
// live child before the compositor removes the closing parent's mapped state.
template <typename Ref, typename Next, typename Identity, typename Match>
bool matchesOrMalformed(Ref current, Next next, Identity identity, Match match) {
    std::array<decltype(identity(current)), 32> visited{};
    std::size_t count = 0;
    while (current) {
        const auto key = identity(current);
        if (count == visited.size() || std::find(visited.begin(), visited.begin() + count, key) != visited.begin() + count)
            return true;
        visited[count++] = key;
        if (match(current))
            return true;
        current = next(current);
    }
    return false;
}
} // namespace Hyprveil::Privacy
