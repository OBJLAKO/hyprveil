#pragma once

// User assets are data. Never expose a window texture, a mirror, a framebuffer
// or an inherited texture binding to a procedural user shader.
#include <algorithm>
#include <array>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <expected>
#include <string>
#include <string_view>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace Hyprveil::Assets {
inline constexpr std::size_t MAX_SHADER_BYTES = 64U * 1024U;

// Walk held directory descriptors: neither a final symlink nor a concurrent
// parent-directory replacement may redirect a previously validated asset.
inline int openAsset(const std::string& path) {
    if (path.size() < 2 || path.front() != '/' || path.size() > 4096) return -1;
    int directory = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (directory < 0) return -1;
    std::size_t begin = 1;
    while (begin < path.size()) {
        const auto end = path.find('/', begin);
        const auto part = path.substr(begin, end == std::string::npos ? end : end - begin);
        if (part.empty() || part == "." || part == "..") { close(directory); return -1; }
        const bool last = end == std::string::npos;
        const int next = openat(directory, part.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK |
                               (last ? 0 : O_DIRECTORY));
        close(directory);
        if (next < 0 || last) return next;
        directory = next;
        begin = end + 1;
    }
    close(directory);
    return -1;
}

inline std::expected<std::string, std::string> readShader(const std::string& path) {
    if (path.empty() || path.front() != '/' || path.size() > 4096)
        return std::unexpected("invalid shader path");
    const int fd = openAsset(path);
    if (fd < 0) return std::unexpected("shader unavailable");
    struct Close { int fd; ~Close() { close(fd); } } closeFile{fd};
    struct stat info {};
    if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_size <= 0 ||
        static_cast<std::uint64_t>(info.st_size) > MAX_SHADER_BYTES)
        return std::unexpected("shader must be a bounded regular file");
    std::string source(static_cast<std::size_t>(info.st_size), '\0');
    std::size_t offset = 0;
    while (offset < source.size()) {
        const auto count = read(fd, source.data() + offset, source.size() - offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return std::unexpected("shader changed while reading");
        offset += static_cast<std::size_t>(count);
    }
    char extra;
    ssize_t count;
    do { count = read(fd, &extra, 1); } while (count < 0 && errno == EINTR);
    if (count != 0 || source.find('\0') != std::string::npos)
        return std::unexpected("shader changed or contains NUL");
    return source;
}

// The public shader API is a function, not a replacement fragment entry point.
// The wrapper controls opaque output and rounding. Comments are ignored when
// checking tokens; preprocessors and opaque resources cannot escape this API.
inline std::expected<std::string, std::string> wrapShader(std::string_view source) {
    if (source.empty() || source.size() > MAX_SHADER_BYTES || source.find('\0') != std::string_view::npos)
        return std::unexpected("invalid shader source length");
    std::string code;
    for (std::size_t i = 0; i < source.size();) {
        if (source.substr(i, 2) == "//") {
            const auto end = source.find('\n', i + 2);
            i = end == std::string_view::npos ? source.size() : end;
        } else if (source.substr(i, 2) == "/*") {
            const auto end = source.find("*/", i + 2);
            if (end == std::string_view::npos) return std::unexpected("unterminated shader comment");
            code += ' ';
            i = end + 2;
        } else code += source[i++];
    }
    const auto word = [](unsigned char c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                                                   (c >= '0' && c <= '9') || c == '_'; };
    for (std::size_t i = 0; i < code.size();) {
        if (code[i] == '#' || code[i] == '\\') return std::unexpected("shader preprocessors are unavailable");
        if (!word(code[i])) { ++i; continue; }
        const auto begin = i;
        while (i < code.size() && word(code[i])) ++i;
        const auto token = std::string_view{code}.substr(begin, i - begin);
        if (token.find("sampler") != std::string_view::npos || token == "uniform" ||
            token.starts_with("image") || token.starts_with("atomic") || token == "buffer")
            return std::unexpected("user shaders cannot access scene resources");
    }
    return std::string{R"GLSL(#version 300 es
precision highp float;
precision highp int;
uniform vec2 fullSize;
uniform float time;
uniform vec3 tint;
uniform float grainAmount;
uniform float darkness;
uniform vec3 maskColor;
uniform float radius;
uniform float roundingPower;
in vec2 v_texcoord;
out vec4 fragColor;
)GLSL"} + std::string{source} + R"GLSL(
void main() {
    vec2 uv = clamp(v_texcoord, 0.0, 1.0);
    float r = clamp(radius, 0.0, min(fullSize.x, fullSize.y) * .5);
    vec2 q = abs((uv - .5) * fullSize) - (fullSize * .5 - r);
    float power = max(roundingPower, 1.0);
    vec2 corner = max(q, 0.0);
    float d = pow(pow(corner.x, power) + pow(corner.y, power), 1.0 / power) + min(max(q.x, q.y), 0.0) - r;
    if (d > 0.0) discard;
    vec4 value = hyprveil(uv);
    if (any(isnan(value.rgb)) || any(isinf(value.rgb))) value.rgb = maskColor;
    fragColor = vec4(clamp(value.rgb, 0.0, 1.0), 1.0);
}
)GLSL";
}

// A single texture sample per fragment, regardless of tiling density. The
// only texture bound for this pass is the validated, user-supplied PNG.
inline constexpr const char* IMAGE_FRAGMENT = R"GLSL(#version 300 es
precision highp float;
precision highp int;
uniform vec2 fullSize;
uniform vec2 imageSize;
uniform sampler2D maskImage;
uniform int imageFit;
uniform vec3 maskColor;
uniform float radius;
uniform float roundingPower;
in vec2 v_texcoord;
out vec4 fragColor;
void main() {
    vec2 uv = clamp(v_texcoord, 0.0, 1.0);
    float r = clamp(radius, 0.0, min(fullSize.x, fullSize.y) * .5);
    vec2 q = abs((uv - .5) * fullSize) - (fullSize * .5 - r);
    float power = max(roundingPower, 1.0);
    vec2 corner = max(q, 0.0);
    float d = pow(pow(corner.x, power) + pow(corner.y, power), 1.0 / power) + min(max(q.x, q.y), 0.0) - r;
    if (d > 0.0) discard;
    vec2 sampleUV = uv;
    bool outside = false;
    if (imageFit == 1 || imageFit == 2) {
        vec2 ratio = fullSize / max(imageSize, vec2(1.0));
        float scale = imageFit == 1 ? max(ratio.x, ratio.y) : min(ratio.x, ratio.y);
        vec2 shown = max(imageSize * scale, vec2(.001));
        sampleUV = (uv * fullSize - (fullSize - shown) * .5) / shown;
        outside = any(lessThan(sampleUV, vec2(0.0))) || any(greaterThan(sampleUV, vec2(1.0)));
    } else if (imageFit == 3) sampleUV = fract(uv * fullSize / max(imageSize, vec2(1.0)));
    vec4 pixel = outside ? vec4(0.0) : texture(maskImage, sampleUV);
    // Cairo assets are premultiplied. Transparent pixels reveal the chosen
    // synthetic fill, never the protected window or a cached monitor texture.
    fragColor = vec4(clamp(pixel.rgb + maskColor * (1.0 - pixel.a), 0.0, 1.0), 1.0);
}
)GLSL";

inline int imageFit(std::string_view value) {
    return value == "fill" ? 1 : value == "fit" ? 2 : value == "tile" ? 3 : 0;
}
} // namespace Hyprveil::Assets
