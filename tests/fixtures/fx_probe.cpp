// Lab-only observation of native transformers; never installed on the desktop.
#include <src/plugins/PluginAPI.hpp>
#include <src/desktop/state/WindowState.hpp>
#include <src/desktop/view/Window.hpp>
#include <cstdlib>
#include <string>
#include <stdexcept>
#include <sys/stat.h>
#include <unistd.h>

namespace {
HANDLE handle = nullptr;
SP<SHyprCtlCommand> command;
std::string observe(eHyprCtlOutputFormat, std::string) {
    unsigned privateCount = 0, publicCount = 0;
    for (const auto& window : Desktop::windowState()->windows()) {
        if (window->m_class == "org.hyprveil.fixture.protected")
            privateCount += window->m_transformers.size();
        if (window->m_class == "org.hyprveil.fixture.foreground")
            publicCount += window->m_transformers.size();
    }
    return "{\"private_transformers\":" + std::to_string(privateCount) +
        ",\"public_transformers\":" + std::to_string(publicCount) + "}";
}
}
APICALL EXPORT std::string PLUGIN_API_VERSION() { return HYPRLAND_API_VERSION; }
APICALL EXPORT PLUGIN_DESCRIPTION_INFO PLUGIN_INIT(HANDLE value) {
    if (std::string{__hyprland_api_get_hash()} != __hyprland_api_get_client_hash())
        throw std::runtime_error("fx probe: ABI mismatch");
    const auto* runtime = std::getenv("XDG_RUNTIME_DIR");
    const auto* lab = std::getenv("HYPRVEIL_LAB_RUNTIME");
    const auto* seat = std::getenv("LIBSEAT_BACKEND");
    struct stat info {}, marker {};
    if (!runtime || !lab || !seat || std::string{runtime} != lab ||
        std::string{seat} != "hyprveil-disabled" || lstat(runtime, &info) != 0 ||
        !S_ISDIR(info.st_mode) || info.st_uid != getuid() || (info.st_mode & 0777) != 0700 ||
        lstat((std::string{runtime} + "/.hyprveil-lab").c_str(), &marker) != 0 ||
        !S_ISREG(marker.st_mode) || marker.st_uid != getuid())
        throw std::runtime_error("fx probe: isolated lab required");
    handle = value;
    command = HyprlandAPI::registerHyprCtlCommand(handle, {.name="hyprveil-fx-probe", .exact=true, .fn=observe});
    if (!command) throw std::runtime_error("fx probe: command registration failed");
    return {"hyprveil-fx-probe", "Synthetic transformer observer", "hyprveil tests", "0.1"};
}
APICALL EXPORT void PLUGIN_EXIT() {
    if (command) HyprlandAPI::unregisterHyprCtlCommand(handle, command);
    command.reset();
}
