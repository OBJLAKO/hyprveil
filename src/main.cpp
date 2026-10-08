// Hyprveil proof of concept: rebuild the capture scene without private views.
// This file intentionally uses Hyprland internals and must be rebuilt for the
// exact running compositor ABI. Only explicit public privacy actions change
// native window flags; rendering and style configuration never do.
#include <algorithm>
#include <array>
#include <cerrno>
#include <climits>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <utility>
#include <unordered_map>
#include <vector>
#include <cairo/cairo.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <src/plugins/PluginAPI.hpp>
#include <src/Compositor.hpp>
#include <src/managers/screenshare/ScreenshareManager.hpp>
#include <src/desktop/view/Window.hpp>
#include <src/desktop/Workspace.hpp>
#include <src/render/Renderer.hpp>
#include <src/render/OpenGL.hpp>
#include <src/render/Shader.hpp>
#include <src/state/MonitorState.hpp>
#include <src/pointer/PointerManager.hpp>
#include <src/managers/input/InputManager.hpp>
#include <src/managers/SessionLockManager.hpp>
#include <src/event/EventBus.hpp>
#include <src/managers/eventLoop/EventLoopManager.hpp>
#include <src/config/shared/actions/ConfigActions.hpp>
#include <src/config/lua/ConfigManager.hpp>
#include <src/config/lua/types/LuaConfigString.hpp>
#include <src/config/lua/types/LuaConfigInt.hpp>
#include <src/config/lua/types/LuaConfigBool.hpp>
#include <src/config/values/types/StringValue.hpp>
#include <src/config/values/types/IntValue.hpp>
#include <src/config/values/types/BoolValue.hpp>
#include <src/desktop/state/FocusState.hpp>
#include <src/protocols/XDGShell.hpp>
#include <src/xwayland/XWM.hpp>

#include "SessionGuard.hpp"
#include "PngGuard.hpp"
#include "PrivacyPolicy.hpp"
#include "SpoilerPattern.hpp"
#include "Appearance.hpp"
#include "NativeConfig.hpp"

namespace {
using Frame = Screenshare::CScreenshareFrame;
using Session = Screenshare::CScreenshareSession;
using Renderer = Render::IHyprRenderer;

// Explicit instantiation permits taking pointers to private/protected members.
// Keep these accessors isolated: they are the version-specific bridge, not an
// extension of the supported Hyprland Plugin API.
template <class Tag, typename Tag::Type Member>
struct InternalMember {
    friend typename Tag::Type member(Tag) { return Member; }
};

struct FrameSession {
    using Type = WP<Session> Frame::*;
    friend Type member(FrameSession);
};
template struct InternalMember<FrameSession, &Frame::m_session>;

struct CaptureBox {
    using Type = CBox Session::*;
    friend Type member(CaptureBox);
};
template struct InternalMember<CaptureBox, &Session::m_captureBox>;

struct SessionType {
    using Type = Screenshare::eScreenshareType Session::*;
    friend Type member(SessionType);
};
template struct InternalMember<SessionType, &Session::m_type>;

struct SessionWindow {
    using Type = PHLWINDOWREF Session::*;
    friend Type member(SessionWindow);
};
template struct InternalMember<SessionWindow, &Session::m_window>;

struct PermissionSnapshot {
    using Type = SP<Render::IFramebuffer> Session::*;
    friend Type member(PermissionSnapshot);
};
template struct InternalMember<PermissionSnapshot, &Session::m_tempFB>;

struct OverlayCursor {
    using Type = bool Frame::*;
    friend Type member(OverlayCursor);
};
template struct InternalMember<OverlayCursor, &Frame::m_overlayCursor>;

struct RenderWorkspace {
    using Type = void (Renderer::*)(PHLMONITOR, PHLWORKSPACE, const Time::steady_tp&, const CBox&);
    friend Type member(RenderWorkspace);
};
template struct InternalMember<RenderWorkspace, &Renderer::renderWorkspace>;

struct ReadX11Property {
    using Type = void (CXWM::*)(SP<CXWaylandSurface>, uint32_t, xcb_get_property_reply_t*);
    friend Type member(ReadX11Property);
};
template struct InternalMember<ReadX11Property, &CXWM::readProp>;

struct ShaderProgram {
    using Type = GLuint CShader::*;
    friend Type member(ShaderProgram);
};
template struct InternalMember<ShaderProgram, &CShader::m_program>;
struct ShaderLocations {
    using Type = std::array<GLint, SHADER_LAST> CShader::*;
    friend Type member(ShaderLocations);
};
template struct InternalMember<ShaderLocations, &CShader::m_uniformLocations>;

HANDLE gHandle = nullptr;
bool gEnabled = true;
bool gCaptureScene = false;
bool gDumpPending = false;
bool gLiveSession = false;
std::string gSession = "uninitialized";
std::string gLabRuntime;
std::string gDumpResult;
std::string gMode = "black";
std::string gImagePath;
std::string gLoadedImagePath;
std::string gImageStatus = "not-loaded";
SP<Render::ITexture> gImage;
Hyprveil::Appearance gAppearance;
bool gConfigReady = false;
bool gConfigBatch = false;
bool gModeDispatcher = false, gCycleDispatcher = false;
bool gLuaConfigure = false, gLuaStatus = false, gLuaMode = false, gLuaCycle = false;
bool gLuaActivePrivacy = false, gLuaSetHidden = false, gLuaToggle = false, gLuaResetSharing = false;
lua_State* gConfigWriter = nullptr;
std::vector<std::string> gRegisteredConfig;
SP<CShader> gSpoilerShader;
SP<Render::ITexture> gSpoilerEye;

std::string gSpoilerStatus = "not-loaded";
std::uint64_t gSpoilerShaderAttempts = 0;
SP<CEventLoopTimer> gSpoilerTimer;
struct AnimatedCapture {
    PHLMONITORREF monitor;
    Time::steady_tp captured;
};
std::vector<AnimatedCapture> gAnimatedCaptures;
std::uint64_t gSceneFrames = 0;
std::uint64_t gFallbackFrames = 0;
std::uint64_t gOmittedWindowPasses = 0;
std::uint64_t gOmittedLayerPasses = 0;
std::uint64_t gPolicyGeneration = 1;
std::uint64_t gInvalidatedSnapshots = 0;
struct SessionPolicy {
    WP<Session> session;
    std::uint64_t generation = 0;
};
std::unordered_map<const Session*, SessionPolicy> gSessionPolicies;
// Only a closing private ancestor creates retention. Weak identity never
// keeps a window alive or transfers its privacy to a reused pointer address.
std::unordered_map<const Desktop::View::CWindow*, PHLWINDOWREF> gOrphanPrivacy;
// This is bookkeeping for our explicit native SetProp sharing choices, not an
// additional privacy policy. Capture always reads the native window property.
struct SharingChoice {
    PHLWINDOWREF window;
    std::optional<bool> priorSetProp;
};
constexpr std::size_t SHARING_LIMIT = 4096;
std::unordered_map<const Desktop::View::CWindow*, SharingChoice> gSharingChoices;
bool gResettingSharing = false;
const Desktop::View::CWindow* gSharingPublication = nullptr;
struct RoleWatch {
    PHLWINDOWREF window;
    WP<CXDGToplevelResource> role;
    CHyprSignalListener destroyed;
};
std::unordered_map<const Desktop::View::CWindow*, RoleWatch> gRoleWatches;
struct ParentDiagnostic {
    std::uintptr_t self = 0, argument = 0, window = 0, argumentWindow = 0, beforeParent = 0, afterParent = 0;
    std::uintptr_t afterImpl = 0, afterData = 0;
    bool beforePrivate = false, afterPrivate = false;
};
std::vector<ParentDiagnostic> gParentDiagnostics;
std::vector<CFunctionHook*> gHooks;
SP<SHyprCtlCommand> gCommand;
CHyprSignalListener gWindowRulesListener;
CHyprSignalListener gWindowCloseListener;
CHyprSignalListener gWindowOpenListener;
CHyprSignalListener gLayerRulesListener;
CHyprSignalListener gConfigReloadListener;
CHyprSignalListener gConfigPreReloadListener;
CHyprSignalListener gLockListener;
CHyprSignalListener gUnlockListener;

// A mask is decoded synchronously by the compositor. Bound both the encoded
// input and its declared allocation before handing any bytes to Cairo.
constexpr std::size_t MAX_PNG_BYTES = Hyprveil::Png::MAX_BYTES;
constexpr std::uint64_t MAX_PNG_PIXELS = Hyprveil::Png::MAX_PIXELS;
constexpr std::uint32_t MAX_PNG_AXIS = Hyprveil::Png::MAX_AXIS;

std::vector<unsigned char> readBoundedPng(const std::string& path) {
    // The path may have changed since the command checked it. O_NONBLOCK makes
    // a replacement FIFO harmless; fstat checks the actual opened object.
    const int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0)
        return {};
    Hyprutils::Utils::CScopeGuard closeFile([fd]() { close(fd); });
    struct stat info {};
    if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_size < 33 ||
        static_cast<std::uint64_t>(info.st_size) > MAX_PNG_BYTES)
        return {};

    std::vector<unsigned char> bytes(static_cast<std::size_t>(info.st_size));
    std::size_t offset = 0;
    while (offset < bytes.size()) {
        const auto count = read(fd, bytes.data() + offset, bytes.size() - offset);
        if (count < 0 && errno == EINTR)
            continue;
        if (count <= 0)
            return {};
        offset += static_cast<std::size_t>(count);
    }
    // Reject a file that grew while being read rather than trusting its old
    // size or leaving the decoder to reopen an unbounded path.
    unsigned char trailing;
    ssize_t extra;
    do { extra = read(fd, &trailing, 1); } while (extra < 0 && errno == EINTR);
    if (extra != 0)
        return {};

    return Hyprveil::Png::pixelOnly(bytes);
}

struct PngStream {
    const std::vector<unsigned char>& bytes;
    std::size_t offset = 0;
};

cairo_status_t readPngStream(void* closure, unsigned char* output, unsigned int length) {
    auto& stream = *static_cast<PngStream*>(closure);
    if (length > stream.bytes.size() - stream.offset)
        return CAIRO_STATUS_READ_ERROR;
    std::copy_n(stream.bytes.data() + stream.offset, length, output);
    stream.offset += length;
    return CAIRO_STATUS_SUCCESS;
}

CFunctionHook* gMonitorHook = nullptr;
CFunctionHook* gWindowHook = nullptr;
CFunctionHook* gLayerHook = nullptr;
CFunctionHook* gFadeoutHook = nullptr;
CFunctionHook* gPassHook = nullptr;
CFunctionHook* gDragHook = nullptr;
CFunctionHook* gCopyHook = nullptr;
CFunctionHook* gSetPropHook = nullptr;
CFunctionHook* gFrameRenderHook = nullptr;
CFunctionHook* gParentHook = nullptr;
CFunctionHook* gX11PropertyHook = nullptr;

void capturePolicyChanged();

void animateCapture(PHLMONITOR monitor, const Time::steady_tp& now) {
    auto found = std::find_if(gAnimatedCaptures.begin(), gAnimatedCaptures.end(),
                             [&](const auto& item) { return item.monitor.get() == monitor.get(); });
    if (found == gAnimatedCaptures.end())
        gAnimatedCaptures.push_back({monitor, now});
    else
        found->captured = now;
    if (gSpoilerTimer && !gSpoilerTimer->armed())
        gSpoilerTimer->updateTimeout(std::chrono::milliseconds{Hyprveil::Spoiler::FRAME_MS});
}

// Custom pass holds no original pixels. Native black is queued beneath it.
// Its objects are explicitly removed before plugin code/vtables can unload.
class SatinPass final : public IPassElement {
  public:
    SatinPass(CBox box, double scale, float time, SP<CShader> shader, Hyprveil::Appearance appearance)
        : m_box(box), m_scale(scale), m_time(time), m_shader(std::move(shader)), m_appearance(std::move(appearance)) {}
    bool needsLiveBlur() override { return false; }
    bool needsPrecomputeBlur() override { return false; }
    const char* passName() override { return Hyprveil::Spoiler::PASS_NAME; }
    ePassElementType type() override { return EK_CUSTOM; }
    std::optional<CBox> boundingBox() override { return m_box.copy().scale(1.0 / m_scale); }
    CRegion opaqueRegion() override { return {}; }
    std::vector<UP<IPassElement>> draw() override {
        if (!Render::GL::g_pHyprOpenGL || !g_pHyprRenderer || !m_shader || !m_shader->program())
            return {};
        auto& data = g_pHyprRenderer->m_renderData;
        CBox box = m_box;
        data.renderModif.applyToBox(box);
        CRegion damage{data.damage};
        damage.intersect(box);
        if (data.clipBox.w > 0 && data.clipBox.h > 0)
            damage.intersect(data.clipBox);
        if (damage.empty())
            return {};
        auto* gl = Render::GL::g_pHyprOpenGL.get();
        auto shader = gl->useShader(m_shader);
        shader->setUniformMatrix3fv(SHADER_PROJ, 1, GL_TRUE, g_pHyprRenderer->projectBoxToTarget(box).getMatrix());
        shader->setUniformFloat2(SHADER_FULL_SIZE, box.w, box.h);
        shader->setUniformFloat(SHADER_TIME, m_time * m_appearance.speed / 100.F);
        const auto tint = m_appearance.tint();
        shader->setUniformFloat3(SHADER_TINT, tint[0], tint[1], tint[2]);
        shader->setUniformFloat(SHADER_NOISE, m_appearance.grain / 100.F);
        shader->setUniformFloat(SHADER_BRIGHTNESS, m_appearance.darkness / 100.F);
        shader->setUniformInt(SHADER_TEX_TYPE, m_appearance.variant == "telegram" ? 1 : 0);
        glBindVertexArray(shader->getUniformLocation(SHADER_SHADER_VAO));
        damage.forEachRect([&](const auto& rect) {
            gl->scissor(&rect, data.transformDamage);
            glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
        });
        glBindVertexArray(0);
        gl->scissor(nullptr);
        // useShader owns the program cache; following native passes switch it.
        return {};
    }
  private:
    CBox m_box;
    double m_scale;
    float m_time;
    SP<CShader> m_shader;
    Hyprveil::Appearance m_appearance;
};

SP<CShader> compileSpoilerShader(const char* fragment) {
    // Native static compilation asserts and dynamic compilation leaks stage
    // objects on early rejection in this ABI. Own every GL allocation until
    // a fully checked program/VAO/VBO can transfer to the native destructor.
    GLuint vertex = 0, pixel = 0, program = 0, vao = 0, vbo = 0;
    Hyprutils::Utils::CScopeGuard release([&]() {
        glBindVertexArray(0);
        glBindBuffer(GL_ARRAY_BUFFER, 0);
        if (vertex) glDeleteShader(vertex);
        if (pixel) glDeleteShader(pixel);
        if (vao) glDeleteVertexArrays(1, &vao);
        if (vbo) glDeleteBuffers(1, &vbo);
        if (program) glDeleteProgram(program);
    });
    const auto compile = [](GLuint& id, GLenum type, const char* source) {
        id = glCreateShader(type);
        if (!id) return false;
        glShaderSource(id, 1, &source, nullptr);
        glCompileShader(id);
        GLint success = GL_FALSE;
        glGetShaderiv(id, GL_COMPILE_STATUS, &success);
        return success == GL_TRUE;
    };
    if (!compile(vertex, GL_VERTEX_SHADER, Hyprveil::Spoiler::VERTEX) || !compile(pixel, GL_FRAGMENT_SHADER, fragment))
        return {};
    program = glCreateProgram();
    if (!program) return {};
    glAttachShader(program, vertex);
    glAttachShader(program, pixel);
    glLinkProgram(program);
    GLint linked = GL_FALSE;
    glGetProgramiv(program, GL_LINK_STATUS, &linked);
    if (linked != GL_TRUE) return {};
    glDetachShader(program, vertex);
    glDetachShader(program, pixel);
    glDeleteShader(vertex); vertex = 0;
    glDeleteShader(pixel); pixel = 0;
    auto shader = makeShared<CShader>();
    auto& locations = shader.get()->*member(ShaderLocations{});
    locations[SHADER_PROJ] = glGetUniformLocation(program, "proj");
    locations[SHADER_FULL_SIZE] = glGetUniformLocation(program, "fullSize");
    locations[SHADER_TIME] = glGetUniformLocation(program, "time");
    locations[SHADER_POS_ATTRIB] = glGetAttribLocation(program, "pos");
    locations[SHADER_TEX_ATTRIB] = glGetAttribLocation(program, "texcoord");
    locations[SHADER_TINT] = glGetUniformLocation(program, "tint");
    locations[SHADER_NOISE] = glGetUniformLocation(program, "grainAmount");
    locations[SHADER_BRIGHTNESS] = glGetUniformLocation(program, "darkness");
    locations[SHADER_TEX_TYPE] = glGetUniformLocation(program, "variant");
    if (locations[SHADER_PROJ] < 0 || locations[SHADER_FULL_SIZE] < 0 || locations[SHADER_TIME] < 0 ||
        locations[SHADER_POS_ATTRIB] < 0 || locations[SHADER_TEX_ATTRIB] < 0 ||
        locations[SHADER_TINT] < 0 || locations[SHADER_NOISE] < 0 || locations[SHADER_BRIGHTNESS] < 0 || locations[SHADER_TEX_TYPE] < 0)
        return {};
    glGenVertexArrays(1, &vao);
    glGenBuffers(1, &vbo);
    if (!vao || !vbo || vao > INT_MAX || vbo > INT_MAX) return {};
    glBindVertexArray(vao);
    glBindBuffer(GL_ARRAY_BUFFER, vbo);
    glBufferData(GL_ARRAY_BUFFER, sizeof(Render::GL::fullVerts), Render::GL::fullVerts.data(), GL_STATIC_DRAW);
    GLint bufferSize = 0;
    glGetBufferParameteriv(GL_ARRAY_BUFFER, GL_BUFFER_SIZE, &bufferSize);
    if (bufferSize != static_cast<GLint>(sizeof(Render::GL::fullVerts))) return {};
    glEnableVertexAttribArray(locations[SHADER_POS_ATTRIB]);
    glVertexAttribPointer(locations[SHADER_POS_ATTRIB], 2, GL_FLOAT, GL_FALSE, sizeof(Render::GL::SVertex),
                          reinterpret_cast<void*>(offsetof(Render::GL::SVertex, x)));
    glEnableVertexAttribArray(locations[SHADER_TEX_ATTRIB]);
    glVertexAttribPointer(locations[SHADER_TEX_ATTRIB], 2, GL_FLOAT, GL_FALSE, sizeof(Render::GL::SVertex),
                          reinterpret_cast<void*>(offsetof(Render::GL::SVertex, u)));
    locations[SHADER_SHADER_VAO] = static_cast<GLint>(vao);
    locations[SHADER_SHADER_VBO] = static_cast<GLint>(vbo);
    shader.get()->*member(ShaderProgram{}) = program;
    program = vao = vbo = 0; // all ownership now belongs to CShader
    return shader;
}

void prepareSpoiler(Renderer* renderer) {
    if (gSpoilerStatus == "black-fallback")
        return; // latch rejection for this plugin lifetime; never retry per frame
    if (gSpoilerShader && gSpoilerShader->program() && gSpoilerEye && gSpoilerEye->ok())
        return;
    try {
        if (renderer->type() != Renderer::RT_GL || !Render::GL::g_pHyprOpenGL)
            throw std::runtime_error("synthetic shader requires GL");
        ++gSpoilerShaderAttempts;
        const char* fragment = Hyprveil::Spoiler::FRAGMENT;
        const auto* fault = std::getenv("HYPRVEIL_LAB_SPOILER_SHADER_FAILURE");
        if (!gLiveSession && fault && std::string{fault} == "1")
            fragment = "#version 300 es\ninvalid synthetic lab shader\n";
        auto shader = compileSpoilerShader(fragment);
        if (!shader)
            throw std::runtime_error("synthetic shader unavailable");
        auto* eye = Hyprveil::Spoiler::eye();
        if (!eye)
            throw std::runtime_error("synthetic eye unavailable");
        Hyprutils::Utils::CScopeGuard destroyEye([eye]() { cairo_surface_destroy(eye); });
        auto texture = renderer->createTexture(eye);
        if (!texture || !texture->ok())
            throw std::runtime_error("synthetic eye texture unavailable");
        texture->m_imageDescription = NColorManagement::DEFAULT_SRGB_IMAGE_DESCRIPTION;
        gSpoilerShader = shader;
        gSpoilerEye = texture;
        gSpoilerStatus = "ready";
    } catch (...) {
        gSpoilerShader.reset();
        gSpoilerEye.reset();
        gSpoilerStatus = "black-fallback";
    }
}

void requireLab() {
    const auto* runtime = std::getenv("XDG_RUNTIME_DIR");
    const auto* lab = std::getenv("HYPRVEIL_LAB_RUNTIME");
    const auto* seat = std::getenv("LIBSEAT_BACKEND");
    std::error_code error;
    if (!runtime || !lab || std::string{runtime} != lab || !seat || std::string{seat} != "hyprveil-disabled" ||
        !std::filesystem::is_regular_file(std::filesystem::path{runtime} / ".hyprveil-lab", error))
        throw std::runtime_error("hyprveil prototype refuses this session: a marked, isolated lab runtime is required");
    struct stat runtimeInfo {}, markerInfo {};
    const auto marker = (std::filesystem::path{runtime} / ".hyprveil-lab").string();
    if (lstat(runtime, &runtimeInfo) != 0 || !S_ISDIR(runtimeInfo.st_mode) || runtimeInfo.st_uid != getuid() ||
        (runtimeInfo.st_mode & 0777) != 0700 || lstat(marker.c_str(), &markerInfo) != 0 || !S_ISREG(markerInfo.st_mode) || markerInfo.st_uid != getuid())
        throw std::runtime_error("hyprveil prototype requires an owned 0700 lab directory and owned regular marker");
    gLabRuntime = runtime;
}

void requireSession() {
    const auto* runtime = std::getenv("XDG_RUNTIME_DIR");
    const auto* lab = std::getenv("HYPRVEIL_LAB_RUNTIME");
    const auto* seat = std::getenv("LIBSEAT_BACKEND");
    if (runtime && lab && std::string{runtime} == lab && seat && std::string{seat} == "hyprveil-disabled") {
        requireLab();
        gLiveSession = false;
        gSession = "lab";
        return;
    }
    const auto now = std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
    Hyprveil::requireLiveMarker(runtime ? runtime : "", getpid(), g_pCompositor->m_instanceSignature, static_cast<std::uint64_t>(now));
    gLiveSession = true;
    gSession = "live";
    gLabRuntime.clear();
    gDumpPending = false;
    gDumpResult = "disabled-in-live";
}

void dumpLocalMirror(PHLMONITOR monitor) {
    // The local mirror contains private content. Live trials never read it or
    // write diagnostic captures, even if a pending flag were set accidentally.
    if (gLiveSession) {
        gDumpPending = false;
        return;
    }
    if (!gDumpPending)
        return;
    gDumpPending = false;
    // Available only in requireLab() sessions. This is a diagnostic snapshot of the
    // synthetic nested compositor, never a capture of the host desktop.
    auto texture = monitor->resources()->getMirrorTexture();
    if (!texture || !texture->m_texID || g_pHyprRenderer->type() != Renderer::RT_GL) {
        gDumpResult = "mirror-unavailable";
        return;
    }
    const int width = static_cast<int>(texture->m_size.x);
    const int height = static_cast<int>(texture->m_size.y);
    if (width <= 0 || height <= 0 || width > 8192 || height > 8192 ||
        static_cast<std::uint64_t>(width) * height > MAX_PNG_PIXELS) {
        gDumpResult = "invalid-mirror-size";
        return;
    }

    GLint previousRead = 0;
    GLint packAlignment = 0;
    glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &previousRead);
    glGetIntegerv(GL_PACK_ALIGNMENT, &packAlignment);
    GLuint framebuffer = 0;
    glGenFramebuffers(1, &framebuffer);
    glBindFramebuffer(GL_READ_FRAMEBUFFER, framebuffer);
    Hyprutils::Utils::CScopeGuard restore([previousRead, packAlignment, framebuffer]() {
        glBindFramebuffer(GL_READ_FRAMEBUFFER, static_cast<GLuint>(previousRead));
        glPixelStorei(GL_PACK_ALIGNMENT, packAlignment);
        glDeleteFramebuffers(1, &framebuffer);
    });
    glFramebufferTexture2D(GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, texture->m_texID, 0);
    if (glCheckFramebufferStatus(GL_READ_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        gDumpResult = "mirror-framebuffer-incomplete";
        return;
    }
    std::vector<std::uint8_t> rgba(static_cast<std::size_t>(width) * height * 4);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, rgba.data());
    std::vector<std::uint32_t> argb(static_cast<std::size_t>(width) * height);
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            // Hyprland's mirror texture is already in export row order.
            const auto source = (static_cast<std::size_t>(y) * width + x) * 4;
            argb[static_cast<std::size_t>(y) * width + x] = 0xFF000000U | (static_cast<std::uint32_t>(rgba[source]) << 16) |
                (static_cast<std::uint32_t>(rgba[source + 1]) << 8) | rgba[source + 2];
        }
    }
    auto* image = cairo_image_surface_create_for_data(reinterpret_cast<unsigned char*>(argb.data()), CAIRO_FORMAT_ARGB32, width, height, width * 4);
    const auto status = cairo_surface_write_to_png(image, (std::filesystem::path{gLabRuntime} / "hyprveil-local.png").c_str());
    cairo_surface_destroy(image);
    gDumpResult = status == CAIRO_STATUS_SUCCESS ? "saved" : "write-failed";
}

using MonitorFn = void (*)(Frame*);
using CopyFn = void (*)(Frame*);
using FrameRenderFn = void (*)(Frame*);
using WindowFn = void (*)(Renderer*, PHLWINDOW, PHLMONITOR, const Time::steady_tp&, bool, Render::eRenderPassMode, bool, bool);
using LayerFn = void (*)(Renderer*, PHLLS, PHLMONITOR, const Time::steady_tp&, bool, bool);
using FadeoutFn = void (*)(Renderer*, PHLMONITOR, Desktop::eFadeoutPlane, PHLWORKSPACE);
using PassFn = void (*)(Render::CRenderPass*, UP<IPassElement>&&);
using DragFn = void (*)(Renderer*, PHLMONITOR, const Time::steady_tp&);
using SetPropFn = Config::Actions::ActionResult (*)(const std::string&, const std::string&, std::optional<PHLWINDOW>);
static_assert(std::is_same_v<SetPropFn, decltype(&Config::Actions::setProp)>);
using ParentFn = void (*)(CXDGToplevelResource*, SP<CXDGToplevelResource>);
static_assert(std::is_same_v<decltype(&CXDGToplevelResource::setNewParent), void (CXDGToplevelResource::*)(SP<CXDGToplevelResource>)>);
using X11PropertyFn = void (*)(CXWM*, SP<CXWaylandSurface>, uint32_t, xcb_get_property_reply_t*);

template <typename Fn>
Fn original(CFunctionHook* hook) {
    return reinterpret_cast<Fn>(hook->m_original);
}

void invalidateSnapshot(const Session* session) {
    const auto& snapshot = session->*member(PermissionSnapshot{});
    if (!snapshot || !snapshot->isAllocated())
        return;
    // CGLFramebuffer::release uses GL before its texture destructor makes the
    // context current. This can run outside an active capture/render pass.
    if (g_pHyprRenderer->type() == Renderer::RT_GL)
        Render::GL::g_pHyprOpenGL->makeEGLCurrent();
    snapshot->release();
    ++gInvalidatedSnapshots;
}

bool retainedPrivacy(const PHLWINDOW& window) {
    const auto found = gOrphanPrivacy.find(window.get());
    return found != gOrphanPrivacy.end() && !found->second.expired() && found->second.get() == window.get();
}

bool privateWindow(PHLWINDOW window) {
    // Separate transient toplevels inherit their mapped ancestor's privacy.
    // A malformed/cyclic/overlong client hierarchy fails closed, never loops.
    return Hyprveil::Privacy::matchesOrMalformed(std::move(window),
        [](const auto& candidate) { return candidate->parent(); },
        [](const auto& candidate) { return candidate.get(); },
        [](const auto& candidate) {
            return retainedPrivacy(candidate) ||
                (candidate->m_ruleApplicator && candidate->m_ruleApplicator->noScreenShare().valueOrDefault());
        });
}

bool retainClosingDescendants(const PHLWINDOW& closing) {
    std::erase_if(gOrphanPrivacy, [](const auto& entry) { return entry.second.expired(); });
    bool changed = false;
    // v0.56.2 emits window.close while the parent is still mapped and its
    // current native rules are still live (Window.cpp:2575, before :2644).
    // Observe them now: tag->close can happen before any capture/render pass.
    if (privateWindow(closing)) {
        for (const auto& child : Desktop::windowState()->windows()) {
            if (child == closing)
                continue;
            const bool descendant = Hyprveil::Privacy::matchesOrMalformed(child->parent(),
                [](const auto& candidate) { return candidate->parent(); },
                [](const auto& candidate) { return candidate.get(); },
                [&closing](const auto& candidate) { return candidate == closing; });
            if (descendant && !retainedPrivacy(child)) {
                gOrphanPrivacy.insert_or_assign(child.get(), PHLWINDOWREF{child});
                changed = true;
            }
        }
    }
    return changed;
}

void watchWindowRole(const PHLWINDOW& window) {
    std::erase_if(gRoleWatches, [](const auto& entry) { return entry.second.window.expired() || entry.second.role.expired(); });
    if (!window || window->m_isX11 || !window->m_xdgSurface)
        return;
    const auto role = window->m_xdgSurface->m_toplevel;
    if (!role || role.expired())
        return;
    const auto found = gRoleWatches.find(window.get());
    if (found != gRoleWatches.end() && found->second.window.get() == window.get() && found->second.role == role)
        return;
    const PHLWINDOWREF weakWindow = window;
    auto destroyed = role->m_events.destroy.listen([weakWindow]() {
        // GTK can destroy xdg_toplevel BEFORE xdg_surface emits Window.close.
        // Its destroy event fires before the owning protocol SP is erased, so
        // descendants still expose the live ancestry here. The later CWindow
        // close event clears the closing window's own latch. Duplicate native
        // destroy emissions are harmless because retention is idempotent.
        if (const auto closing = weakWindow.lock(); closing && retainClosingDescendants(closing))
            capturePolicyChanged();
    });
    gRoleWatches.insert_or_assign(window.get(), RoleWatch{weakWindow, role, std::move(destroyed)});
}

void closingWindow(PHLWINDOW closing) {
    bool changed = retainClosingDescendants(closing);
    // A child may itself be closing with surviving descendants. Propagate its
    // retained privacy BEFORE clearing the closing child's own map lifetime.
    changed = gOrphanPrivacy.erase(closing.get()) != 0 || changed;
    gRoleWatches.erase(closing.get());
    gSharingChoices.erase(closing.get());
    if (changed)
        capturePolicyChanged();
}

void copyHook(Frame* frame) {
    // Sessions are owned by CUniquePointer, not CSharedPointer. Their weak
    // references support get()/operator->; lock() deliberately asserts here.
    const auto sessionRef = frame->*member(FrameSession{});
    const auto* session = sessionRef.get();
    // A permission prompt freezes a cached framebuffer before user consent.
    // Native render() returns that snapshot without calling renderMonitor().
    // Discard it after policy changes, including black mode and window export.
    // Do all bookkeeping before originalCopy: it may finish/delete the frame.
    std::erase_if(gSessionPolicies, [](const auto& entry) { return entry.second.session.expired(); });
    if (session) {
        const auto previous = gSessionPolicies.find(session);
        const bool unknownOrChanged = previous == gSessionPolicies.end() || previous->second.session.get() != session ||
            previous->second.generation != gPolicyGeneration;
        if (unknownOrChanged)
            invalidateSnapshot(session);
        gSessionPolicies.insert_or_assign(session, SessionPolicy{sessionRef, gPolicyGeneration});
    }
    const auto monitor = session ? session->monitor() : nullptr;
    const bool customScene = gEnabled && monitor;
    if (!customScene) {
        original<CopyFn>(gCopyHook)(frame);
        return;
    }

    // SurfacePassElement draws/discards are deferred until endRender(), after
    // renderMonitor() has returned. Keep feedback suppression around the entire
    // copy operation, including SHM/DMABUF draw submission and permission
    // snapshot rendering. Scope restoration also covers early return/exception.
    auto* renderer = g_pHyprRenderer.get();
    const bool oldFeedback = renderer->m_bBlockSurfaceFeedback;
    // CRenderPass::render writes this monitor-level optimization flag even for
    // a fake/export pass. Our blur-free capture must not disable local blur.
    const bool oldBlurQueued = monitor->m_blurFBShouldRender;
    const bool oldNoSimplify = renderer->m_renderData.noSimplify;
    const auto oldRenderModif = renderer->m_renderData.renderModif;
    renderer->m_bBlockSurfaceFeedback = true;
    // Export coordinates are independent of local workspace/plugin hints.
    renderer->m_renderData.renderModif = {};
    Hyprutils::Utils::CScopeGuard restore([renderer, monitor, oldFeedback, oldBlurQueued, oldNoSimplify, oldRenderModif]() {
        renderer->m_bBlockSurfaceFeedback = oldFeedback;
        monitor->m_blurFBShouldRender = oldBlurQueued;
        renderer->m_renderData.noSimplify = oldNoSimplify;
        renderer->m_renderData.renderModif = oldRenderModif;
    });
    original<CopyFn>(gCopyHook)(frame);
}

void opaqueCapture(Frame* frame) {
    // Never sample a mirror or lock surface on these denial paths. Clear the
    // complete export target, including region captures and non-square outputs.
    auto* renderer = g_pHyprRenderer.get();
    const auto size = frame->bufferSize();
    renderer->m_renderData.fbSize = size;
    renderer->setProjectionType(Render::RPT_EXPORT);
    renderer->m_renderData.transformDamage = false;
    renderer->m_renderData.noSimplify = true;
    renderer->setViewport(0, 0, static_cast<int>(size.x), static_cast<int>(size.y));
    renderer->draw(CClearPassElement::SClearData{Colors::BLACK}, CRegion{0, 0, size.x, size.y});
}

void frameRenderHook(Frame* frame) {
    // Native render() can export an allocated permission snapshot before it
    // calls renderMonitor/renderWindow. Deny all capture types while locked,
    // including window capture, without acknowledging a local lockscreen frame.
    const auto* session = (frame->*member(FrameSession{})).get();
    const auto monitor = session ? session->monitor() : nullptr;
    const auto type = session ? session->*member(SessionType{}) : Screenshare::SHARE_NONE;
    const bool unsupportedOutput = monitor && (type == Screenshare::SHARE_MONITOR || type == Screenshare::SHARE_REGION) &&
        (monitor->isMirror() || monitor->m_transform != WL_OUTPUT_TRANSFORM_NORMAL);
    const bool privateTarget = session && session->*member(SessionType{}) == Screenshare::SHARE_WINDOW &&
        privateWindow((session->*member(SessionWindow{})).lock());
    if (gEnabled && (g_pSessionLockManager->isSessionLocked() || privateTarget || unsupportedOutput)) {
        if (monitor)
            dumpLocalMirror(monitor); // Lab-only diagnostic; live never reads pixels.
        // The denied snapshot can no longer be used after this copy, even if
        // an output transform changed without a config reload in between.
        if (session)
            invalidateSnapshot(session);
        opaqueCapture(frame);
        ++gFallbackFrames;
        return;
    }
    original<FrameRenderFn>(gFrameRenderHook)(frame);
}

void parentHook(CXDGToplevelResource* self, SP<CXDGToplevelResource> parent) {
    const auto window = self->m_window.lock();
    const bool before = privateWindow(window);
    ParentDiagnostic diagnostic;
    // set_parent normally arrives before the child's app-id/title requests.
    // Its already mapped synthetic parent identifies this fixture request.
    const bool fixtureWindow = window && window->m_class.starts_with("org.hyprveil.fixture.");
    const bool fixtureParent = parent && parent->m_window && parent->m_window->m_class.starts_with("org.hyprveil.fixture.");
    const bool diagnose = !gLiveSession && (fixtureWindow || fixtureParent);
    if (diagnose) {
        diagnostic.self = reinterpret_cast<std::uintptr_t>(self);
        diagnostic.argument = reinterpret_cast<std::uintptr_t>(parent.get());
        diagnostic.window = reinterpret_cast<std::uintptr_t>(window.get());
        diagnostic.argumentWindow = reinterpret_cast<std::uintptr_t>(parent ? parent->m_window.get() : nullptr);
        diagnostic.beforeParent = reinterpret_cast<std::uintptr_t>(self->m_parent.get());
        diagnostic.beforePrivate = before;
    }
    // Never move from an intercepted nontrivial by-value argument. Native
    // v0.56.2's same-TU caller caches its control block and decrements it after
    // return without rereading this argument (ELF 0x94f012). Moving here would
    // also decrement our forwarding temporary and destroy the live parent.
    // A separate copy preserves the native caller's object and ownership.
    original<ParentFn>(gParentHook)(self, parent);
    if (diagnose) {
        diagnostic.afterParent = reinterpret_cast<std::uintptr_t>(self->m_parent.get());
        diagnostic.afterImpl = reinterpret_cast<std::uintptr_t>(self->m_parent.impl_);
        diagnostic.afterData = reinterpret_cast<std::uintptr_t>(self->m_parent.m_data);
        diagnostic.afterPrivate = privateWindow(window);
        if (gParentDiagnostics.size() == 100)
            gParentDiagnostics.erase(gParentDiagnostics.begin());
        gParentDiagnostics.push_back(diagnostic);
    }
    // setNewParent emits neither updateRules nor a damage event in v0.56.2.
    // Only a real change in effective capture privacy invalidates static video.
    if (privateWindow(window) != before)
        capturePolicyChanged();
}

void x11PropertyHook(CXWM* self, SP<CXWaylandSurface> surface, uint32_t atom, xcb_get_property_reply_t* reply) {
    if (atom != XCB_ATOM_WM_TRANSIENT_FOR) {
        original<X11PropertyFn>(gX11PropertyHook)(self, surface, atom, reply);
        return;
    }
    PHLWINDOW window;
    for (const auto& candidate : Desktop::windowState()->windows()) {
        if (candidate->m_isX11 && candidate->m_xwaylandSurface == surface) {
            window = candidate;
            break;
        }
    }
    const bool before = privateWindow(window);
    original<X11PropertyFn>(gX11PropertyHook)(self, surface, atom, reply);
    // XWM's transient-property handler also updates ancestry without emitting
    // updateRules. Unrelated X11 properties do no extra work or monitor damage.
    if (privateWindow(window) != before)
        capturePolicyChanged();
}

Config::Actions::ActionResult setPropHook(const std::string& prop, const std::string& value, std::optional<PHLWINDOW> target) {
    // set_prop bypasses RuleApplicator::propertiesChanged/updateRules. Observe
    // the real resolved target and value around the successful native action.
    const auto window = prop == "no_screen_share" ? target.value_or(Desktop::focusState()->window()) : nullptr;
    // Consume our publication before native callbacks run. A later/reentrant
    // external setter owns its new choice, including an explicit false->false.
    const bool ownSharingAction = window && gSharingPublication == window.get();
    if (ownSharingAction) gSharingPublication = nullptr;
    const bool before = window && window->m_ruleApplicator->noScreenShare().valueOrDefault();
    auto result = original<SetPropFn>(gSetPropHook)(prop, value, target);
    if (result && window) {
        const bool after = window->m_ruleApplicator->noScreenShare().valueOrDefault();
        if (after || !ownSharingAction) gSharingChoices.erase(window.get());
        // A successful explicit false setter is the user's reset for orphan
        // inheritance, including false->false. A still-live private ancestor
        // continues to win through privateWindow; rule/title reloads never
        // implicitly clear this retention.
        const bool released = !after && gOrphanPrivacy.erase(window.get()) != 0;
        if (after != before || released)
            capturePolicyChanged();
    }
    return result;
}

bool resetSharingChoices() noexcept {
    if (gResettingSharing) return false;
    gResettingSharing = true;
    struct ResetGuard { ~ResetGuard() { gResettingSharing = false; } } guard;
    bool changed = false, success = true;
    while (!gSharingChoices.empty()) {
        const auto entry = gSharingChoices.begin();
        const auto choice = entry->second;
        gSharingChoices.erase(entry);
        const auto window = choice.window.lock();
        if (!window || !window->m_isMapped || !window->m_ruleApplicator) continue;
        auto& property = window->m_ruleApplicator->noScreenShare();
        // A subsequent native/user choice takes precedence over our reset.
        if (!property.hasValue() || property.getPriority() != Desktop::Types::PRIORITY_SET_PROP || property.valueOrDefault()) continue;
        changed = true;
        try {
            if (choice.priorSetProp) property.set(*choice.priorSetProp, Desktop::Types::PRIORITY_SET_PROP);
            else property.unset(Desktop::Types::PRIORITY_SET_PROP);
            if (g_pCompositor && !g_pCompositor->m_isShuttingDown)
                window->m_ruleApplicator->propertiesChanged(Desktop::Rule::RULE_PROP_ALL);
        } catch (...) {
            // A failed rule refresh must revoke this temporary share too.
            property.set(true, Desktop::Types::PRIORITY_SET_PROP);
            success = false;
        }
    }
    if (changed) {
        try {
            if (g_pCompositor && !g_pCompositor->m_isShuttingDown && g_pHyprRenderer) capturePolicyChanged();
            else ++gPolicyGeneration;
        } catch (...) { success = false; }
    }
    return success;
}

void windowHook(Renderer* self, PHLWINDOW window, PHLMONITOR monitor, const Time::steady_tp& now,
                bool decorate, Render::eRenderPassMode mode, bool ignorePosition, bool standalone) {
    if (gCaptureScene && privateWindow(window)) {
        ++gOmittedWindowPasses;
        if ((gMode == "image" || gMode == "black" || gMode == "spoiler") && mode != Render::RENDER_PASS_POPUP) {
            // Insert the replacement at this window's normal stack position.
            // Do not draw the original surface; image alpha reveals only the
            // already sanitized background. Popups remain omitted separately.
            const auto offset = window->m_workspace && !window->m_pinned ? window->m_workspace->m_renderOffset->value() : Vector2D{};
            auto box = CBox{window->position(Desktop::View::IGeometric::GEOMETRIC_CURRENT) + offset + window->m_floatingOffset - monitor->m_position,
                            window->size(Desktop::View::IGeometric::GEOMETRIC_CURRENT)}.scale(monitor->m_scale);
            if (gMode == "spoiler") {
                prepareSpoiler(self);
                // Always opaque underlay. A failed allocation/upload never
                // delegates to the original window or cached local pixels.
                self->m_renderPass.add(makeUnique<CRectPassElement>(CRectPassElement::SRectData{.box = box, .color = Colors::BLACK}));
                if (gSpoilerShader && gSpoilerShader->program()) {
                    if (gAppearance.animated())
                        animateCapture(monitor, now);
                    const auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()).count();
                    self->m_renderPass.add(makeUnique<SatinPass>(box, monitor->m_scale,
                        Hyprveil::Spoiler::seconds(static_cast<std::uint64_t>(milliseconds)), gSpoilerShader, gAppearance));
                    const double side = std::min({static_cast<double>(gAppearance.eyeSize) * monitor->m_scale, box.w * 0.72, box.h * 0.72});
                    if (gAppearance.eye && side >= 12 && gSpoilerEye && gSpoilerEye->ok()) {
                        const CBox eyeBox{box.pos() + (box.size() - Vector2D{side, side}) / 2.0, Vector2D{side, side}};
                        self->m_renderPass.add(makeUnique<CTexPassElement>(CTexPassElement::SRenderData{.tex = gSpoilerEye, .box = eyeBox}));
                    }
                }
            } else if (gMode == "image" && gImage && gImage->ok())
                self->m_renderPass.add(makeUnique<CTexPassElement>(CTexPassElement::SRenderData{.tex = gImage, .box = box}));
            else
                self->m_renderPass.add(makeUnique<CRectPassElement>(CRectPassElement::SRectData{.box = box, .color = Colors::BLACK}));
        }
        return;
    }
    original<WindowFn>(gWindowHook)(self, window, monitor, now, decorate, mode, ignorePosition, standalone);
}

void layerHook(Renderer* self, PHLLS layer, PHLMONITOR monitor, const Time::steady_tp& now, bool popups, bool lockscreen) {
    if (gCaptureScene && layer && layer->m_ruleApplicator->noScreenShare().valueOrDefault()) {
        ++gOmittedLayerPasses;
        return;
    }
    original<LayerFn>(gLayerHook)(self, layer, monitor, now, popups, lockscreen);
}

void fadeoutHook(Renderer* self, PHLMONITOR monitor, Desktop::eFadeoutPlane plane, PHLWORKSPACE workspace) {
    // v0.56.2 fadeout objects discard their original owner's privacy property.
    // Until we track owners at snapshot creation, omit *all* closing snapshots
    // in the capture pass. Local closing animations remain unchanged.
    if (gCaptureScene)
        return;
    original<FadeoutFn>(gFadeoutHook)(self, monitor, plane, workspace);
}

void dragHook(Renderer* self, PHLMONITOR monitor, const Time::steady_tp& now) {
    // Drag images and IME preedit are separate surfaces whose private owner is
    // not represented by no_screen_share in this version. Omit them in the POC.
    if (!gCaptureScene)
        original<DragFn>(gDragHook)(self, monitor, now);
}

void passHook(Render::CRenderPass* self, UP<IPassElement>&& element) {
    if (gCaptureScene && element) {
        // Cached monitor blur contains the original scene, including omitted
        // windows. Do not reuse it. This POC disables capture-only background
        // blur instead of risking disclosure through a translucent public view.
        switch (element->type()) {
            case EK_PRE_BLUR:
                return;
            case EK_SURFACE: {
                auto* surface = static_cast<CSurfacePassElement*>(element.get());
                surface->m_data.blur = false;
                surface->m_data.blockBlurOptimization = true;
                break;
            }
            case EK_RECT:
                static_cast<CRectPassElement*>(element.get())->m_data.blur = false;
                break;
            case EK_TRANSFORMED_WINDOW:
                static_cast<CTransformedWindowPassElement*>(element.get())->m_data.blur = false;
                break;
            case EK_TEXTURE: {
                auto& data = static_cast<CTexPassElement*>(element.get())->m_data;
                data.blur = false;
                data.blockBlurOptimization = true;
                data.blurredBG.reset();
                data.blurAlphaMatte.reset();
                break;
            }
            default:
                break;
        }
    }
    original<PassFn>(gPassHook)(self, std::move(element));
}

void monitorHook(Frame* frame) {
    const auto* session = (frame->*member(FrameSession{})).get();
    const auto monitor = session ? session->monitor() : nullptr;
    if (!gEnabled || !monitor || frame->done()) {
        ++gFallbackFrames;
        original<MonitorFn>(gMonitorHook)(frame);
        return;
    }
    // Native mirror/rotated masks and transformed-window bounds are not a
    // privacy guarantee. Unsupported outputs fail closed with an opaque frame.
    // storeTempFB() calls renderMonitor directly, so also guard pending consent
    // here; the Frame::render hook covers approved snapshots/window captures.
    if (monitor->isMirror() || monitor->m_transform != WL_OUTPUT_TRANSFORM_NORMAL || g_pSessionLockManager->isSessionLocked()) {
        opaqueCapture(frame);
        ++gFallbackFrames;
        return;
    }

    auto* renderer = g_pHyprRenderer.get();
    const auto size = frame->bufferSize();
    const auto captureBox = session->*member(CaptureBox{});
    const bool overlayCursor = frame->*member(OverlayCursor{});
    const auto now = Time::steadyNow();
    const CBox sceneBox = {{}, monitor->m_pixelSize};
    dumpLocalMirror(monitor);
    if (gMode == "image" && gLoadedImagePath != gImagePath) {
        gImage.reset();
        gLoadedImagePath = gImagePath;
        try {
            const auto bytes = readBoundedPng(gImagePath);
            if (bytes.empty()) {
                gImageStatus = "invalid-png";
            } else {
                PngStream stream{bytes};
                auto* image = cairo_image_surface_create_from_png_stream(readPngStream, &stream);
                Hyprutils::Utils::CScopeGuard destroyImage([image]() { cairo_surface_destroy(image); });
                const int width = cairo_image_surface_get_width(image);
                const int height = cairo_image_surface_get_height(image);
                if (cairo_surface_status(image) != CAIRO_STATUS_SUCCESS || width <= 0 || height <= 0 ||
                    width > static_cast<int>(MAX_PNG_AXIS) || height > static_cast<int>(MAX_PNG_AXIS) ||
                    static_cast<std::uint64_t>(width) * height > MAX_PNG_PIXELS) {
                    gImageStatus = "invalid-png";
                } else {
                    // loadAsset() resolves packaged asset names, not arbitrary
                    // paths. Upload the bounded decoded snapshot only in the
                    // active capture GL context; failure remains a black mask.
                    gImage = renderer->createTexture(image);
                    if (gImage && gImage->ok()) {
                        gImage->m_imageDescription = NColorManagement::DEFAULT_SRGB_IMAGE_DESCRIPTION;
                        gImageStatus = "ready";
                    } else {
                        gImage.reset();
                        gImageStatus = "texture-failed";
                    }
                }
            }
        } catch (...) {
            // Resource failure must not fall back to the real window surface.
            gImage.reset();
            gImageStatus = "texture-failed";
        }
    }

    renderer->m_renderData.fbSize = size;
    renderer->setProjectionType(Render::RPT_EXPORT);
    renderer->m_renderData.transformDamage = false;
    renderer->m_renderData.noSimplify = true;
    renderer->setViewport(0, 0, static_cast<int>(size.x), static_cast<int>(size.y));
    renderer->startRenderPass();

    const bool oldCapture = gCaptureScene;
    const bool oldFeedback = renderer->m_bBlockSurfaceFeedback;
    gCaptureScene = true;
    renderer->m_bBlockSurfaceFeedback = true;
    Hyprutils::Utils::CScopeGuard restore([renderer, oldCapture, oldFeedback]() {
        gCaptureScene = oldCapture;
        renderer->m_bBlockSurfaceFeedback = oldFeedback;
    });

    // Region coordinates were already scaled to physical pixels by Session.
    // Shift scene elements only; window geometry and workspace stay untouched.
    if (captureBox.pos() != Vector2D{}) {
        Render::SRenderModifData modifs;
        modifs.modifs.emplace_back(Render::SRenderModifData::RMOD_TYPE_TRANSLATE, -captureBox.pos());
        renderer->m_renderPass.add(makeUnique<CRendererHintsPassElement>(CRendererHintsPassElement::SData{modifs}));
    }

    (renderer->*member(RenderWorkspace{}))(monitor, monitor->m_activeWorkspace, now, sceneBox);
    // No renderLockscreen call: it changes session-lock acknowledgement state.
    // Locked captures were denied above without rendering any local lock view.
    // IME intentionally omitted: its source can be a protected window.

    if (captureBox.pos() != Vector2D{})
        renderer->m_renderPass.add(makeUnique<CRendererHintsPassElement>(CRendererHintsPassElement::SData{Render::SRenderModifData{}}));

    if (overlayCursor) {
        CRegion damage{0, 0, size.x, size.y};
        const auto cursor = g_pInputManager->getMouseCoordsInternal() - monitor->m_position - captureBox.pos() / monitor->m_scale;
        // Native monitor capture reuses a mirror that already contains any
        // software cursor, and normally skips drawing it twice. Our rebuilt
        // scene has no such cursor, so force it exactly once when requested.
        Pointer::mgr()->renderSoftwareCursorsFor(monitor, now, damage, cursor, true, true);
    }
    ++gSceneFrames;
}

CFunctionHook* makeHook(const std::string& readableName, const char* exactSymbol, const void* destination) {
    // v0.56.2's name lookup caches two separate nm outputs and associates
    // demangled names by row number. Every hook resolves a verified exact ELF
    // export directly, after the ABI gate, without relying on that mapping.
    void* source = dlsym(RTLD_DEFAULT, exactSymbol);
    if (!source)
        throw std::runtime_error("hyprveil: missing exact internal hook: " + readableName + "; export: " + exactSymbol);
    Dl_info target {}, nativeApi {};
    const auto* api = dlsym(RTLD_DEFAULT, "__hyprland_api_get_hash");
    if (!api || !dladdr(source, &target) || !dladdr(api, &nativeApi) || target.dli_fbase != nativeApi.dli_fbase ||
        !target.dli_sname || std::string{target.dli_sname} != exactSymbol)
        throw std::runtime_error("hyprveil: internal hook export identity/module mismatch: " + readableName + "; export: " + exactSymbol);
    auto* hook = HyprlandAPI::createFunctionHook(gHandle, source, destination);
    if (!hook)
        throw std::runtime_error("hyprveil: failed to allocate hook: " + readableName);
    gHooks.push_back(hook);
    return hook;
}

using NativeSettings = Hyprveil::NativeConfig::Settings;
using NativePatch = Hyprveil::NativeConfig::Patch;
using NativeValue = Hyprveil::NativeConfig::Value;

void syncNativeConfiguration();
void failNativeConfiguration() noexcept;
void nativeConfigParsed() noexcept {
    if (!gConfigReady || gConfigBatch)
        return;
    try {
        syncNativeConfiguration();
    } catch (...) {
        // No exception may escape a Lua parser or compositor event callback.
        // A resource failure retains interception and an opaque replacement.
        failNativeConfiguration();
    }
}

// v0.56.2 fromGenericValue() drops generic validators and integer bounds.
// Retain the official typed registration/ownership, but install strict native
// Lua parsers in the public config-value map before exposing our callbacks.
class NativeString final : public Config::Lua::CLuaConfigString {
  public:
    NativeString(std::string field, std::string value) : CLuaConfigString(value), m_field(std::move(field)) {}
    Config::Lua::SParseError parse(lua_State* state) override {
        try {
        using namespace Config::Lua;
        if (lua_type(state, -1) != LUA_TSTRING)
            return {.errorCode=PARSE_ERROR_BAD_TYPE, .message="Hyprveil string setting requires a string"};
        std::size_t size = 0;
        const char* bytes = lua_tolstring(state, -1, &size);
        if (size > 4096)
            return {.errorCode=PARSE_ERROR_BAD_VALUE, .message="Hyprveil string setting exceeds its bound"};
        NativeSettings checked;
        const auto result = checked.set(m_field, std::string{bytes, size});
        if (!result)
            return {.errorCode=PARSE_ERROR_BAD_VALUE, .message=result.error()};
        // Store canonical color in the same value read by hl.get_config.
        if (m_field == "color") lua_pushlstring(state, checked.appearance.color.data(), checked.appearance.color.size());
        const auto parsed = CLuaConfigString::parse(state);
        if (m_field == "color") lua_pop(state, 1);
        if (parsed.errorCode == PARSE_ERROR_OK) nativeConfigParsed();
        return parsed;
        } catch (...) {
            failNativeConfiguration();
            return {.errorCode=Config::Lua::PARSE_ERROR_BAD_VALUE, .message="resource error"};
        }
    }
    void secureBlack() noexcept {
        // This exact-ABI native string owns its storage. Mode grammar is at
        // most seven bytes, so black fits its already allocated string buffer.
        auto* value = const_cast<std::string*>(static_cast<const std::string*>(data()));
        value->assign("black");
        m_bSetByUser = true;
    }
  private:
    std::string m_field;
};
NativeString* gNativeModeParser = nullptr;

void failNativeConfiguration() noexcept {
    if (gNativeModeParser) gNativeModeParser->secureBlack();
    gMode = "black";
    gEnabled = true;
    gAnimatedCaptures.clear();
    try {
        if (gSpoilerTimer) gSpoilerTimer->updateTimeout(std::nullopt);
        capturePolicyChanged();
    } catch (...) {}
}

class NativeInt final : public Config::Lua::CLuaConfigInt {
  public:
    NativeInt(int value, int minimum, int maximum) : CLuaConfigInt(value, minimum, maximum) {}
    Config::Lua::SParseError parse(lua_State* state) override {
        try {
        if (!lua_isinteger(state, -1))
            return {.errorCode=Config::Lua::PARSE_ERROR_BAD_TYPE, .message="Hyprveil integer setting requires an integer"};
        const auto parsed = CLuaConfigInt::parse(state);
        if (parsed.errorCode == Config::Lua::PARSE_ERROR_OK) nativeConfigParsed();
        return parsed;
        } catch (...) {
            failNativeConfiguration();
            return {.errorCode=Config::Lua::PARSE_ERROR_BAD_VALUE, .message="resource error"};
        }
    }
};

class NativeBool final : public Config::Lua::CLuaConfigBool {
  public:
    NativeBool(bool value) : CLuaConfigBool(value) {}
    Config::Lua::SParseError parse(lua_State* state) override {
        try {
        if (lua_type(state, -1) != LUA_TBOOLEAN)
            return {.errorCode=Config::Lua::PARSE_ERROR_BAD_TYPE, .message="Hyprveil boolean setting requires a boolean"};
        const auto parsed = CLuaConfigBool::parse(state);
        if (parsed.errorCode == Config::Lua::PARSE_ERROR_OK) nativeConfigParsed();
        return parsed;
        } catch (...) {
            failNativeConfiguration();
            return {.errorCode=Config::Lua::PARSE_ERROR_BAD_VALUE, .message="resource error"};
        }
    }
};

Config::Lua::ILuaConfigValue* nativeValue(std::string_view field) {
    // The config manager is uniquely owned: its weak handle supports access,
    // not lock() into a shared owner. All calls execute on its event loop.
    const auto manager = Config::Lua::mgr();
    if (!manager) throw std::runtime_error("hyprveil: native configuration requires Lua Hyprland");
    const auto found = manager->m_configValues.find("plugin.hyprveil." + std::string{field});
    if (found == manager->m_configValues.end() || !found->second)
        throw std::runtime_error("hyprveil: registered config value unavailable");
    return found->second.get();
}

NativeSettings readNativeConfiguration() {
    NativeSettings result;
    result.mode = nativeValue("mode")->asString();
    result.imagePath = nativeValue("image_path")->asString();
    result.appearance.variant = nativeValue("variant")->asString();
    result.appearance.color = nativeValue("color")->asString();
    result.appearance.grain = nativeValue("grain")->asInt();
    result.appearance.speed = nativeValue("speed")->asInt();
    result.appearance.darkness = nativeValue("darkness")->asInt();
    result.appearance.eye = nativeValue("eye")->asInt();
    result.appearance.eyeSize = nativeValue("eye_size")->asInt();
    return result;
}

void syncNativeConfiguration() {
    const auto settings = readNativeConfiguration();
    const bool changed = gMode != settings.mode || gImagePath != settings.imagePath || gAppearance != settings.appearance || !gEnabled;
    if (gImagePath != settings.imagePath) {
        gLoadedImagePath.clear();
        gImageStatus = "pending";
    }
    gMode = settings.mode;
    gImagePath = settings.imagePath;
    gAppearance = settings.appearance;
    gEnabled = true;
    if (gMode != "spoiler" || !gAppearance.animated()) {
        gAnimatedCaptures.clear();
        if (gSpoilerTimer) gSpoilerTimer->updateTimeout(std::nullopt);
    }
    if (changed) capturePolicyChanged();
}

void pushNativeValue(lua_State* state, const NativeValue& value) {
    if (const auto* text = std::get_if<std::string>(&value)) lua_pushlstring(state, text->data(), text->size());
    else if (const auto* number = std::get_if<std::int64_t>(&value)) lua_pushinteger(state, *number);
    else lua_pushboolean(state, std::get<bool>(value));
}

std::expected<void, std::string> updateNativeConfiguration(const NativePatch& patch, bool reloadImage = false) {
    const auto candidate = readNativeConfiguration().patched(patch);
    if (!candidate) return std::unexpected(candidate.error());
    if (!gConfigWriter) return std::unexpected("native config writer unavailable");
    // Full validation precedes all mutations. No Lua code/eval or IPC is run.
    gConfigBatch = true;
    Hyprutils::Utils::CScopeGuard batch([] { gConfigBatch = false; });
    const auto values = candidate->values();
    for (const auto& [field, ignored] : patch) {
        const auto& value = values.at(field);
        pushNativeValue(gConfigWriter, value);
        const auto result = nativeValue(field)->parse(gConfigWriter);
        lua_pop(gConfigWriter, 1);
        if (result.errorCode != Config::Lua::PARSE_ERROR_OK) {
            failNativeConfiguration();
            return std::unexpected("validated native setting could not be committed");
        }
    }
    syncNativeConfiguration();
    if (reloadImage) {
        gLoadedImagePath.clear();
        gImageStatus = "pending";
        capturePolicyChanged();
    }
    return {};
}

void pushNativeStatus(lua_State* state) {
    lua_createtable(state, 0, 4);
    lua_pushinteger(state, 1); lua_setfield(state, -2, "config_api");
    lua_pushlstring(state, gMode.data(), gMode.size()); lua_setfield(state, -2, "mode");
    lua_pushlstring(state, gImagePath.data(), gImagePath.size()); lua_setfield(state, -2, "image_path");
    lua_createtable(state, 0, 7);
    for (const auto& [field, value] : NativeSettings{.appearance=gAppearance}.values()) {
        if (field == "mode" || field == "image_path") continue;
        pushNativeValue(state, value); lua_setfield(state, -2, field.c_str());
    }
    lua_setfield(state, -2, "appearance");
}

int luaNativeStatus(lua_State* state) {
    try {
        if (lua_gettop(state) != 0) {
            lua_pushnil(state); lua_pushliteral(state, "hyprveil.status expects no arguments"); return 2;
        }
        if (gConfigReady) syncNativeConfiguration();
        pushNativeStatus(state);
        return 1;
    } catch (...) {
        lua_pushnil(state); lua_pushliteral(state, "native Hyprveil status unavailable"); return 2;
    }
}

int luaNativeConfigure(lua_State* state) {
    const auto fail = [state](const std::string& error) {
        lua_pushnil(state); lua_pushlstring(state, error.data(), error.size()); return 2;
    };
    try {
        if (lua_gettop(state) != 1 || lua_type(state, 1) != LUA_TTABLE)
            return fail("hyprveil.configure expects one plain partial settings table");
        if (lua_getmetatable(state, 1)) {
            lua_pop(state, 1); return fail("hyprveil.configure does not accept a table metatable");
        }
        NativePatch patch;
        lua_pushnil(state);
        while (lua_next(state, 1) != 0) {
            if (patch.size() >= 9 || lua_type(state, -2) != LUA_TSTRING)
                return fail("hyprveil.configure requires at most nine string keys");
            std::size_t size = 0;
            const char* bytes = lua_tolstring(state, -2, &size);
            if (size > 16) return fail("unknown Hyprveil setting");
            const std::string field{bytes, size};
            NativeValue value;
            if (lua_type(state, -1) == LUA_TSTRING) {
                bytes = lua_tolstring(state, -1, &size);
                if (size > 4096) return fail("Hyprveil string setting exceeds its bound");
                value = std::string{bytes, size};
            } else if (lua_type(state, -1) == LUA_TBOOLEAN) value = bool(lua_toboolean(state, -1));
            else if (lua_isinteger(state, -1)) value = std::int64_t{lua_tointeger(state, -1)};
            else return fail("Hyprveil values require a string, integer or boolean");
            patch.emplace(field, std::move(value));
            lua_pop(state, 1);
        }
        const auto updated = updateNativeConfiguration(patch);
        if (!updated) return fail(updated.error());
        pushNativeStatus(state);
        return 1;
    } catch (...) {
        failNativeConfiguration();
        return fail("Hyprveil configuration failed; capture replacement is black");
    }
}

int luaNativeDispatcher(lua_State* state, bool cycle) {
    const auto fail = [state](const std::string& error) {
        lua_pushnil(state); lua_pushlstring(state, error.data(), error.size()); return 2;
    };
    try {
        std::string argument;
        if (cycle) {
            if (lua_gettop(state) != 0) return fail("hyprveil.cycle expects no arguments");
        } else {
            if (lua_gettop(state) != 1 || lua_type(state, 1) != LUA_TSTRING)
                return fail("hyprveil.mode expects one mode string");
            std::size_t size = 0;
            const auto* bytes = lua_tolstring(state, 1, &size);
            if (size > 7) return fail("mode must be black, omit, spoiler or image");
            argument.assign(bytes, size);
        }
        if (!g_pKeybindManager) return fail("native dispatcher manager unavailable");
        const auto found = g_pKeybindManager->m_dispatchers.find(cycle ? "hyprveil:cycle" : "hyprveil:mode");
        if (found == g_pKeybindManager->m_dispatchers.end()) return fail("native Hyprveil dispatcher unavailable");
        // Lua Hyprland's dispatcher table exposes only built-in actions. This
        // plugin-owned ID callback bridges directly to our native dispatcher,
        // without eval, shell, IPC or a raw callback retained by Lua on unload.
        const auto result = found->second(std::move(argument));
        if (!result.success) return fail(result.error);
        pushNativeStatus(state);
        return 1;
    } catch (...) {
        failNativeConfiguration();
        return fail("native dispatcher failed; black selected");
    }
}
int luaNativeMode(lua_State* state) { return luaNativeDispatcher(state, false); }
int luaNativeCycle(lua_State* state) { return luaNativeDispatcher(state, true); }

PHLWINDOW focusedPrivacyWindow() {
    const auto window = Desktop::focusState()->window();
    return window && window->m_isMapped && window->m_ruleApplicator ? window : nullptr;
}

void pushPrivacySnapshot(lua_State* state, const PHLWINDOW& window) {
    const bool present = window && window->m_isMapped && window->m_ruleApplicator;
    const bool native = present && window->m_ruleApplicator->noScreenShare().valueOrDefault();
    const bool effective = present && privateWindow(window);
    const auto address = present ? std::format("0x{:x}", reinterpret_cast<std::uintptr_t>(window.get())) : std::string{};
    const auto identity = present ? std::to_string(window->m_stableID) : std::string{};
    lua_createtable(state, 0, 5);
    lua_pushstring(state, present ? effective ? "hidden" : "visible" : "none"); lua_setfield(state, -2, "state");
    lua_pushlstring(state, address.data(), address.size()); lua_setfield(state, -2, "address");
    lua_pushlstring(state, identity.data(), identity.size()); lua_setfield(state, -2, "stable_id");
    lua_pushboolean(state, native); lua_setfield(state, -2, "native_private");
    lua_pushboolean(state, effective && !native); lua_setfield(state, -2, "inherited");
}

std::expected<void, std::string> setPinnedHidden(const PHLWINDOW& window, bool hidden) {
    if (gResettingSharing) return std::unexpected("temporary sharing reset is in progress");
    if (!window || !window->m_isMapped || !window->m_ruleApplicator || Desktop::focusState()->window() != window)
        return std::unexpected("privacy target is no longer the focused mapped window");
    bool attempted = false;
    const bool protectedBefore = privateWindow(window);
    try {
        auto& property = window->m_ruleApplicator->noScreenShare();
        if (!hidden) {
            // Sharing a transient cannot revoke its ancestor or orphan latch.
            // Validate before setting anything, even if its own flag is true.
            if ((protectedBefore && !property.valueOrDefault()) || privateWindow(window->parent()))
                return std::unexpected("window protection is inherited; change the protected parent instead");
            // An already visible window needs no false override. Otherwise a
            // harmless repeated click could suppress future native rules.
            if (!protectedBefore) return {};
            std::erase_if(gSharingChoices, [](const auto& entry) { return entry.second.window.expired(); });
            const auto existing = gSharingChoices.find(window.get());
            if (existing == gSharingChoices.end()) {
                if (gSharingChoices.size() >= SHARING_LIMIT)
                    return std::unexpected("temporary sharing limit reached; revoke existing shares first");
                std::optional<bool> prior;
                if (property.hasValue() && property.getPriority() == Desktop::Types::PRIORITY_SET_PROP)
                    prior = property.valueOrDefault();
                // Allocate bookkeeping before publishing a visible property.
                gSharingChoices.emplace(window.get(), SharingChoice{window, prior});
            }
        }
        attempted = true;
        gSharingPublication = window.get();
        struct PublicationGuard { ~PublicationGuard() { gSharingPublication = nullptr; } } publication;
        const auto result = Config::Actions::setProp("no_screen_share", hidden ? "true" : "false", window);
        if (!result || privateWindow(window) != hidden) {
            property.set(true, Desktop::Types::PRIORITY_SET_PROP);
            gSharingChoices.erase(window.get());
            capturePolicyChanged();
            return std::unexpected("native privacy change failed; window remains protected");
        }
        return {};
    } catch (...) {
        if (attempted) {
            window->m_ruleApplicator->noScreenShare().set(true, Desktop::Types::PRIORITY_SET_PROP);
            gSharingChoices.erase(window.get());
            try { capturePolicyChanged(); } catch (...) {}
        }
        // Short owned string avoids a second allocation on the failure path.
        return std::unexpected("privacy failed");
    }
}

int luaPrivacyError(lua_State* state, std::string_view message) {
    lua_pushnil(state); lua_pushlstring(state, message.data(), message.size()); return 2;
}

int luaActivePrivacy(lua_State* state) {
    try {
        if (lua_gettop(state) != 0) return luaPrivacyError(state, "hyprveil.active_privacy expects no arguments");
        pushPrivacySnapshot(state, focusedPrivacyWindow());
        return 1;
    } catch (...) { return luaPrivacyError(state, "native privacy state unavailable"); }
}

int luaSetHidden(lua_State* state) {
    try {
        if (lua_gettop(state) != 3 || lua_type(state, 1) != LUA_TSTRING || lua_type(state, 2) != LUA_TSTRING || lua_type(state, 3) != LUA_TBOOLEAN)
            return luaPrivacyError(state, "hyprveil.set_hidden expects address, stable_id and boolean hidden");
        std::size_t addressSize = 0, identitySize = 0;
        const auto* addressBytes = lua_tolstring(state, 1, &addressSize);
        const auto* identityBytes = lua_tolstring(state, 2, &identitySize);
        if (addressSize < 3 || addressSize > 18 || identitySize == 0 || identitySize > 19)
            return luaPrivacyError(state, "invalid bounded privacy identity");
        const std::string_view address{addressBytes, addressSize}, identity{identityBytes, identitySize};
        if (!address.starts_with("0x") || identity.front() == '0' ||
            !std::all_of(identity.begin(), identity.end(), [](char c) { return c >= '0' && c <= '9'; }) ||
            !std::all_of(address.begin() + 2, address.end(), [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); }))
            return luaPrivacyError(state, "invalid canonical privacy identity");
        const auto window = focusedPrivacyWindow();
        if (!window || address != std::format("0x{:x}", reinterpret_cast<std::uintptr_t>(window.get())) || identity != std::to_string(window->m_stableID))
            return luaPrivacyError(state, "privacy target identity or focus changed");
        const auto result = setPinnedHidden(window, lua_toboolean(state, 3));
        if (!result) return luaPrivacyError(state, result.error());
        pushPrivacySnapshot(state, window);
        return 1;
    } catch (...) { return luaPrivacyError(state, "native privacy action unavailable"); }
}

int luaTogglePrivacy(lua_State* state) {
    try {
        if (lua_gettop(state) != 0) return luaPrivacyError(state, "hyprveil.toggle expects no arguments");
        const auto window = focusedPrivacyWindow();
        if (window) {
            const auto result = setPinnedHidden(window, !privateWindow(window));
            if (!result) return luaPrivacyError(state, result.error());
        }
        pushPrivacySnapshot(state, window);
        return 1;
    } catch (...) { return luaPrivacyError(state, "native privacy action unavailable"); }
}

int luaResetSharing(lua_State* state) {
    try {
        if (lua_gettop(state) != 0) return luaPrivacyError(state, "hyprveil.reset_sharing expects no arguments");
        if (!resetSharingChoices()) return luaPrivacyError(state, "temporary shares revoked; native rule refresh failed");
        pushPrivacySnapshot(state, focusedPrivacyWindow());
        return 1;
    } catch (...) { return luaPrivacyError(state, "native privacy reset unavailable"); }
}

void registerNativeConfiguration() {
    const auto manager = Config::Lua::mgr();
    if (!manager) throw std::runtime_error("hyprveil: this configuration API requires Lua Hyprland");
    constexpr std::array<const char*, 9> names{"plugin:hyprveil:mode", "plugin:hyprveil:image_path", "plugin:hyprveil:variant",
        "plugin:hyprveil:color", "plugin:hyprveil:grain", "plugin:hyprveil:speed", "plugin:hyprveil:darkness",
        "plugin:hyprveil:eye", "plugin:hyprveil:eye_size"};
    const auto defaults = NativeSettings{}.values();
    for (const auto* field : Hyprveil::NativeConfig::FIELDS)
        if (manager->m_configValues.contains("plugin.hyprveil." + std::string{field}))
            throw std::runtime_error("hyprveil: native configuration name collision");
    for (std::size_t index = 0; index < names.size(); ++index) {
        const auto* field = Hyprveil::NativeConfig::FIELDS[index];
        const auto& value = defaults.at(field);
        SP<Config::Values::IValue> generic;
        UP<Config::Lua::ILuaConfigValue> strict;
        if (const auto* text = std::get_if<std::string>(&value)) {
            generic = makeShared<Config::Values::CStringValue>(names[index], "Hyprveil capture replacement setting", *text);
            strict = makeUnique<NativeString>(field, *text);
        } else if (const auto* number = std::get_if<std::int64_t>(&value)) {
            const int minimum = std::string_view{field} == "eye_size" ? 40 : 0;
            const int maximum = std::string_view{field} == "eye_size" ? 128 : std::string_view{field} == "speed" ? 200 : 100;
            generic = makeShared<Config::Values::CIntValue>(names[index], "Hyprveil capture replacement setting", *number,
                Config::Values::SIntValueOptions{.min=minimum, .max=maximum});
            strict = makeUnique<NativeInt>(*number, minimum, maximum);
        } else {
            generic = makeShared<Config::Values::CBoolValue>(names[index], "Hyprveil capture replacement setting", std::get<bool>(value));
            strict = makeUnique<NativeBool>(std::get<bool>(value));
        }
        if (!HyprlandAPI::addConfigValueV2(gHandle, generic))
            throw std::runtime_error("hyprveil: typed native config registration failed");
        const auto key = "plugin.hyprveil." + std::string{field};
        manager->m_configValues.at(key) = std::move(strict);
        if (index == 0) gNativeModeParser = static_cast<NativeString*>(manager->m_configValues.at(key).get());
        // Repoint the official stable pointer cell after replacing its parser.
        manager->getConfigValue(key);
        gRegisteredConfig.emplace_back(key);
    }
    gConfigWriter = luaL_newstate();
    if (!gConfigWriter) throw std::runtime_error("hyprveil: native config writer allocation failed");
}

void cleanup() {
    gConfigReady = false;
    // Revoke this plugin's temporary shares while its native hooks and window
    // objects still exist. Unloading never leaves a forgotten false override.
    resetSharingChoices();
    gConfigPreReloadListener.reset();
    if (gLuaConfigure) HyprlandAPI::removeLuaFunction(gHandle, "hyprveil", "configure");
    if (gLuaStatus) HyprlandAPI::removeLuaFunction(gHandle, "hyprveil", "status");
    if (gLuaMode) HyprlandAPI::removeLuaFunction(gHandle, "hyprveil", "mode");
    if (gLuaCycle) HyprlandAPI::removeLuaFunction(gHandle, "hyprveil", "cycle");
    if (gLuaActivePrivacy) HyprlandAPI::removeLuaFunction(gHandle, "hyprveil", "active_privacy");
    if (gLuaSetHidden) HyprlandAPI::removeLuaFunction(gHandle, "hyprveil", "set_hidden");
    if (gLuaToggle) HyprlandAPI::removeLuaFunction(gHandle, "hyprveil", "toggle");
    if (gLuaResetSharing) HyprlandAPI::removeLuaFunction(gHandle, "hyprveil", "reset_sharing");
    gLuaConfigure = gLuaStatus = gLuaMode = gLuaCycle = false;
    gLuaActivePrivacy = gLuaSetHidden = gLuaToggle = gLuaResetSharing = false;
    if (gModeDispatcher) HyprlandAPI::removeDispatcher(gHandle, "hyprveil:mode");
    if (gCycleDispatcher) HyprlandAPI::removeDispatcher(gHandle, "hyprveil:cycle");
    gModeDispatcher = gCycleDispatcher = false;
    if (gConfigWriter) lua_close(gConfigWriter);
    gConfigWriter = nullptr;
    gNativeModeParser = nullptr;
    // This also destroys the plugin-owned strict-parser vtables before dlclose.
    if (Config::mgr()) Config::mgr()->onPluginUnload(gHandle);
    gRegisteredConfig.clear();
    gEnabled = false;
    gDumpPending = false;
    if (gSpoilerTimer) {
        gSpoilerTimer->cancel();
        if (g_pEventLoopManager)
            g_pEventLoopManager->removeTimer(gSpoilerTimer);
        gSpoilerTimer.reset();
    }
    gAnimatedCaptures.clear();
    // Signal handlers live in this DSO. Release them before hooks/code unload.
    gWindowRulesListener.reset();
    gWindowCloseListener.reset();
    gWindowOpenListener.reset();
    gRoleWatches.clear();
    gLayerRulesListener.reset();
    gConfigReloadListener.reset();
    gLockListener.reset();
    gUnlockListener.reset();
    // A native frame can outlive this plugin while its permission prompt is
    // pending. Never leave it a snapshot from an earlier capture policy.
    if (g_pCompositor && !g_pCompositor->m_isShuttingDown && g_pHyprRenderer &&
        (g_pHyprRenderer->type() != Renderer::RT_GL || Render::GL::g_pHyprOpenGL)) {
        for (const auto& [key, policy] : gSessionPolicies) {
            if (const auto* session = policy.session.get())
                invalidateSnapshot(session);
        }
    }
    gSessionPolicies.clear();
    gOrphanPrivacy.clear();
    gParentDiagnostics.clear();
    gImage.reset();
    // A rendered pass can retain custom objects until the next clear().
    // Destroy plugin vtables while this DSO is still mapped.
    if (gSpoilerShader && Render::GL::g_pHyprOpenGL)
        Render::GL::g_pHyprOpenGL->makeEGLCurrent();
    if (g_pHyprRenderer)
        g_pHyprRenderer->m_renderPass.removeAllOfType(Hyprveil::Spoiler::PASS_NAME);
    gSpoilerShader.reset();
    gSpoilerEye.reset();
    gLoadedImagePath.clear();
    if (gCommand) {
        HyprlandAPI::unregisterHyprCtlCommand(gHandle, gCommand);
        gCommand.reset();
    }
    for (auto it = gHooks.rbegin(); it != gHooks.rend(); ++it)
        HyprlandAPI::removeFunctionHook(gHandle, *it);
    gHooks.clear();
}

void damageCaptureScene() {
    // Continuous consumers using copy_with_damage otherwise keep the previous
    // frame indefinitely when only the capture policy changes in a static
    // desktop. Invalidate and schedule the normal outputs without touching
    // window geometry, visibility, or the native noScreenShare fallback.
    for (const auto& monitor : State::monitorState()->monitors()) {
        if (!monitor || !monitor->enabled() || monitor->isMirror())
            continue;
        g_pHyprRenderer->damageMonitor(monitor);
        monitor->scheduleFrame(Aquamarine::IOutput::AQ_SCHEDULE_DAMAGE);
    }
}

void capturePolicyChanged() {
    ++gPolicyGeneration;
    damageCaptureScene();
}

std::string quotedJson(const std::string& value) {
    constexpr char hex[] = "0123456789abcdef";
    std::string result = "\"";
    for (const auto raw : value) {
        const auto c = static_cast<unsigned char>(raw);
        if (c == '\\' || c == '"') {
            result += '\\';
            result += static_cast<char>(c);
        } else if (c < 0x20) {
            result += "\\u00";
            result += hex[c >> 4];
            result += hex[c & 15];
        } else
            result += static_cast<char>(c);
    }
    return result + '"';
}

std::string fixturePrivacy() {
    if (gLiveSession)
        return R"({"error":"fixture-privacy is disabled in live sessions"})";
    std::string result = "{\"windows\":[";
    std::size_t count = 0;
    for (const auto& window : Desktop::windowState()->windows()) {
        if (!window->m_class.starts_with("org.hyprveil.fixture."))
            continue;
        if (count == 100)
            break;
        if (count++)
            result += ',';
        const auto parent = window->parent();
        const auto protocolParent = !window->m_isX11 && window->m_xdgSurface && window->m_xdgSurface->m_toplevel ?
            window->m_xdgSurface->m_toplevel->m_parent.lock() : nullptr;
        result += "{\"address\":" + quotedJson(std::format("0x{:x}", reinterpret_cast<std::uintptr_t>(window.get()))) +
            ",\"title\":" + quotedJson(window->m_title) + ",\"class\":" + quotedJson(window->m_class) +
            ",\"mapped\":" + (window->m_isMapped ? "true" : "false") +
            ",\"parent\":" + quotedJson(std::format("0x{:x}", reinterpret_cast<std::uintptr_t>(parent.get()))) +
            ",\"protocol_parent_resource\":" + quotedJson(std::format("0x{:x}", reinterpret_cast<std::uintptr_t>(protocolParent.get()))) +
            ",\"protocol_parent_window\":" + quotedJson(std::format("0x{:x}", reinterpret_cast<std::uintptr_t>(protocolParent ? protocolParent->m_window.get() : nullptr))) +
            ",\"protocol_toplevel_resource\":" + quotedJson(std::format("0x{:x}", reinterpret_cast<std::uintptr_t>(!window->m_isX11 && window->m_xdgSurface ? window->m_xdgSurface->m_toplevel.get() : nullptr))) +
            ",\"native_private\":" + (window->m_ruleApplicator->noScreenShare().valueOrDefault() ? "true" : "false") +
            ",\"effective_private\":" + (privateWindow(window) ? "true" : "false") +
            ",\"retained_private\":" + (retainedPrivacy(window) ? "true" : "false") + '}';
    }
    result += "],\"parent_events\":[";
    count = 0;
    for (const auto& event : gParentDiagnostics) {
        if (count++)
            result += ',';
        result += std::format("{{\"self\":\"0x{:x}\",\"argument\":\"0x{:x}\",\"window\":\"0x{:x}\",\"argument_window\":\"0x{:x}\",\"before_parent\":\"0x{:x}\",\"after_parent\":\"0x{:x}\",\"after_impl\":\"0x{:x}\",\"after_data\":\"0x{:x}\",\"before_private\":{},\"after_private\":{}}}",
            event.self, event.argument, event.window, event.argumentWindow, event.beforeParent, event.afterParent, event.afterImpl, event.afterData,
            event.beforePrivate ? "true" : "false", event.afterPrivate ? "true" : "false");
    }
    return result + "]}";
}

std::string activePrivacy() {
    // Snapshot both focus and effective protection in the compositor event
    // loop. External UI never needs a racing activewindow/getprop pair, window
    // titles or synchronous Lua IPC. This command changes no policy.
    const auto window = Desktop::focusState()->window();
    if (!window || !window->m_isMapped || !window->m_ruleApplicator)
        return R"({"state":"none","address":"","stable_id":"","native_private":false,"inherited":false})";
    const bool native = window->m_ruleApplicator->noScreenShare().valueOrDefault();
    const bool effective = privateWindow(window);
    return "{\"state\":\"" + std::string{effective ? "hidden" : "visible"} +
           "\",\"address\":\"" + std::format("0x{:x}", reinterpret_cast<std::uintptr_t>(window.get())) +
           "\",\"stable_id\":\"" + std::to_string(window->m_stableID) +
           "\",\"native_private\":" + (native ? "true" : "false") +
           ",\"inherited\":" + (effective && !native ? "true" : "false") + "}";
}

std::string command(eHyprCtlOutputFormat, std::string request) {
    if (gConfigReady) syncNativeConfiguration();
    std::expected<void, std::string> updated;
    if (request == "hyprveil active-privacy")
        return activePrivacy();
    if (request == "hyprveil fixture-privacy")
        return fixturePrivacy();
    if (request == "hyprveil off" || request == "hyprveil black") {
        // "off" means a secure black replacement while loaded. Native mirror
        // masking can leak transformed window edges, so never delegate it here.
        updated = updateNativeConfiguration({{"mode", std::string{"black"}}});
    }
    else if (request == "hyprveil on" || request == "hyprveil omit") {
        updated = updateNativeConfiguration({{"mode", std::string{"omit"}}});
    }
    else if (request == "hyprveil spoiler") {
        updated = updateNativeConfiguration({{"mode", std::string{"spoiler"}}});
    }
    else if (request.starts_with("hyprveil appearance ")) {
        const auto value = Hyprveil::Appearance::parse(std::string_view{request}.substr(20));
        if (!value)
            return R"({"error":"appearance requires variant hex-color grain[0..100] speed[0..200] darkness[0..100] eye[0|1] eye-size[40..128]"})";
        auto patch = NativeSettings{.appearance=*value}.values();
        patch.erase("mode");
        patch.erase("image_path");
        updated = updateNativeConfiguration(patch);
    }
    else if (request == "hyprveil appearance") {
        // Query only. Appearance never changes window privacy or mode.
    }
    else if (request.starts_with("hyprveil image ")) {
        const auto path = request.substr(std::string{"hyprveil image "}.size());
        // A repeated image command also reloads its bounded file snapshot.
        updated = updateNativeConfiguration({{"mode", std::string{"image"}}, {"image_path", path}}, true);
    }
    else if (request == "hyprveil dump-local") {
        if (gLiveSession)
            return R"({"error":"dump-local is disabled in live sessions"})";
        gDumpPending = true;
    }
    else if (request != "hyprveil" && request != "hyprveil status")
        return R"({"error":"usage: hyprveil [status|omit|black|spoiler|image ABSOLUTE_PATH|dump-local]"})";
    if (!updated) return "{\"error\":" + quotedJson(updated.error()) + "}";
    const auto pendingSnapshots = std::count_if(gSessionPolicies.begin(), gSessionPolicies.end(), [](const auto& entry) {
        const auto* session = entry.second.session.get();
        if (!session)
            return false;
        const auto& snapshot = session->*member(PermissionSnapshot{});
        return snapshot && snapshot->isAllocated();
    });
    return std::string{"{\"config_api\":1,\"image_path\":"} + quotedJson(gImagePath) + ",\"session\":\"" + gSession + "\",\"mode\":\"" + (gEnabled ? gMode : "black") + "\",\"scene_frames\":" + std::to_string(gSceneFrames) +
           ",\"fallback_frames\":" + std::to_string(gFallbackFrames) + ",\"omitted_window_passes\":" + std::to_string(gOmittedWindowPasses) +
           ",\"omitted_layer_passes\":" + std::to_string(gOmittedLayerPasses) + ",\"invalidated_snapshots\":" +
           std::to_string(gInvalidatedSnapshots) + ",\"pending_snapshots\":" + std::to_string(pendingSnapshots) +
           ",\"policy_generation\":" + std::to_string(gPolicyGeneration) + ",\"local_dump\":\"" +
           (gDumpPending ? "pending" : gDumpResult) + "\",\"image_status\":\"" + gImageStatus +
           "\",\"spoiler_status\":\"" + gSpoilerStatus + "\",\"spoiler_shader_attempts\":" +
           std::to_string(gSpoilerShaderAttempts) + ",\"spoiler_animation_armed\":" +
           (gSpoilerTimer && gSpoilerTimer->armed() ? "true" : "false") + ",\"appearance\":" + gAppearance.json() + "}";
}
} // namespace

// One-shot, exact-ABI handoff from the separately tested temporary upgrade
// guard. Weak references preserve identity without keeping closed windows
// alive. Captures remain suspended until this function completes successfully.
APICALL EXPORT void HYPRVEIL_ADOPT_V1(const std::vector<PHLWINDOWREF>& windows) {
    if (!gEnabled || gMode != "black" || windows.size() > 4096)
        throw std::runtime_error("privacy handoff requires initialized black mode");
    for (const auto& reference : windows) {
        const auto window = reference.lock();
        if (!window || !window->m_isMapped || !window->m_ruleApplicator ||
            window->m_ruleApplicator->noScreenShare().valueOrDefault())
            continue;
        gOrphanPrivacy[window.get()] = window;
    }
    capturePolicyChanged();
}

APICALL EXPORT std::string PLUGIN_API_VERSION() {
    return HYPRLAND_API_VERSION;
}

APICALL EXPORT PLUGIN_DESCRIPTION_INFO PLUGIN_INIT(HANDLE handle) {
    gHandle = handle;
    if (std::string{__hyprland_api_get_hash()} != __hyprland_api_get_client_hash())
        throw std::runtime_error("hyprveil: exact Hyprland ABI mismatch; rebuild for this compositor");
    // Validate the ABI before reading any compositor object fields.
    requireSession();

    // Live admission starts in capture-only black replacement. The native
    // surface is omitted even if a local transformer expands it beyond its box.
    gEnabled = true;
    gMode = "black";
    try {
        registerNativeConfiguration();
        // Exact exports from the installed v0.56.2 ELF; readable names document
        // the interception points. Missing/changed exports refuse initialization.
        gSetPropHook = makeHook("Config::Actions::setProp",
            "_ZN6Config7Actions7setPropERKNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEES8_St8optionalIN9Hyprutils6Memory14CSharedPointerIN7Desktop4View7CWindowEEEE",
            reinterpret_cast<const void*>(setPropHook));
        gParentHook = makeHook("CXDGToplevelResource::setNewParent",
            "_ZN20CXDGToplevelResource12setNewParentEN9Hyprutils6Memory14CSharedPointerIS_EE",
            reinterpret_cast<const void*>(parentHook));
        gX11PropertyHook = makeHook("CXWM::readProp (WM_TRANSIENT_FOR only)",
            "_ZN4CXWM8readPropEN9Hyprutils6Memory14CSharedPointerI16CXWaylandSurfaceEEjP24xcb_get_property_reply_t",
            reinterpret_cast<const void*>(x11PropertyHook));
        gWindowHook = makeHook("Render::IHyprRenderer::renderWindow",
            "_ZN6Render13IHyprRenderer12renderWindowEN9Hyprutils6Memory14CSharedPointerIN7Desktop4View7CWindowEEENS3_IN7Monitor8CMonitorEEERKNSt6chrono10time_pointINSB_3_V212steady_clockENSB_8durationIlSt5ratioILl1ELl1000000000EEEEEEbNS_15eRenderPassModeEbb",
            reinterpret_cast<const void*>(windowHook));
        gLayerHook = makeHook("Render::IHyprRenderer::renderLayer",
            "_ZN6Render13IHyprRenderer11renderLayerEN9Hyprutils6Memory14CSharedPointerIN7Desktop4View13CLayerSurfaceEEENS3_IN7Monitor8CMonitorEEERKNSt6chrono10time_pointINSB_3_V212steady_clockENSB_8durationIlSt5ratioILl1ELl1000000000EEEEEEbb",
            reinterpret_cast<const void*>(layerHook));
        gFadeoutHook = makeHook("Render::IHyprRenderer::renderFadeouts",
            "_ZN6Render13IHyprRenderer14renderFadeoutsEN9Hyprutils6Memory14CSharedPointerIN7Monitor8CMonitorEEEN7Desktop13eFadeoutPlaneENS3_I10CWorkspaceEE",
            reinterpret_cast<const void*>(fadeoutHook));
        gPassHook = makeHook("Render::CRenderPass::add",
            "_ZN6Render11CRenderPass3addEON9Hyprutils6Memory14CUniquePointerI12IPassElementEE",
            reinterpret_cast<const void*>(passHook));
        gDragHook = makeHook("Render::IHyprRenderer::renderDragIcon",
            "_ZN6Render13IHyprRenderer14renderDragIconEN9Hyprutils6Memory14CSharedPointerIN7Monitor8CMonitorEEERKNSt6chrono10time_pointINS7_3_V212steady_clockENS7_8durationIlSt5ratioILl1ELl1000000000EEEEEE",
            reinterpret_cast<const void*>(dragHook));
        gCopyHook = makeHook("Screenshare::CScreenshareFrame::copy",
            "_ZN11Screenshare17CScreenshareFrame4copyEv", reinterpret_cast<const void*>(copyHook));
        gFrameRenderHook = makeHook("Screenshare::CScreenshareFrame::render",
            "_ZN11Screenshare17CScreenshareFrame6renderEv", reinterpret_cast<const void*>(frameRenderHook));
        // Install the interception point last: every skip hook must already be
        // active before capture can select the alternate renderer.
        gMonitorHook = makeHook("Screenshare::CScreenshareFrame::renderMonitor",
            "_ZN11Screenshare17CScreenshareFrame13renderMonitorEv", reinterpret_cast<const void*>(monitorHook));
        for (auto* hook : gHooks) {
            if (!hook->hook())
                throw std::runtime_error("hyprveil: internal function hook could not be installed");
        }
        gCommand = HyprlandAPI::registerHyprCtlCommand(gHandle, {.name = "hyprveil", .exact = false, .fn = [](eHyprCtlOutputFormat format, std::string request) {
            try { return command(format, std::move(request)); }
            catch (...) { failNativeConfiguration(); return std::string{R"({"error":true})"}; }
        }});
        if (!gCommand)
            throw std::runtime_error("hyprveil: could not register status command");
        gLuaConfigure = HyprlandAPI::addLuaFunction(gHandle, "hyprveil", "configure", luaNativeConfigure);
        gLuaStatus = HyprlandAPI::addLuaFunction(gHandle, "hyprveil", "status", luaNativeStatus);
        if (!gLuaConfigure || !gLuaStatus)
            throw std::runtime_error("hyprveil: Lua callback registration failed");
        if (!g_pKeybindManager || g_pKeybindManager->m_dispatchers.contains("hyprveil:mode") ||
            g_pKeybindManager->m_dispatchers.contains("hyprveil:cycle"))
            throw std::runtime_error("hyprveil: native dispatcher name collision");
        gModeDispatcher = HyprlandAPI::addDispatcherV2(gHandle, "hyprveil:mode", [](std::string mode) -> SDispatchResult {
            try {
                const auto result = updateNativeConfiguration({{"mode", std::move(mode)}});
                return {.success=bool(result), .error=result ? "" : result.error()};
            } catch (...) { failNativeConfiguration(); return {.success=false, .error="Hyprveil mode change failed"}; }
        });
        gCycleDispatcher = HyprlandAPI::addDispatcherV2(gHandle, "hyprveil:cycle", [](std::string argument) -> SDispatchResult {
            try {
                if (!argument.empty()) return {.success=false, .error="hyprveil:cycle expects no arguments"};
                const auto mode = readNativeConfiguration().mode;
                const auto result = updateNativeConfiguration({{"mode", std::string{mode == "black" ? "spoiler" : mode == "spoiler" ? "omit" : "black"}}});
                return {.success=bool(result), .error=result ? "" : result.error()};
            } catch (...) { failNativeConfiguration(); return {.success=false, .error="Hyprveil cycle failed"}; }
        });
        if (!gModeDispatcher || !gCycleDispatcher)
            throw std::runtime_error("hyprveil: native dispatcher registration failed");
        gLuaMode = HyprlandAPI::addLuaFunction(gHandle, "hyprveil", "mode", luaNativeMode);
        gLuaCycle = HyprlandAPI::addLuaFunction(gHandle, "hyprveil", "cycle", luaNativeCycle);
        if (!gLuaMode || !gLuaCycle)
            throw std::runtime_error("hyprveil: Lua dispatcher bridge registration failed");
        gLuaActivePrivacy = HyprlandAPI::addLuaFunction(gHandle, "hyprveil", "active_privacy", luaActivePrivacy);
        gLuaSetHidden = HyprlandAPI::addLuaFunction(gHandle, "hyprveil", "set_hidden", luaSetHidden);
        gLuaToggle = HyprlandAPI::addLuaFunction(gHandle, "hyprveil", "toggle", luaTogglePrivacy);
        gLuaResetSharing = HyprlandAPI::addLuaFunction(gHandle, "hyprveil", "reset_sharing", luaResetSharing);
        if (!gLuaActivePrivacy || !gLuaSetHidden || !gLuaToggle || !gLuaResetSharing)
            throw std::runtime_error("hyprveil: native privacy callback registration failed");
        // A native privacy tag/rule changes capture pixels without changing the
        // local scene. copy_with_damage needs an explicit invalidation even
        // when decoration/layout updates produce no normal monitor damage.
        gWindowRulesListener = Event::bus()->m_events.window.updateRules.listen([](PHLWINDOW) { capturePolicyChanged(); });
        gWindowCloseListener = Event::bus()->m_events.window.close.listen(closingWindow);
        gWindowOpenListener = Event::bus()->m_events.window.openEarly.listen([](PHLWINDOW window) { watchWindowRole(window); });
        for (const auto& window : Desktop::windowState()->windows())
            watchWindowRole(window);
        gLayerRulesListener = Event::bus()->m_events.layer.updateRules.listen([](PHLLS) { capturePolicyChanged(); });
        gConfigPreReloadListener = Event::bus()->m_events.config.preReload.listen([]() {
            gConfigReady = false;
            gMode = "black";
            gAnimatedCaptures.clear();
            if (gSpoilerTimer) gSpoilerTimer->updateTimeout(std::nullopt);
            try { capturePolicyChanged(); } catch (...) {}
        });
        gConfigReloadListener = Event::bus()->m_events.config.reloaded.listen([]() {
            gConfigReady = true;
            try {
                if (Config::mgr() && !Config::mgr()->configVerifPassed()) {
                    const auto black = updateNativeConfiguration({{"mode", std::string{"black"}}});
                    if (!black) failNativeConfiguration();
                } else nativeConfigParsed();
            } catch (...) {
                failNativeConfiguration();
            }
            // Even equal settings can accompany changed privacy rules.
            try { capturePolicyChanged(); } catch (...) {}
        });
        gLockListener = g_pSessionLockManager->m_events.lock.listen([]() { capturePolicyChanged(); });
        gUnlockListener = g_pSessionLockManager->m_events.unlock.listen([]() { capturePolicyChanged(); });
        gSpoilerTimer = makeShared<CEventLoopTimer>(std::nullopt, [](SP<CEventLoopTimer> timer, void*) {
            if (!gEnabled || gMode != "spoiler" || !gAppearance.animated() || !g_pHyprRenderer || g_pCompositor->m_isShuttingDown ||
                g_pSessionLockManager->isSessionLocked()) {
                gAnimatedCaptures.clear();
                return;
            }
            const auto now = Time::steadyNow();
            std::erase_if(gAnimatedCaptures, [&](const auto& item) {
                const auto monitor = item.monitor.lock();
                return !monitor || !monitor->enabled() || now - item.captured > std::chrono::milliseconds{400};
            });
            for (const auto& item : gAnimatedCaptures) {
                if (const auto monitor = item.monitor.lock()) {
                    g_pHyprRenderer->damageMonitor(monitor);
                    monitor->scheduleFrame(Aquamarine::IOutput::AQ_SCHEDULE_DAMAGE);
                }
            }
            // Damage wakes copy_with_damage; style/privacy generations change
            // only for real policy changes, never for animation ticks.
            if (!gAnimatedCaptures.empty())
                timer->updateTimeout(std::chrono::milliseconds{Hyprveil::Spoiler::FRAME_MS});
        }, nullptr);
        g_pEventLoopManager->addTimer(gSpoilerTimer);
        gEnabled = true;
        gConfigReady = true;
    } catch (...) {
        cleanup();
        throw;
    }
    return {"hyprveil", "Capture-only privacy with native typed Lua configuration; exact Hyprland ABI required", "OBJLAKO", "0.4.0"};
}

APICALL EXPORT void PLUGIN_EXIT() {
    cleanup();
}
