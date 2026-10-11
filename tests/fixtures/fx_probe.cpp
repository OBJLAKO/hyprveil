// Lab-only observation of native transformers; never installed on the desktop.
#include <src/plugins/PluginAPI.hpp>
#include <src/desktop/state/WindowState.hpp>
#include <src/desktop/view/Window.hpp>
#include <src/render/Renderer.hpp>
#include <src/render/OpenGL.hpp>
#include <src/output/MonitorResources.hpp>
#include <src/render/pass/PassElement.hpp>
#include <algorithm>
#include <array>
#include <cstdlib>
#include <dlfcn.h>
#include <string>
#include <stdexcept>
#include <sys/stat.h>
#include <unistd.h>

namespace {
HANDLE handle = nullptr;
SP<SHyprCtlCommand> command;
Render::IWindowTransformer* unknown = nullptr;
PHLWINDOWREF unknownWindow;
unsigned unknownCaptureCalls = 0, unknownTotalCalls = 0;
CFunctionHook* workspaceHook = nullptr;
bool mirrorArmed = false, mirrorSeeded = false, mirrorChecked = false, mirrorUnchanged = false;
unsigned mirrorChangedPixels = 0;
std::string mirrorError;
SP<Render::ITexture> sentinel;
WP<Render::IFramebuffer> seededBuffer;
constexpr std::array<unsigned char, 4> SENTINEL = {41, 87, 173, 255};
constexpr const char* SENTINEL_PASS = "hyprveil lab synthetic mirror sentinel";

// The sole readback source is this fixture's newly allocated solid texture.
// Run after the queued window passes, before any later local frame can reuse
// the pool. Holding only a weak FB reference is essential to the regression.
class CheckMirror final : public IPassElement {
  public:
    bool needsLiveBlur() override { return false; }
    bool needsPrecomputeBlur() override { return false; }
    bool disableSimplification() override { return true; }
    const char* passName() override { return SENTINEL_PASS; }
    ePassElementType type() override { return EK_CUSTOM; }
    std::vector<UP<IPassElement>> draw() override {
        mirrorChecked = true;
        GLint readFB = 0, pack = 0, rowLength = 0, skipPixels = 0, skipRows = 0;
        glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &readFB);
        glGetIntegerv(GL_PACK_ALIGNMENT, &pack);
        glGetIntegerv(GL_PACK_ROW_LENGTH, &rowLength);
        glGetIntegerv(GL_PACK_SKIP_PIXELS, &skipPixels);
        glGetIntegerv(GL_PACK_SKIP_ROWS, &skipRows);
        GLuint reader = 0;
        glGenFramebuffers(1, &reader);
        glBindFramebuffer(GL_READ_FRAMEBUFFER, reader);
        if (sentinel) glFramebufferTexture2D(GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, sentinel->m_texID, 0);
        if (sentinel && glCheckFramebufferStatus(GL_READ_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE) {
            const auto width = static_cast<int>(sentinel->m_size.x), height = static_cast<int>(sentinel->m_size.y);
            std::vector<unsigned char> pixels(static_cast<size_t>(width) * height * 4);
            glPixelStorei(GL_PACK_ALIGNMENT, 1);
            glPixelStorei(GL_PACK_ROW_LENGTH, 0);
            glPixelStorei(GL_PACK_SKIP_PIXELS, 0);
            glPixelStorei(GL_PACK_SKIP_ROWS, 0);
            glReadBuffer(GL_COLOR_ATTACHMENT0);
            glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, pixels.data());
            for (size_t index = 0; index < pixels.size(); index += 4)
                if (!std::equal(SENTINEL.begin(), SENTINEL.end(), pixels.begin() + index)) ++mirrorChangedPixels;
            mirrorUnchanged = mirrorChangedPixels == 0;
        } else mirrorError = "synthetic sentinel read framebuffer incomplete";
        if (const auto buffer = seededBuffer.lock()) buffer->disableMirror();
        seededBuffer.reset();
        sentinel.reset();
        glBindFramebuffer(GL_READ_FRAMEBUFFER, readFB);
        glPixelStorei(GL_PACK_ALIGNMENT, pack);
        glPixelStorei(GL_PACK_ROW_LENGTH, rowLength);
        glPixelStorei(GL_PACK_SKIP_PIXELS, skipPixels);
        glPixelStorei(GL_PACK_SKIP_ROWS, skipRows);
        glDeleteFramebuffers(1, &reader);
        return {};
    }
};

using WorkspaceFn = void (*)(Render::IHyprRenderer*, PHLMONITOR, PHLWORKSPACE, const Time::steady_tp&, const CBox&);
void renderWorkspace(Render::IHyprRenderer* renderer, PHLMONITOR monitor, PHLWORKSPACE workspace,
                     const Time::steady_tp& now, const CBox& box) {
    const bool seed = mirrorArmed && renderer->m_bBlockSurfaceFeedback;
    if (seed) {
        mirrorArmed = false;
        const auto saved = renderer->m_renderData;
        GLint readFB = 0;
        const bool scissor = glIsEnabled(GL_SCISSOR_TEST);
        glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &readFB);
        try {
            if (!monitor || monitor->m_pixelSize.x <= 0 || monitor->m_pixelSize.y <= 0 ||
                monitor->m_pixelSize.x > 2048 || monitor->m_pixelSize.y > 2048)
                throw std::runtime_error("synthetic sentinel size refused");
            auto buffer = monitor->resources()->getUnusedWorkBuffer();
            if (!buffer || buffer->getMirrorTexture()) throw std::runtime_error("first unused synthetic work buffer already mirrored");
            sentinel = renderer->createTexture();
            sentinel->allocate(monitor->m_pixelSize, DRM_FORMAT_ABGR8888);
            if (!sentinel->ok()) throw std::runtime_error("synthetic sentinel allocation failed");
            buffer->enableMirror(sentinel);
            buffer->bind();
            glDisable(GL_SCISSOR_TEST);
            const std::array<float, 4> color = {SENTINEL[0]/255.F, SENTINEL[1]/255.F, SENTINEL[2]/255.F, 1.F};
            glClearBufferfv(GL_COLOR, 0, color.data());
            glClearBufferfv(GL_COLOR, 1, color.data());
            seededBuffer = buffer;
            mirrorSeeded = true;
            // buffer's strong reference ends here, before the production
            // guard enumerates unused mirror-attached pool entries.
        } catch (const std::exception& error) { mirrorError = error.what(); }
        renderer->m_renderData = saved;
        renderer->bindFB(saved.currentFB);
        renderer->setViewport(0, 0, saved.fbSize.x, saved.fbSize.y);
        glBindFramebuffer(GL_READ_FRAMEBUFFER, readFB);
        if (scissor) glEnable(GL_SCISSOR_TEST);
    }
    reinterpret_cast<WorkspaceFn>(workspaceHook->m_original)(renderer, monitor, workspace, now, box);
    if (seed && mirrorSeeded) renderer->addPassElement(makeUnique<CheckMirror>());
}
// Deliberately outside the reviewed OmaFX module. Returning the supplied
// framebuffer is harmless, but the production admission gate must still
// refuse this mixed chain in a private export.
class UnknownIdentity final : public Render::IWindowTransformer {
  public:
    ~UnknownIdentity() override { if (unknown == this) unknown = nullptr; }
    SP<Render::IFramebuffer> transform(SP<Render::IFramebuffer> input) override {
        ++unknownTotalCalls;
        if (g_pHyprRenderer && g_pHyprRenderer->m_bBlockSurfaceFeedback) ++unknownCaptureCalls;
        return input;
    }
};
void detachUnknown() {
    if (const auto window = unknownWindow.lock(); window && unknown) {
        const auto target = unknown;
        std::erase_if(window->m_transformers, [target](const auto& transformer) { return transformer.get() == target; });
        g_pHyprRenderer->damageWindow(window, true);
    }
    unknown = nullptr;
    unknownWindow.reset();
}
std::string observe(eHyprCtlOutputFormat, std::string request) {
    if (request == "hyprveil-fx-probe attach-unknown") {
        if (unknown) return "err unknown transformer already attached";
        PHLWINDOW fixture;
        for (const auto& window : Desktop::windowState()->windows()) {
            if (window->m_class != "org.hyprveil.fixture.protected") continue;
            if (fixture) return "err ambiguous protected fixture";
            fixture = window;
        }
        if (!fixture) return "err protected fixture missing";
        auto transformer = makeUnique<UnknownIdentity>();
        unknown = transformer.get();
        unknownWindow = fixture;
        unknownCaptureCalls = unknownTotalCalls = 0;
        fixture->m_transformers.emplace_back(std::move(transformer));
        g_pHyprRenderer->damageWindow(fixture, true);
    } else if (request == "hyprveil-fx-probe detach-unknown") detachUnknown();
    else if (request == "hyprveil-fx-probe arm-mirror") {
        if (mirrorArmed || sentinel) return "err synthetic mirror already armed";
        mirrorArmed = true;
        mirrorSeeded = mirrorChecked = mirrorUnchanged = false;
        mirrorChangedPixels = 0;
        mirrorError.clear();
    }
    else if (request != "hyprveil-fx-probe") return "err unknown probe request";
    unsigned privateCount = 0, publicCount = 0;
    for (const auto& window : Desktop::windowState()->windows()) {
        if (window->m_class == "org.hyprveil.fixture.protected")
            privateCount += window->m_transformers.size();
        if (window->m_class == "org.hyprveil.fixture.foreground")
            publicCount += window->m_transformers.size();
    }
    return "{\"private_transformers\":" + std::to_string(privateCount) +
        ",\"public_transformers\":" + std::to_string(publicCount) +
        ",\"unknown_attached\":" + (unknown ? "true" : "false") +
        ",\"unknown_capture_calls\":" + std::to_string(unknownCaptureCalls) +
        ",\"unknown_total_calls\":" + std::to_string(unknownTotalCalls) +
        ",\"mirror_armed\":" + (mirrorArmed ? "true" : "false") +
        ",\"mirror_seeded\":" + (mirrorSeeded ? "true" : "false") +
        ",\"mirror_checked\":" + (mirrorChecked ? "true" : "false") +
        ",\"mirror_unchanged\":" + (mirrorUnchanged ? "true" : "false") +
        ",\"mirror_changed_pixels\":" + std::to_string(mirrorChangedPixels) +
        ",\"mirror_error\":\"" + mirrorError + "\"}";
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
    constexpr const char* symbol = "_ZN6Render13IHyprRenderer15renderWorkspaceEN9Hyprutils6Memory14CSharedPointerIN7Monitor8CMonitorEEENS3_I10CWorkspaceEERKNSt6chrono10time_pointINS9_3_V212steady_clockENS9_8durationIlSt5ratioILl1ELl1000000000EEEEEERKNS1_4Math4CBoxE";
    auto* source = dlsym(RTLD_DEFAULT, symbol);
    Dl_info target{}, api{};
    if (!source || !dladdr(source, &target) || !dladdr(dlsym(RTLD_DEFAULT, "__hyprland_api_get_hash"), &api) ||
        target.dli_fbase != api.dli_fbase || !target.dli_sname || std::string{target.dli_sname} != symbol)
        throw std::runtime_error("fx probe: exact workspace hook identity refused");
    workspaceHook = HyprlandAPI::createFunctionHook(handle, source, reinterpret_cast<void*>(renderWorkspace));
    if (!workspaceHook || !workspaceHook->hook()) throw std::runtime_error("fx probe: workspace hook failed");
    command = HyprlandAPI::registerHyprCtlCommand(handle, {.name="hyprveil-fx-probe", .exact=false, .fn=observe});
    if (!command) throw std::runtime_error("fx probe: command registration failed");
    return {"hyprveil-fx-probe", "Synthetic transformer observer", "hyprveil tests", "0.1"};
}
APICALL EXPORT void PLUGIN_EXIT() {
    detachUnknown();
    if (g_pHyprRenderer) g_pHyprRenderer->m_renderPass.removeAllOfType(SENTINEL_PASS);
    mirrorArmed = false;
    if (const auto buffer = seededBuffer.lock()) buffer->disableMirror();
    seededBuffer.reset();
    sentinel.reset();
    if (workspaceHook) HyprlandAPI::removeFunctionHook(handle, workspaceHook);
    workspaceHook = nullptr;
    if (command) HyprlandAPI::unregisterHyprCtlCommand(handle, command);
    command.reset();
}
