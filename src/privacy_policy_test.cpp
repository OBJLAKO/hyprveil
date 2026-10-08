#include "PrivacyPolicy.hpp"

#include <array>
#include <cstdlib>
#include <iostream>
#include <string_view>

struct Window {
    Window* parent = nullptr;
    bool privateFlag = false;
    bool orphanLatch = false;
};

static int checks = 0;
static void require(bool ok, std::string_view name) {
    if (!ok) {
        std::cerr << "FAIL: " << name << '\n';
        std::exit(1);
    }
    ++checks;
}

static bool privateWindow(Window* window) {
    return Hyprveil::Privacy::matchesOrMalformed(window, [](auto* w) { return w->parent; },
        [](auto* w) { return w; }, [](auto* w) { return w->privateFlag || w->orphanLatch; });
}

static bool closingAncestor(Window* child, Window* closing) {
    return Hyprveil::Privacy::matchesOrMalformed(child->parent, [](auto* w) { return w->parent; },
        [](auto* w) { return w; }, [closing](auto* w) { return w == closing; });
}

int main() {
    Window parent, child{&parent}, grandchild{&child}, unrelated;
    require(!privateWindow(&child), "public child of public parent");
    require(!closingAncestor(&unrelated, &parent), "unrelated public window excluded from closing family");
    require(closingAncestor(&grandchild, &parent), "deep transient reaches closing ancestor");
    // No capture or prior policy observation has occurred before this change.
    parent.privateFlag = true;
    require(privateWindow(&grandchild), "current native flag protects descendants without previous capture");
    if (privateWindow(&parent) && closingAncestor(&child, &parent))
        child.orphanLatch = true;
    if (privateWindow(&parent) && closingAncestor(&grandchild, &parent))
        grandchild.orphanLatch = true;
    child.parent = nullptr; // Native parent() stops returning an unmapped parent.
    require(privateWindow(&child) && privateWindow(&grandchild), "surviving children retain privacy after parent unmap");
    child.parent = &unrelated;
    require(privateWindow(&child), "public reparent does not release retained privacy");
    child.privateFlag = false;
    require(privateWindow(&child), "title or rule reapply to native false does not release retained privacy");
    // Child close propagates protection before its own retention is erased.
    if (privateWindow(&child) && closingAncestor(&grandchild, &child))
        grandchild.orphanLatch = true;
    child.orphanLatch = false;
    grandchild.parent = nullptr;
    require(!privateWindow(&child) && privateWindow(&grandchild), "child unmap clears own latch after protecting surviving grandchild");
    grandchild.orphanLatch = false; // Successful explicit native false setter.
    require(!privateWindow(&grandchild), "explicit reset releases an orphan without a live private parent");
    child.parent = &parent;
    child.orphanLatch = false;
    require(privateWindow(&child), "explicit reset cannot override a live private parent");

    Window cyclicA, cyclicB;
    cyclicA.parent = &cyclicB;
    cyclicB.parent = &cyclicA;
    require(privateWindow(&cyclicA), "public cyclic hierarchy fails closed");
    require(closingAncestor(&cyclicA, &unrelated), "malformed close-time ancestry fails closed");
    std::array<Window, 33> chain{};
    for (std::size_t i = 0; i + 1 < chain.size(); ++i)
        chain[i].parent = &chain[i + 1];
    require(privateWindow(&chain.front()), "overlong public hierarchy fails closed");
    chain[31].parent = nullptr;
    require(!privateWindow(&chain.front()), "exactly bounded public hierarchy stays public");
    chain[31].privateFlag = true;
    require(privateWindow(&chain.front()), "last allowed ancestor privacy is observed");
    require(!privateWindow(nullptr), "missing window stays public");
    std::cout << checks << " privacy ancestry/lifecycle checks passed\n";
}
