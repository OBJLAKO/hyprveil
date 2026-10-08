#pragma once

// Every input is synthetic: coordinates, dimensions and bounded time. No
// window texture, buffer, screenshot, title or blur cache enters this shader.
#include <cstdint>
#include <cairo/cairo.h>

namespace Hyprveil::Spoiler {
inline constexpr int EYE_SIZE = 128;
inline constexpr int FRAME_MS = 42;
inline constexpr const char* PASS_NAME = "CHyprveilSatinPass";
inline float seconds(std::uint64_t milliseconds) {
    return static_cast<float>(milliseconds % 3600000) / 1000.0F;
}
inline constexpr const char* VERTEX = R"GLSL(#version 300 es
uniform mat3 proj;
in vec2 pos;
in vec2 texcoord;
out vec2 v_texcoord;
void main() {
    gl_Position = vec4(proj * vec3(pos, 1.0), 1.0);
    v_texcoord = texcoord;
}
)GLSL";
inline constexpr const char* FRAGMENT = R"GLSL(#version 300 es
precision highp float;
uniform vec2 fullSize;
uniform float time;
uniform vec3 tint;
uniform float grainAmount;
uniform float darkness;
uniform int variant;
in vec2 v_texcoord;
out vec4 fragColor;

float hash(vec2 p) {
    vec3 q = fract(vec3(p.xyx) * vec3(.1031, .1030, .0973));
    q += dot(q, q.yzx + 33.33);
    return fract((q.x + q.y) * q.z);
}
float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x),
               mix(hash(i + vec2(0, 1)), hash(i + vec2(1)), f.x), f.y);
}
void main() {
    vec2 uv = v_texcoord;
    vec2 px = uv * fullSize;
    float t = time * .065;
    // Diffused, slowly drifting light in a cool graphite / warm pearl field.
    // Low frequency layers create satin depth without revealing real pixels.
    vec2 flow = vec2(sin(t), cos(t * .8));
    float fog = noise(uv * vec2(3.0, 2.6) + flow * .42);
    fog = .68 * fog + .32 * noise(uv * 6.0 - flow * .3);
    float fold = sin(uv.x * 4.8 + uv.y * 3.1 + fog * 2.8 + t);
    fold = smoothstep(-.75, .9, fold);
    vec2 warmPos = vec2(.22 + .17 * sin(t), .24 + .12 * cos(t * .7));
    vec2 coolPos = vec2(.76 + .12 * cos(t * .8), .68 + .13 * sin(t));
    float warm = exp(-dot((uv - warmPos) * vec2(1.25, 1.65),
                         (uv - warmPos) * vec2(1.25, 1.65)) * 3.2);
    float cool = exp(-dot((uv - coolPos) * vec2(1.55, 1.15),
                         (uv - coolPos) * vec2(1.55, 1.15)) * 3.6);
    vec3 color = vec3(.055, .063, .072);
    color += fog * vec3(.042, .045, .048);
    color += fold * vec3(.021, .023, .025);
    color += warm * vec3(.070, .060, .044);
    color += cool * vec3(.035, .047, .058);
    // Grain stays subpixel/fine at any window size; two hashes interpolate
    // instead of flashing bright particles or stretching a bitmap.
    float tick = time * 24.0;
    float grain = mix(hash(floor(px) + floor(tick) * vec2(13, 7)),
                      hash(floor(px) + (floor(tick) + 1.0) * vec2(13, 7)), fract(tick));
    color += (grain - .5) * .060 * grainAmount;
    if (variant == 1) {
        // Telegram-like fine drifting dust over an opaque frosted field.
        // Cell size is physical pixels, independent of the window dimensions.
        // The two soft lobes avoid hard square pixels and oversized stars.
        vec2 p = (px + vec2(sin(time * .31) * 2.4, time * .75)) / 3.2;
        vec2 cell = floor(p);
        float identity = hash(cell);
        vec2 center = vec2(hash(cell + vec2(7, 19)), hash(cell + vec2(31, 3))) * .6 + .2;
        vec2 delta = fract(p) - center;
        float distance2 = dot(delta, delta);
        float flicker = .35 + .65 * pow(.5 + .5 * sin(time * 1.25 + identity * 28.0), 2.0);
        float dust = (exp(-distance2 * 38.0) + .20 * exp(-distance2 * 6.0))
                     * smoothstep(.24, .48, identity) * flicker;
        color = vec3(.075, .086, .105) + fog * vec3(.032, .035, .042);
        color += (warm * .016 + cool * .020) + (grain - .5) * .025 * grainAmount;
        color += dust * .23 * grainAmount;
    }
    // Soft vignette and a hairline of light at the perimeter, no frame badge.
    vec2 centered = (uv - .5) * 2.0;
    color *= 1.0 - .17 * dot(centered, centered);
    float edge = min(min(px.x, fullSize.x - px.x), min(px.y, fullSize.y - px.y));
    color += exp(-max(edge, 0.0) * 1.7) * vec3(.035, .035, .032);
    color *= tint * (2.0 * (1.0 - darkness));
    // Darkness changes RGB only. Alpha is always one, including extrema.
    fragColor = vec4(clamp(color, 0.0, 1.0), 1.0);
}
)GLSL";

inline cairo_surface_t* eye() {
    auto* surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, EYE_SIZE, EYE_SIZE);
    auto* paint = cairo_create(surface);
    cairo_set_operator(paint, CAIRO_OPERATOR_CLEAR);
    cairo_paint(paint);
    cairo_set_operator(paint, CAIRO_OPERATOR_OVER);
    cairo_set_source_rgba(paint, .86, .85, .81, .76);
    cairo_set_line_width(paint, 2.1);
    cairo_set_line_cap(paint, CAIRO_LINE_CAP_ROUND);
    cairo_set_line_join(paint, CAIRO_LINE_JOIN_ROUND);
    // Interrupted eye outline leaves the slash clean without a dark badge.
    cairo_move_to(paint, 42, 48);
    cairo_curve_to(paint, 60, 35, 81, 44, 98, 64);
    cairo_curve_to(paint, 93, 71, 88, 76, 82, 79);
    cairo_move_to(paint, 72, 82);
    cairo_curve_to(paint, 57, 87, 40, 79, 30, 64);
    cairo_curve_to(paint, 32, 61, 34, 58, 37, 55);
    cairo_stroke(paint);
    cairo_arc(paint, 64, 64, 11, -2.35, .78);
    cairo_stroke(paint);
    cairo_arc(paint, 64, 64, 11, .95, 3.7);
    cairo_stroke(paint);
    cairo_move_to(paint, 38, 38);
    cairo_line_to(paint, 90, 90);
    cairo_stroke(paint);
    const auto status = cairo_status(paint);
    cairo_destroy(paint);
    if (status != CAIRO_STATUS_SUCCESS || cairo_surface_status(surface) != CAIRO_STATUS_SUCCESS) {
        cairo_surface_destroy(surface);
        return nullptr;
    }
    return surface;
}
} // namespace Hyprveil::Spoiler
