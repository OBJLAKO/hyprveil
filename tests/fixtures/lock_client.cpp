// Real ext-session-lock client, restricted to an explicit synthetic lab.
#include "ext-session-lock-client.h"
#include <wayland-client.h>
#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace {
wl_display* display;
wl_compositor* compositor;
wl_shm* shm;
wl_output* output;
ext_session_lock_manager_v1* manager;
ext_session_lock_v1* lock;
ext_session_lock_surface_v1* lockSurface;
wl_surface* surface;
bool locked = false;
bool finished = false;
bool mapped = false;
std::string outputName, statePath;

void state() {
    const std::string temporary = statePath + ".tmp";
    std::ofstream out{temporary};
    out << "{\"lock_requested\":" << (lock ? "true" : "false")
        << ",\"locked\":" << (locked ? "true" : "false")
        << ",\"finished\":" << (finished ? "true" : "false")
        << ",\"mapped\":" << (mapped ? "true" : "false") << "}\n";
    out.close();
    std::filesystem::rename(temporary, statePath);
}

void onLocked(void*, ext_session_lock_v1*) { locked = true; state(); }
void onFinished(void*, ext_session_lock_v1*) { finished = true; state(); }
const ext_session_lock_v1_listener lockListener{onLocked, onFinished};

void configure(void*, ext_session_lock_surface_v1* object, uint32_t serial, uint32_t width, uint32_t height) {
    if (!width || !height || width > 8192 || height > 8192 || uint64_t{width} * height > 16777216)
        throw std::runtime_error("invalid synthetic lock dimensions");
    ext_session_lock_surface_v1_ack_configure(object, serial);
    const size_t size = size_t{width} * height * 4;
    const int fd = memfd_create("hyprveil-lock-fixture", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, size) != 0)
        throw std::runtime_error("lock fixture memory allocation failed");
    auto* pixels = static_cast<uint32_t*>(mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0));
    if (pixels == MAP_FAILED)
        throw std::runtime_error("lock fixture mmap failed");
    std::fill_n(pixels, size / 4, 0xFF28D8B8U); // Distinct opaque cyan lock pixels.
    auto* pool = wl_shm_create_pool(shm, fd, size);
    auto* buffer = wl_shm_pool_create_buffer(pool, 0, width, height, width * 4, WL_SHM_FORMAT_ARGB8888);
    wl_shm_pool_destroy(pool);
    close(fd);
    munmap(pixels, size);
    wl_surface_attach(surface, buffer, 0, 0);
    wl_surface_damage_buffer(surface, 0, 0, width, height);
    wl_surface_commit(surface);
    // Buffer remains alive until client teardown; one configure in this test.
    mapped = true;
    state();
}
const ext_session_lock_surface_v1_listener surfaceListener{configure};

void outputGeometry(void*, wl_output*, int32_t, int32_t, int32_t, int32_t, int32_t, const char*, const char*, int32_t) {}
void outputMode(void*, wl_output*, uint32_t, int32_t, int32_t, int32_t) {}
void outputDone(void*, wl_output*) {}
void outputScale(void*, wl_output*, int32_t) {}
void outputNamed(void*, wl_output*, const char* name) { outputName = name; }
void outputDescription(void*, wl_output*, const char*) {}
const wl_output_listener outputListener{outputGeometry, outputMode, outputDone, outputScale, outputNamed, outputDescription};

void global(void*, wl_registry* registry, uint32_t name, const char* interface, uint32_t version) {
    if (std::strcmp(interface, "wl_compositor") == 0)
        compositor = static_cast<wl_compositor*>(wl_registry_bind(registry, name, &wl_compositor_interface, std::min(version, 4U)));
    else if (std::strcmp(interface, "wl_shm") == 0)
        shm = static_cast<wl_shm*>(wl_registry_bind(registry, name, &wl_shm_interface, 1));
    else if (std::strcmp(interface, "wl_output") == 0) {
        if (output)
            throw std::runtime_error("lock fixture requires exactly one isolated output");
        output = static_cast<wl_output*>(wl_registry_bind(registry, name, &wl_output_interface, std::min(version, 4U)));
        wl_output_add_listener(output, &outputListener, nullptr);
    } else if (std::strcmp(interface, "ext_session_lock_manager_v1") == 0)
        manager = static_cast<ext_session_lock_manager_v1*>(wl_registry_bind(registry, name, &ext_session_lock_manager_v1_interface, 1));
}
void globalRemoved(void*, wl_registry*, uint32_t) {}
const wl_registry_listener registryListener{global, globalRemoved};

void guard(const std::string& runtime, const std::string& displayName, const std::string& path) {
    const auto envEquals = [](const char* name, const std::string& value) {
        const auto* environment = std::getenv(name);
        return environment && value == environment;
    };
    const auto dir = std::filesystem::path{runtime};
    struct stat info {}, marker {};
    const auto markerPath = dir / ".hyprveil-lab";
    if (dir.parent_path() != "/tmp" || !dir.filename().string().starts_with("hv-") ||
        lstat(runtime.c_str(), &info) != 0 || !S_ISDIR(info.st_mode) || info.st_uid != getuid() || (info.st_mode & 0777) != 0700 ||
        lstat(markerPath.c_str(), &marker) != 0 || !S_ISREG(marker.st_mode) || marker.st_uid != getuid() ||
        !envEquals("XDG_RUNTIME_DIR", runtime) || !envEquals("HYPRVEIL_LAB_RUNTIME", runtime) ||
        !envEquals("WAYLAND_DISPLAY", displayName) || displayName.empty() || displayName.find('/') != std::string::npos ||
        std::getenv("DISPLAY") || std::getenv("WAYLAND_SOCKET") || std::filesystem::path{path}.parent_path() != dir)
        throw std::runtime_error("refusing lock fixture outside explicit marked lab");
    std::ifstream file{markerPath};
    const std::string contents{std::istreambuf_iterator<char>{file}, std::istreambuf_iterator<char>{}};
    if (contents.find("\"runtime_dir\": \"" + runtime + "\"") == std::string::npos ||
        contents.find("\"wayland_display\": \"" + displayName + "\"") == std::string::npos)
        throw std::runtime_error("lock fixture marker identity mismatch");
}
} // namespace

int main(int argc, char** argv) {
    try {
        if (argc != 4)
            throw std::runtime_error("usage: lock-client LAB_RUNTIME LAB_DISPLAY STATE_PATH");
        guard(argv[1], argv[2], argv[3]);
        statePath = argv[3];
        display = wl_display_connect(argv[2]);
        if (!display)
            throw std::runtime_error("failed connecting marked display");
        auto* registry = wl_display_get_registry(display);
        wl_registry_add_listener(registry, &registryListener, nullptr);
        if (wl_display_roundtrip(display) < 0 || wl_display_roundtrip(display) < 0 || !compositor || !shm || !manager || !output || outputName != "HV-TEST")
            throw std::runtime_error("missing isolated HV-TEST session-lock interfaces");
        state();
        for (;;) {
            while (wl_display_prepare_read(display) != 0)
                if (wl_display_dispatch_pending(display) < 0)
                    throw std::runtime_error("Wayland connection closed");
            wl_display_flush(display);
            pollfd fds[]{{wl_display_get_fd(display), POLLIN, 0}, {STDIN_FILENO, POLLIN, 0}};
            if (poll(fds, 2, -1) < 0) {
                wl_display_cancel_read(display);
                continue;
            }
            if (fds[0].revents & POLLIN) {
                if (wl_display_read_events(display) < 0)
                    throw std::runtime_error("Wayland read failed");
                wl_display_dispatch_pending(display);
            } else
                wl_display_cancel_read(display);
            if (!(fds[1].revents & (POLLIN | POLLHUP)))
                continue;
            std::string command;
            if (!std::getline(std::cin, command))
                break;
            if (command == "lock" && !lock) {
                lock = ext_session_lock_manager_v1_lock(manager);
                ext_session_lock_v1_add_listener(lock, &lockListener, nullptr);
            } else if (command == "show" && lock && !surface) {
                surface = wl_compositor_create_surface(compositor);
                lockSurface = ext_session_lock_v1_get_lock_surface(lock, surface, output);
                ext_session_lock_surface_v1_add_listener(lockSurface, &surfaceListener, nullptr);
            } else if (command == "unlock" && lock && locked) {
                ext_session_lock_v1_unlock_and_destroy(lock);
                lock = nullptr;
                locked = mapped = false;
                if (lockSurface) ext_session_lock_surface_v1_destroy(lockSurface);
                if (surface) wl_surface_destroy(surface);
                lockSurface = nullptr;
                surface = nullptr;
                wl_display_roundtrip(display);
            } else if (command == "quit")
                break;
            else if (command != "status")
                throw std::runtime_error("invalid lock fixture command/state");
            wl_display_flush(display);
            state();
        }
        if (lock && locked) ext_session_lock_v1_unlock_and_destroy(lock);
        if (lock && !locked) ext_session_lock_v1_destroy(lock);
        wl_display_roundtrip(display);
        wl_display_disconnect(display);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
