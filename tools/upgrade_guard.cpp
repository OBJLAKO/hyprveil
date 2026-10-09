// Temporary, exact-release bridge. It holds every compositor capture copy
// while the orchestrator replaces Hyprveil and transfers weak privacy state.
// Build: c++ -std=c++23 -shared -fPIC -fno-gnu-unique -O2 -I src
//        tools/upgrade_guard.cpp -o build/hyprveil-upgrade-guard.so
//        $(pkg-config --cflags --libs hyprland) -lcrypto -ldl
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <dlfcn.h>
#include <filesystem>
#include <fstream>
#include <fcntl.h>
#include <linux/magic.h>
#include <openssl/evp.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/vfs.h>
#include <unistd.h>
#include <unordered_map>
#include <vector>

#include <src/Compositor.hpp>
#include <src/desktop/view/Window.hpp>
#include <src/event/EventBus.hpp>
#include <src/managers/screenshare/ScreenshareManager.hpp>
#include <src/plugins/PluginAPI.hpp>
#include <src/plugins/PluginSystem.hpp>
#include <src/protocols/XDGShell.hpp>
#include "PrivacyPolicy.hpp"
#include "SessionGuard.hpp"

namespace {
struct PinnedRelease {
    std::string_view digest;
    std::uintptr_t privateWindowVaddr;
};
// These local-symbol offsets are valid only for the exact immutable ELF whose
// digest accompanies them. An unknown build is refused before native access.
constexpr std::array<PinnedRelease, 6> OLD_RELEASES{{
    {"fdf4c7d008af84e9c9e92d3d06bb2833cea77b41be54a2072a4101990bee0630", 0x16890},
    {"cb787c8fc8001fcc822e2a4907d515e8fec087616245557d3244dba461d2dc30", 0x19610},
    {"894ec2f1ef28c8f4f6d56d5fbde30c6ba13f01fd47a477adf746c13f7b4d916b", 0x1aa10},
    {"6a9c071c38cef45eb5c179312e5af1e846405b4622578a78c8bf3afebbb55ba8", 0x1bfd0},
    {"0c792b967f3204b5302aaf151997fc972cff04ae07be69b8c43b81fe0e51b95e", 0x1bfd0},
    {"0426199c06472e4ae262972ea9d13d9fcea11229af55f7274057ce5e370e4196", 0x226e0},
}};
constexpr char COMMIT_SYMBOL[] = "_ZN11Screenshare19CScreenshareManager14onOutputCommitEN9Hyprutils6Memory14CSharedPointerIN7Monitor8CMonitorEEE";
using OldPrivateFn = bool (*)(PHLWINDOW);
using CommitFn = void (*)(Screenshare::CScreenshareManager*, PHLMONITOR);
using AdoptFn = void (*)(const std::vector<PHLWINDOWREF>&);
HANDLE gHandle = nullptr;
HANDLE gOldHandle = nullptr;
std::string gOldPath;
OldPrivateFn gOldPrivate = nullptr;
CFunctionHook* gCommitHook = nullptr;
SP<SHyprCtlCommand> gCommand;
bool gHeld = true, gTransferred = false, gOverflow = false;
std::uint64_t gHeldCommits = 0;
std::vector<PHLWINDOWREF> gPrivate;
CHyprSignalListener gCloseListener, gOpenListener, gRuleListener;
struct RoleWatch {
    PHLWINDOWREF window;
    WP<CXDGToplevelResource> role;
    CHyprSignalListener destroyed;
};
std::unordered_map<const Desktop::View::CWindow*, RoleWatch> gRoles;

void admitSession() {
    const auto* runtime = std::getenv("XDG_RUNTIME_DIR");
    const auto* lab = std::getenv("HYPRVEIL_LAB_RUNTIME");
    const auto* seat = std::getenv("LIBSEAT_BACKEND");
    if (runtime && lab && std::string{runtime} == lab && seat && std::string{seat} == "hyprveil-disabled") {
        struct stat directory {}, marker {};
        const auto path = std::filesystem::path{runtime} / ".hyprveil-lab";
        if (lstat(runtime, &directory) != 0 || !S_ISDIR(directory.st_mode) || directory.st_uid != getuid() ||
            (directory.st_mode & 0777) != 0700 || lstat(path.c_str(), &marker) != 0 ||
            !S_ISREG(marker.st_mode) || marker.st_uid != getuid())
            throw std::runtime_error("upgrade guard: invalid marked lab");
        return;
    }
    const auto now = std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
    Hyprveil::requireLiveMarker(runtime ? runtime : "", getpid(), g_pCompositor->m_instanceSignature, now);
}

struct ModuleFile {
    std::string digest;
    struct stat info;
    bool btrfs;
};

ModuleFile ownedDigest(const std::string& path) {
    if (!std::filesystem::path{path}.is_absolute() || std::filesystem::canonical(path) != std::filesystem::path{path})
        throw std::runtime_error("upgrade guard: module path must be absolute and contain no symlinks");
    Hyprveil::Detail::OwnedFd fd{open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK)};
    struct stat info {};
    if (fd.get() < 0 || fstat(fd.get(), &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != getuid() ||
        info.st_nlink != 1 || (info.st_mode & 0222) || info.st_size <= 0 || info.st_size > 32 * 1024 * 1024)
        throw std::runtime_error("upgrade guard: module must be an owned immutable regular file under 32 MiB");
    auto* hash = EVP_MD_CTX_new();
    if (!hash)
        throw std::runtime_error("upgrade guard: digest allocation failed");
    Hyprutils::Utils::CScopeGuard destroy([hash]() { EVP_MD_CTX_free(hash); });
    if (EVP_DigestInit_ex(hash, EVP_sha256(), nullptr) != 1)
        throw std::runtime_error("upgrade guard: digest initialization failed");
    std::array<unsigned char, 65536> bytes{};
    std::uint64_t size = 0;
    for (;;) {
        const auto count = read(fd.get(), bytes.data(), bytes.size());
        if (count < 0 && errno == EINTR)
            continue;
        if (count < 0)
            throw std::runtime_error("upgrade guard: cannot read module");
        if (!count)
            break;
        size += count;
        if (size > 32 * 1024 * 1024 || EVP_DigestUpdate(hash, bytes.data(), count) != 1)
            throw std::runtime_error("upgrade guard: module digest failed or grew");
    }
    if (size != static_cast<std::uint64_t>(info.st_size))
        throw std::runtime_error("upgrade guard: module size changed");
    std::array<unsigned char, EVP_MAX_MD_SIZE> output{};
    unsigned int length = 0;
    if (EVP_DigestFinal_ex(hash, output.data(), &length) != 1 || length != 32)
        throw std::runtime_error("upgrade guard: invalid module digest");
    constexpr char hex[] = "0123456789abcdef";
    std::string digest;
    for (unsigned int index = 0; index < length; ++index) {
        digest += hex[output[index] >> 4];
        digest += hex[output[index] & 15];
    }
    struct statfs filesystem {};
    if (fstatfs(fd.get(), &filesystem) != 0)
        throw std::runtime_error("upgrade guard: cannot attest module filesystem");
    return {digest, info, filesystem.f_type == BTRFS_SUPER_MAGIC};
}

void verifyMapping(const void* address, const std::string& path, const struct stat& file, bool btrfs) {
    std::ifstream maps{"/proc/self/maps"};
    std::string line;
    const auto pointer = reinterpret_cast<std::uintptr_t>(address);
    while (std::getline(maps, line)) {
        std::istringstream stream{line};
        std::string span, perms, offset, device, mappedPath;
        std::uint64_t inode = 0;
        if (!(stream >> span >> perms >> offset >> device >> inode))
            continue;
        std::getline(stream >> std::ws, mappedPath);
        const auto dash = span.find('-'), colon = device.find(':');
        if (dash == std::string::npos || colon == std::string::npos)
            continue;
        const auto start = std::stoull(span.substr(0, dash), nullptr, 16);
        const auto end = std::stoull(span.substr(dash + 1), nullptr, 16);
        if (pointer < start || pointer >= end)
            continue;
        if (mappedPath != path || inode != file.st_ino || perms.size() < 3 || perms[2] != 'x' ||
            std::stoull(device.substr(0, colon), nullptr, 16) != major(file.st_dev) ||
            // Btrfs getattr reports the subvolume's anonymous st_dev, while
            // /proc/maps reports the backing superblock device. Exact path,
            // inode, SHA and executable-module base still have to match.
            (!btrfs && std::stoull(device.substr(colon + 1), nullptr, 16) != minor(file.st_dev)))
            throw std::runtime_error("upgrade guard: mapped module inode/address mismatch: " + mappedPath +
                "; expected " + path + "; mapping permissions " + perms + "; inode " + std::to_string(inode) +
                "/" + std::to_string(file.st_ino) + "; device " + device + "/" +
                std::to_string(major(file.st_dev)) + ":" + std::to_string(minor(file.st_dev)));
        return;
    }
    throw std::runtime_error("upgrade guard: cannot attest executable module mapping");
}

bool oldPresent() {
    const auto* old = g_pPluginSystem->getPluginByPath(gOldPath);
    return old && old->m_handle == gOldHandle && old->m_name == "hyprveil";
}

bool snapshotted(const PHLWINDOW& window) {
    return std::any_of(gPrivate.begin(), gPrivate.end(), [&](const auto& weak) {
        return !weak.expired() && weak.get() == window.get();
    });
}

bool effectivePrivate(const PHLWINDOW& window) {
    if (oldPresent())
        return gOldPrivate(window);
    // Keep ancestry/weak retention alive independently while the old DSO is
    // absent. Role-destroy listeners observe parents before weak links vanish.
    return Hyprveil::Privacy::matchesOrMalformed(window,
        [](const auto& item) { return item->parent(); }, [](const auto& item) { return item.get(); },
        [](const auto& item) { return snapshotted(item) ||
            (item->m_ruleApplicator && item->m_ruleApplicator->noScreenShare().valueOrDefault()); });
}

void snapshot() noexcept {
    if (!gHeld || gTransferred)
        return;
    try {
        std::erase_if(gPrivate, [](const auto& item) { return item.expired(); });
        for (const auto& window : Desktop::windowState()->windows()) {
            if (!window || !window->m_isMapped || snapshotted(window) || !effectivePrivate(window))
                continue;
            if (gPrivate.size() == 4096) {
                gOverflow = true;
                return;
            }
            gPrivate.push_back(window);
        }
    } catch (...) {
        // Event callbacks cannot propagate resource failure into the desktop.
        // An incomplete snapshot can never authorize release/adoption.
        gOverflow = true;
    }
}

void watchRole(const PHLWINDOW& window) noexcept {
    try {
        std::erase_if(gRoles, [](const auto& item) { return item.second.window.expired() || item.second.role.expired(); });
        if (!window || window->m_isX11 || !window->m_xdgSurface || !window->m_xdgSurface->m_toplevel)
            return;
        const auto role = window->m_xdgSurface->m_toplevel;
        const auto found = gRoles.find(window.get());
        if (found != gRoles.end() && found->second.window.get() == window.get() && found->second.role == role)
            return;
        if (gRoles.size() == 4096 && found == gRoles.end()) {
            gOverflow = true;
            return;
        }
        auto listener = role->m_events.destroy.listen([]() { snapshot(); });
        gRoles.insert_or_assign(window.get(), RoleWatch{window, role, std::move(listener)});
    } catch (...) {
        gOverflow = true;
    }
}

void commitHook(Screenshare::CScreenshareManager* self, PHLMONITOR monitor) {
    if (gHeld) {
        ++gHeldCommits;
        return;
    }
    reinterpret_cast<CommitFn>(gCommitHook->m_original)(self, monitor);
}

std::string status() {
    const auto alive = std::count_if(gPrivate.begin(), gPrivate.end(), [](const auto& item) { return !item.expired(); });
    return "{\"held\":" + std::string{gHeld ? "true" : "false"} + ",\"transferred\":" +
        (gTransferred ? "true" : "false") + ",\"private_refs\":" + std::to_string(alive) +
        ",\"snapshot_refused\":" + (gOverflow ? "true" : "false") + ",\"held_commits\":" + std::to_string(gHeldCommits) + "}";
}

std::string command(eHyprCtlOutputFormat, std::string request) {
    if (request == "hv-upgrade status" || request == "hv-upgrade")
        return status();
    if (request == "hv-upgrade release") {
        if (!gTransferred)
            return R"({"error":"privacy must be transferred before release"})";
        gHeld = false;
        return status();
    }
    constexpr std::string_view prefix = "hv-upgrade handoff ";
    if (!request.starts_with(prefix))
        return R"({"error":"usage: hv-upgrade [status|handoff ABSOLUTE_NEW_PLUGIN SHA256|release]"})";
    if (!gHeld || gTransferred)
        return R"({"error":"handoff requires a held, untransferred upgrade"})";
    try {
        const auto arguments = request.substr(prefix.size());
        const auto separator = arguments.rfind(' ');
        if (separator == std::string::npos)
            throw std::runtime_error("upgrade guard: handoff requires an explicit SHA-256 pin");
        const auto path = arguments.substr(0, separator), pin = arguments.substr(separator + 1);
        if (path.find_first_of(" \t\n\r\v\f") != std::string::npos || pin.size() != 64 ||
            pin.find_first_not_of("0123456789abcdef") != std::string::npos)
            throw std::runtime_error("upgrade guard: invalid handoff path/pin");
        if (gOverflow)
            throw std::runtime_error("upgrade guard: privacy snapshot is incomplete");
        if (path == gOldPath || oldPresent())
            throw std::runtime_error("upgrade guard: old module must be unloaded before handoff");
        const auto* next = g_pPluginSystem->getPluginByPath(path);
        if (!next || next->m_name != "hyprveil" || next->m_version != "0.5.0")
            throw std::runtime_error("upgrade guard: exact new module is not loaded");
        const auto [digest, file, btrfs] = ownedDigest(path);
        if (digest != pin)
            throw std::runtime_error("upgrade guard: new module SHA-256 differs from explicit handoff pin");
        const auto* init = dlsym(next->m_handle, PLUGIN_INIT_FUNC_STR);
        const auto* adopt = dlsym(next->m_handle, "HYPRVEIL_ADOPT_V1");
        Dl_info initInfo {}, adoptInfo {};
        if (!init || !adopt || !dladdr(init, &initInfo) || !dladdr(adopt, &adoptInfo) ||
            initInfo.dli_fbase != adoptInfo.dli_fbase || !initInfo.dli_fname || std::string{initInfo.dli_fname} != path)
            throw std::runtime_error("upgrade guard: adoption export module mismatch");
        verifyMapping(adopt, path, file, btrfs);
        snapshot();
        if (gOverflow)
            throw std::runtime_error("upgrade guard: privacy snapshot is incomplete");
        reinterpret_cast<AdoptFn>(const_cast<void*>(adopt))(gPrivate);
        gTransferred = true;
        gCloseListener.reset();
        gOpenListener.reset();
        gRuleListener.reset();
        gRoles.clear();
        return status();
    } catch (...) {
        return R"({"error":"handoff failed; compositor captures remain held"})";
    }
}

void cleanup() {
    gCloseListener.reset();
    gOpenListener.reset();
    gRuleListener.reset();
    gRoles.clear();
    if (gCommand) {
        HyprlandAPI::unregisterHyprCtlCommand(gHandle, gCommand);
        gCommand.reset();
    }
    if (gCommitHook) {
        HyprlandAPI::removeFunctionHook(gHandle, gCommitHook);
        gCommitHook = nullptr;
    }
    gPrivate.clear();
}
} // namespace

APICALL EXPORT std::string PLUGIN_API_VERSION() { return HYPRLAND_API_VERSION; }

APICALL EXPORT PLUGIN_DESCRIPTION_INFO PLUGIN_INIT(HANDLE handle) {
    gHandle = handle;
    Hyprveil::requireReviewedAbi(__hyprland_api_get_hash(), __hyprland_api_get_client_hash());
    admitSession();
    const auto plugins = g_pPluginSystem->getAllPlugins();
    const auto count = std::count_if(plugins.begin(), plugins.end(), [](const auto* item) { return item->m_name == "hyprveil"; });
    if (count != 1)
        throw std::runtime_error("upgrade guard: one exact old Hyprveil must be loaded");
    const auto* old = *std::find_if(plugins.begin(), plugins.end(), [](const auto* item) { return item->m_name == "hyprveil"; });
    const auto [digest, file, btrfs] = ownedDigest(old->m_path);
    const auto pin = std::find_if(OLD_RELEASES.begin(), OLD_RELEASES.end(), [&](const auto& item) { return item.digest == digest; });
    if (pin == OLD_RELEASES.end())
        throw std::runtime_error("upgrade guard: old module SHA-256 does not match migration pin");
    gOldPath = old->m_path;
    gOldHandle = old->m_handle;
    const auto* init = dlsym(gOldHandle, PLUGIN_INIT_FUNC_STR);
    Dl_info module {};
    if (!init || !dladdr(init, &module) || !module.dli_fbase || !module.dli_fname || std::string{module.dli_fname} != gOldPath)
        throw std::runtime_error("upgrade guard: old initialization module mismatch");
    auto* localPrivate = reinterpret_cast<void*>(reinterpret_cast<std::uintptr_t>(module.dli_fbase) + pin->privateWindowVaddr);
    verifyMapping(localPrivate, gOldPath, file, btrfs);
    gOldPrivate = reinterpret_cast<OldPrivateFn>(localPrivate);
    try {
        auto* source = dlsym(RTLD_DEFAULT, COMMIT_SYMBOL);
        const auto* api = dlsym(RTLD_DEFAULT, "__hyprland_api_get_hash");
        Dl_info native {}, nativeApi {};
        if (!source || !api || !dladdr(source, &native) || !dladdr(api, &nativeApi) ||
            native.dli_fbase != nativeApi.dli_fbase || !native.dli_sname || std::string{native.dli_sname} != COMMIT_SYMBOL)
            throw std::runtime_error("upgrade guard: exact commit export mismatch");
        gCommitHook = HyprlandAPI::createFunctionHook(gHandle, source, reinterpret_cast<const void*>(commitHook));
        if (!gCommitHook || !gCommitHook->hook())
            throw std::runtime_error("upgrade guard: cannot hold capture copies");
        snapshot();
        for (const auto& window : Desktop::windowState()->windows())
            watchRole(window);
        gOpenListener = Event::bus()->m_events.window.openEarly.listen([](PHLWINDOW window) { watchRole(window); snapshot(); });
        gCloseListener = Event::bus()->m_events.window.close.listen([](PHLWINDOW) { snapshot(); });
        gRuleListener = Event::bus()->m_events.window.updateRules.listen([](PHLWINDOW) { snapshot(); });
        gCommand = HyprlandAPI::registerHyprCtlCommand(gHandle, {.name = "hv-upgrade", .exact = false, .fn = command});
        if (!gCommand)
            throw std::runtime_error("upgrade guard: command registration failed");
    } catch (...) {
        cleanup();
        throw;
    }
    return {"hyprveil-upgrade-guard", "Temporary pinned migration capture hold and weak privacy handoff", "OBJLAKO", "1.0.0"};
}

// The orchestrator must never unload this guard before successful handoff.
// A compositor shutdown or explicit out-of-band forced unload always releases
// native resources; destructors cannot leave callbacks in an unloaded DSO.
APICALL EXPORT void PLUGIN_EXIT() { cleanup(); }
