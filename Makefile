CXX ?= c++
PKG_CONFIG ?= pkg-config
CPPFLAGS += $(shell $(PKG_CONFIG) --cflags hyprland)
CXXFLAGS ?= -O2 -g
CXXFLAGS += -std=c++23 -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers -fPIC -fno-gnu-unique
# Admission pins must survive copying the same source to another checkout.
CXXFLAGS += -ffile-prefix-map="$(CURDIR)"=. -fdebug-prefix-map="$(CURDIR)"=.
LDLIBS += $(shell $(PKG_CONFIG) --libs hyprland) -lGLESv2 -ldl -lz

.PHONY: all clean upgrade-guard test-session-guard test-png-guard test-privacy-policy test-spoiler-pattern test-appearance test-native-config
all: build/hyprveil.so

build/abi-probe: src/abi_probe.cpp
	mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

build/hyprveil.so: src/main.cpp src/SessionGuard.hpp src/PngGuard.hpp src/PrivacyPolicy.hpp src/SpoilerPattern.hpp src/Appearance.hpp src/NativeConfig.hpp
	mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -shared $< -o $@.tmp $(LDFLAGS) $(LDLIBS)
	mv $@.tmp $@

# One-time migration bridge for the explicitly pinned predecessor ELFs.
build/hyprveil-upgrade-guard.so: tools/upgrade_guard.cpp src/SessionGuard.hpp src/PrivacyPolicy.hpp
	mkdir -p build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -I src -shared $< -o $@.tmp $(LDFLAGS) $(LDLIBS) -lcrypto
	mv $@.tmp $@

upgrade-guard: build/hyprveil-upgrade-guard.so

build/session-guard-test: src/session_guard_test.cpp src/SessionGuard.hpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) $< -o $@

test-session-guard: build/session-guard-test
	./build/session-guard-test

build/png-guard-test: src/png_guard_test.cpp src/PngGuard.hpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) $(shell $(PKG_CONFIG) --cflags cairo) $< -o $@ $(shell $(PKG_CONFIG) --libs cairo) -lz

test-png-guard: build/png-guard-test
	./build/png-guard-test

build/privacy-policy-test: src/privacy_policy_test.cpp src/PrivacyPolicy.hpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) $< -o $@

test-privacy-policy: build/privacy-policy-test
	./build/privacy-policy-test

build/spoiler-pattern-test: src/spoiler_pattern_test.cpp src/SpoilerPattern.hpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) $(shell $(PKG_CONFIG) --cflags cairo) $< -o $@ $(shell $(PKG_CONFIG) --libs cairo)

test-spoiler-pattern: build/spoiler-pattern-test
	./build/spoiler-pattern-test

build/appearance-test: src/appearance_test.cpp src/Appearance.hpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) $< -o $@

test-appearance: build/appearance-test
	./build/appearance-test

build/native-config-test: src/native_config_test.cpp src/NativeConfig.hpp src/Appearance.hpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) $< -o $@

test-native-config: build/native-config-test
	./build/native-config-test

clean:
	rm -rf build
