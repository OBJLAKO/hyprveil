#pragma once

// Every input is synthetic: coordinates, dimensions and bounded time. No
// window texture, buffer, screenshot, title or blur cache enters this shader.
#include <cstdint>
#include <string_view>
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
precision highp int;
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
float square(float value) { return value * value; }

// Tiny original 5x7 alphabet. Four 7-bit columns fit in the first uint;
// the fifth is separate. Every shift below is bounded to 0..21 or 0..6.
uvec2 glyphBits(int code) {
    if (code == 48) return uvec2(0x8b268beu, 0x3eu); // 0
    if (code == 49) return uvec2(0x81fe100u, 0x00u); // 1
    if (code == 50) return uvec2(0x93470c2u, 0x46u); // 2
    if (code == 51) return uvec2(0x93264c1u, 0x36u); // 3
    if (code == 52) return uvec2(0xfe48a18u, 0x10u); // 4
    if (code == 65) return uvec2(0x12244feu, 0x7eu); // A
    if (code == 66) return uvec2(0x93264ffu, 0x36u); // B
    if (code == 67) return uvec2(0x83060beu, 0x41u); // C
    if (code == 68) return uvec2(0x83060ffu, 0x3eu); // D
    if (code == 69) return uvec2(0x93264ffu, 0x41u); // E
    if (code == 70) return uvec2(0x12244ffu, 0x01u); // F
    if (code == 71) return uvec2(0x93260beu, 0x79u); // G
    if (code == 72) return uvec2(0x102047fu, 0x7fu); // H
    if (code == 73) return uvec2(0x83fe080u, 0x00u); // I
    if (code == 74) return uvec2(0x7f06020u, 0x01u); // J
    if (code == 75) return uvec2(0x445047fu, 0x41u); // K
    if (code == 76) return uvec2(0x810207fu, 0x40u); // L
    if (code == 77) return uvec2(0x043017fu, 0x7fu); // M
    if (code == 78) return uvec2(0x101017fu, 0x7fu); // N
    if (code == 79) return uvec2(0x83060beu, 0x3eu); // O
    if (code == 80) return uvec2(0x12244ffu, 0x06u); // P
    return uvec2(0u);
}
float glyph(vec2 uv, int code) {
    if (uv.x < 0.0 || uv.x >= 1.0 || uv.y < 0.0 || uv.y >= 1.0) return 0.0;
    vec2 grid = uv * vec2(5.0, 7.0);
    ivec2 cell = clamp(ivec2(floor(grid)), ivec2(0), ivec2(4, 6));
    uvec2 bits = glyphBits(code);
    uint column = cell.x == 4 ? bits.y : (bits.x >> uint(cell.x * 7));
    float ink = float((column >> uint(cell.y)) & 1u);
    vec2 inset = min(fract(grid), 1.0 - fract(grid));
    return ink * smoothstep(.035, .105, min(inset.x, inset.y));
}
float hiddenCaption(vec2 uv) {
    if (uv.x < 0.0 || uv.x >= 1.0) return 0.0;
    int index = int(floor(uv.x * 6.0));
    int code = index == 0 ? 72 : index == 1 ? 73 : index < 4 ? 68 : index == 4 ? 69 : 78;
    return glyph(vec2(fract(uv.x * 6.0) / .76, uv.y), code);
}
float roundBox(vec2 p, vec2 extent, float radius) {
    vec2 q = abs(p) - extent + radius;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - radius;
}
mat2 rotate(float angle) {
    float c = cos(angle), s = sin(angle);
    return mat2(c, -s, s, c);
}
vec3 glassEnvironment(vec2 p, float t) {
    // Refraction is evaluated against this invented studio, never a window.
    float blue = exp(-square((p.y - .24 * sin(p.x * 2.8 + t * .7)) * 5.0));
    float rose = exp(-square((p.x + .28 + .12 * sin(t * .5)) * 4.0));
    float stripe = .5 + .5 * sin(p.x * 16.0 + p.y * 5.0 - t * .6);
    vec3 studio = vec3(.39, .48, .64) + .10 * (p.y + .5);
    studio += blue * vec3(-.14, .18, .28) + rose * vec3(.30, -.08, .12);
    return studio + stripe * vec3(.045, .045, .055);
}
vec3 glassLens(vec3 behind, vec2 p, vec2 center, vec2 extent, float radius, float angle, float t) {
    mat2 rotation = rotate(angle);
    vec2 q = rotation * (p - center);
    float d = roundBox(q, extent, radius);
    float aa = max(fwidth(d), .001);
    float body = 1.0 - smoothstep(-aa, aa, d);
    // Curved normal produces a visible magnification and chromatic rim.
    vec2 n = q / max(extent, vec2(.001));
    n /= max(length(n), .001);
    vec2 normal = transpose(rotation) * n;
    float depth = clamp(-d / max(radius, .001), 0.0, 1.0);
    vec2 refracted = p - (p - center) * .28 + normal * .045 * (1.0 - depth);
    vec3 lens = vec3(glassEnvironment(refracted + normal * .013, t).r,
                     glassEnvironment(refracted, t).g,
                     glassEnvironment(refracted - normal * .013, t).b);
    lens = mix(lens, vec3(.78, .88, .98), .17);
    float edge = exp(-abs(d) * 130.0);
    float innerEdge = exp(-abs(d + .012) * 95.0);
    float grazing = pow(clamp(dot(normal, normalize(vec2(-.7, -.9))) * .5 + .5, 0.0, 1.0), 7.0);
    vec2 highlightPosition = q + vec2(extent.x * .22, extent.y * .48) + .018 * vec2(sin(t), cos(t));
    float highlight = exp(-dot(highlightPosition * vec2(2.5, 7.0), highlightPosition * vec2(2.5, 7.0)) * 7.0);
    lens += vec3(.38, .42, .48) * highlight + innerEdge * vec3(.055, .085, .12);
    float shadow = exp(-max(d, 0.0) * 30.0) * (1.0 - body) * .17;
    vec3 result = mix(behind * (1.0 - shadow), lens, body);
    return result + edge * (.12 + .75 * grazing) * vec3(.72, .89, 1.0);
}

void main() {
    vec2 uv = v_texcoord;
    vec2 px = uv * fullSize;
    vec2 p = (uv - .5) * vec2(fullSize.x / max(fullSize.y, 1.0), 1.0);
    // Motion is low-frequency geometry, never flashing source-derived pixels.
    // Matte's grain is fixed; all other textures freeze exactly at speed zero.
    float t = time * .32;
    float tick = variant == 5 ? 0.0 : time * 18.0;
    float grain = mix(hash(floor(px) + floor(tick) * vec2(13, 7)),
                      hash(floor(px) + (floor(tick) + 1.0) * vec2(13, 7)), fract(tick));
    vec3 color;
    if (variant == 0) {
        // Prism: intersecting angular planes with moving dichroic light.
        // Hard geometry and warm/cool facets distinguish it from Aurora.
        float a = p.x * .85 + p.y * .65 + .22 * sin(t * .8);
        float b = p.y - p.x * .55 + .16 * cos(t * .65);
        float facet = abs(fract(a * 1.35 + t * .10) - .5) * 2.0;
        vec3 spectrum = .5 + .5 * cos(vec3(.2, 2.25, 4.3) + (a + b * .35) * 3.6 + t * .55);
        float ridge = 1.0 - smoothstep(.006, .025, abs(b));
        color = vec3(.050, .048, .085) + spectrum * (.10 + .24 * facet);
        color += step(0.0, b) * vec3(.015, .028, .030);
        color += ridge * vec3(.12, .15, .18);
        color += (grain - .5) * .025 * grainAmount;
    } else if (variant == 1) {
        // Signal: warm phosphor scanlines and a rolling broadcast band.
        // Horizontal structure stays readable even as a small stream tile.
        float scan = .5 + .5 * cos(px.y * 1.5707963);
        float phase = fract(uv.y - t * .11);
        float band = exp(-square((phase - .5) * 7.0));
        float ripple = .5 + .5 * sin(uv.y * 25.0 - t * 2.8 + sin(uv.x * 4.0 + t) * .8);
        color = vec3(.095, .038, .020);
        color += scan * vec3(.030, .012, .005);
        color += band * vec3(.24, .105, .032);
        color += ripple * vec3(.024, .012, .004);
        color += (grain - .5) * .050 * grainAmount;
    } else if (variant == 2) {
        // Aurora: broad teal and violet ribbons in continuous fluid motion.
        float bend = .21 * sin(p.x * 2.5 + t) + .085 * sin(p.x * 4.7 - t * .7);
        float ribbon = exp(-square((p.y - bend + .04) * 4.3));
        float echo = exp(-square((p.y - bend - .24) * 6.0));
        float light = .65 + .35 * sin(p.x * 2.0 + t * 1.3);
        color = vec3(.022, .045, .080);
        color += ribbon * light * vec3(.075, .320, .285);
        color += echo * vec3(.215, .070, .300);
        color += (grain - .5) * .020 * grainAmount;
    } else if (variant == 3) {
        // Contour: living terracotta topography with broad organic cells.
        // Derivatives keep the fine moving contours antialiased under scaling.
        vec2 q = p * 3.1;
        float terrain = .62 * sin(q.x * 1.35 + t * .75) + .40 * cos(q.y * 1.7 - t * .65)
                      + .28 * sin((q.x + q.y) * 1.1 + t * .45);
        float level = terrain * 5.0;
        float distanceToLine = abs(fract(level) - .5);
        float lineWidth = max(fwidth(level), .003);
        float line = 1.0 - smoothstep(lineWidth * .30, lineWidth * 1.4, distanceToLine);
        float land = .5 + .5 * sin(terrain * 1.6);
        color = vec3(.095, .043, .039) + land * vec3(.085, .045, .033);
        color += line * vec3(.220, .130, .090);
        color += (grain - .5) * .025 * grainAmount;
    } else if (variant == 4) {
        // Radar: a rotating sonar sector, concentric rings and precise grid.
        // No loops or texture samples; the sweep reads as motion without noise.
        float radius = length(p);
        float angle = radius > .000001 ? atan(p.y, p.x) : 0.0;
        float sweep = mod(angle - t * 1.8 + 6.2831853, 6.2831853);
        float beam = exp(-sweep * 3.0) * smoothstep(.025, .08, radius);
        float rings = abs(fract(radius * 5.0) - .5);
        float ring = 1.0 - smoothstep(.006, .022, rings);
        vec2 cell = abs(fract(px / 64.0) - .5);
        float grid = 1.0 - smoothstep(.006, .020, .5 - max(cell.x, cell.y));
        float halo = exp(-square((radius - .30 - .035 * sin(t * 1.7)) * 20.0));
        color = vec3(.018, .062, .060);
        color += grid * vec3(.015, .040, .037) + ring * vec3(.026, .075, .065);
        color += beam * vec3(.050, .280, .185) + halo * vec3(.010, .025, .020);
        color += (grain - .5) * .022 * grainAmount;
    } else if (variant == 5) {
        // Matte: still mineral graphite for the quiet end of the collection.
        float mineral = noise(px * .017);
        color = vec3(.105, .112, .117);
        color += (mineral - .5) * vec3(.025, .027, .026);
        color += (1.0 - uv.y) * vec3(.018, .017, .014);
        color += (grain - .5) * .036 * grainAmount;
    } else if (variant == 6) {
        // 404: actual bitmap typography, with a rare travelling glitch slice.
        // The headline remains readable throughout the short broadcast cycle.
        float cycle = fract(time * .42);
        float glitch = smoothstep(.72, .78, cycle) * (1.0 - smoothstep(.88, .96, cycle));
        float row = floor(uv.y * 28.0);
        float slice = step(.70, hash(vec2(row, floor(time * 9.0))));
        vec2 q = p;
        q.x += glitch * slice * (hash(vec2(row, 7.0)) - .5) * .12;
        float scale = min(1.0, fullSize.x / max(fullSize.y, 1.0) / 1.35);
        vec2 headline = (q / max(scale, .001) + vec2(.51, .22)) / vec2(1.02, .39);
        float index = floor(headline.x * 3.0);
        float digits = index >= 0.0 && index < 3.0 ?
            glyph(vec2(fract(headline.x * 3.0) / .84, headline.y), index == 1.0 ? 48 : 52) : 0.0;
        float caption = hiddenCaption((q / max(scale, .001) + vec2(.24, -.245)) / vec2(.48, .055));
        float scan = .5 + .5 * cos(px.y * 1.5707963);
        float sweep = exp(-square((uv.y - fract(time * .18)) * 24.0));
        float rule = (1.0 - smoothstep(.001, .003, abs(p.y / max(scale, .001) - .215)))
                   * (1.0 - smoothstep(.48, .51, abs(p.x / max(scale, .001))));
        color = vec3(.026, .022, .065) + scan * vec3(.007, .008, .014);
        color += sweep * vec3(.026, .055, .090);
        color += digits * mix(vec3(.65, .74, .91), vec3(.35, .86, .96), sweep);
        color += (caption + rule * .38) * vec3(.27, .58, .72);
        color += (grain - .5) * .018 * grainAmount;
    } else if (variant == 7) {
        // Matrix: readable glyph columns, independent falling heads and tails.
        // One cell and one 5x7 lookup per fragment; no glyph-atlas sampler.
        float size = clamp(fullSize.y / 22.0, 18.0, 36.0);
        vec2 cell = floor(px / size);
        vec2 local = fract(px / size);
        float rows = ceil(fullSize.y / size) + 15.0;
        float seed = hash(vec2(cell.x, 13.0));
        float head = mod(time * (3.0 + seed * 6.0) + seed * rows * 4.0, rows);
        float distance = mod(head - cell.y + rows, rows);
        float trail = exp(-distance * (.11 + .08 * seed));
        trail *= 1.0 - smoothstep(11.0 + seed * 8.0, 16.0 + seed * 8.0, distance);
        float bright = exp(-distance * 1.7);
        int code = 65 + int(floor(hash(cell + vec2(0, floor(time * .85 + seed * 5.0))) * 16.0));
        float ink = glyph((local - vec2(.16, .08)) / vec2(.65, .82), code);
        float column = .72 + .28 * seed;
        color = vec3(.006, .025, .017);
        color += ink * trail * column * vec3(.045, .56, .19);
        color += ink * bright * vec3(.68, .92, .76);
        color += exp(-square((local.x - .49) * 3.5)) * trail * .010 * vec3(.1, 1.0, .3);
        color += (grain - .5) * .018 * grainAmount;
    } else if (variant == 8) {
        // Anonymous: an original geometric mask. The expression stays fixed;
        // a cool scanning reflection and breathing halo travel over the face.
        float scale = min(1.0, fullSize.x / max(fullSize.y, 1.0) / .85);
        vec2 q = p / max(scale, .001);
        q.y += .015 * sin(t * 1.4);
        float taper = 1.0 - .24 * smoothstep(-.04, .34, q.y);
        float faceDistance = length(vec2(q.x / (.27 * taper), (q.y + .025) / .37)) - 1.0;
        float face = 1.0 - smoothstep(-.006, .006, faceDistance);
        float edge = exp(-abs(faceDistance) * 28.0);
        float ax = abs(q.x);
        float eyeY = q.y + .070 - .15 * (ax - .11);
        float eye = 1.0 - smoothstep(.85, 1.05, length(vec2((ax - .112) / .064, eyeY / .021)));
        float browY = -.142 - .045 * sin(clamp((ax - .03) / .17, 0.0, 1.0) * 3.1415927);
        float brow = (1.0 - smoothstep(.008, .014, abs(q.y - browY)))
                   * smoothstep(.025, .040, ax) * (1.0 - smoothstep(.19, .205, ax));
        float moustacheY = .092 - .038 * sin(clamp(ax / .175, 0.0, 1.0) * 3.1415927);
        float moustache = (1.0 - smoothstep(.010, .017, abs(q.y - moustacheY)))
                        * smoothstep(.010, .026, ax) * (1.0 - smoothstep(.165, .185, ax));
        float smile = (1.0 - smoothstep(.005, .011, abs(q.y - (.167 - .80 * q.x * q.x))))
                    * (1.0 - smoothstep(.17, .195, ax));
        float goatee = smoothstep(.203, .22, q.y) * (1.0 - smoothstep(.294, .31, q.y))
                     * (1.0 - smoothstep(.002, .009, ax - max(.0, .31 - q.y) * .29));
        float nose = exp(-square(q.x * 45.0)) * smoothstep(-.055, .035, q.y)
                   * (1.0 - smoothstep(.045, .074, q.y));
        float cheek = exp(-dot(vec2((ax - .16) * 18.0, (q.y - .045) * 25.0),
                                   vec2((ax - .16) * 18.0, (q.y - .045) * 25.0)));
        float scan = exp(-square((q.y - (.39 - fract(time * .18) * .78)) * 22.0));
        float halo = exp(-abs(faceDistance) * 4.0) * (.68 + .12 * sin(t * 2.2));
        color = vec3(.018, .037, .060) + halo * vec3(.013, .060, .072);
        vec3 porcelain = vec3(.76, .77, .72) - q.y * vec3(.11, .10, .08);
        porcelain -= nose * vec3(.26, .25, .22) + cheek * vec3(.075, .09, .08);
        float features = clamp(eye + brow + moustache + smile + goatee, 0.0, 1.0);
        porcelain = mix(porcelain, vec3(.015, .027, .032), features);
        porcelain += scan * vec3(.025, .11, .12);
        color = mix(color, porcelain, face);
        color += edge * vec3(.035, .075, .080);
        color += (grain - .5) * .014 * grainAmount;
    } else {
        // Liquid Glass: floating, magnifying lenses over an invented studio.
        // Three fixed analytic shapes: bounded GPU cost and no scene sampling.
        color = glassEnvironment(p, t);
        color = glassLens(color, p, vec2(-.24 + .035 * sin(t * .8), -.06 + .055 * cos(t)),
                          vec2(.20, .28), .12, -.25 + .12 * sin(t * .65), t);
        color = glassLens(color, p, vec2(.23 + .045 * cos(t * .75), .08 + .045 * sin(t * .9)),
                          vec2(.18, .22), .12, .32 - .16 * cos(t * .7), t);
        color = glassLens(color, p, vec2(.08 + .08 * sin(t * .6), -.275 + .025 * cos(t)),
                          vec2(.10, .10), .10, 0.0, t);
        color += (grain - .5) * .008 * grainAmount;
    }
    vec2 centered = (uv - .5) * 2.0;
    color *= 1.0 - .15 * dot(centered, centered);
    float edge = min(min(px.x, fullSize.x - px.x), min(px.y, fullSize.y - px.y));
    color += exp(-max(edge, 0.0) * 1.7) * vec3(.025);
    color *= tint * (2.0 * (1.0 - darkness));
    // Every style and every boundary value remains wholly synthetic and opaque.
    fragColor = vec4(clamp(color, 0.0, 1.0), 1.0);
}
)GLSL";

inline cairo_surface_t* icon(std::string_view shape) {
    if (shape != "eye" && shape != "lock" && shape != "shield") return nullptr;
    auto* surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, EYE_SIZE, EYE_SIZE);
    auto* paint = cairo_create(surface);
    cairo_set_operator(paint, CAIRO_OPERATOR_CLEAR);
    cairo_paint(paint);
    cairo_set_operator(paint, CAIRO_OPERATOR_OVER);
    cairo_set_source_rgba(paint, .92, .94, .96, 1.0);
    cairo_set_line_width(paint, 3.4);
    cairo_set_line_cap(paint, CAIRO_LINE_CAP_ROUND);
    cairo_set_line_join(paint, CAIRO_LINE_JOIN_ROUND);
    if (shape == "eye") {
        // Exact mirrored curves and a concentric pupil, with no uneven slash.
        cairo_move_to(paint, 24, 64);
        cairo_curve_to(paint, 40, 42, 52, 38, 64, 38);
        cairo_curve_to(paint, 76, 38, 88, 42, 104, 64);
        cairo_curve_to(paint, 88, 86, 76, 90, 64, 90);
        cairo_curve_to(paint, 52, 90, 40, 86, 24, 64);
        cairo_close_path(paint);
        cairo_stroke(paint);
        cairo_arc(paint, 64, 64, 11.5, 0, 6.283185307);
        cairo_stroke(paint);
    } else if (shape == "lock") {
        cairo_move_to(paint, 46, 55);
        cairo_line_to(paint, 46, 44);
        cairo_curve_to(paint, 46, 20, 82, 20, 82, 44);
        cairo_line_to(paint, 82, 55);
        cairo_stroke(paint);
        cairo_move_to(paint, 45, 55);
        cairo_line_to(paint, 83, 55);
        cairo_curve_to(paint, 87, 55, 90, 58, 90, 62);
        cairo_line_to(paint, 90, 91);
        cairo_curve_to(paint, 90, 95, 87, 98, 83, 98);
        cairo_line_to(paint, 45, 98);
        cairo_curve_to(paint, 41, 98, 38, 95, 38, 91);
        cairo_line_to(paint, 38, 62);
        cairo_curve_to(paint, 38, 58, 41, 55, 45, 55);
        cairo_close_path(paint);
        cairo_stroke(paint);
        cairo_arc(paint, 64, 73, 3.5, 0, 6.283185307);
        cairo_fill(paint);
        cairo_move_to(paint, 64, 76);
        cairo_line_to(paint, 64, 83);
        cairo_stroke(paint);
    } else {
        cairo_move_to(paint, 64, 26);
        cairo_curve_to(paint, 75, 34, 87, 39, 98, 42);
        cairo_line_to(paint, 94, 70);
        cairo_curve_to(paint, 91, 86, 79, 99, 64, 106);
        cairo_curve_to(paint, 49, 99, 37, 86, 34, 70);
        cairo_line_to(paint, 30, 42);
        cairo_curve_to(paint, 41, 39, 53, 34, 64, 26);
        cairo_close_path(paint);
        cairo_stroke(paint);
        cairo_move_to(paint, 48, 64);
        cairo_line_to(paint, 60, 76);
        cairo_line_to(paint, 81, 54);
        cairo_stroke(paint);
    }
    const auto status = cairo_status(paint);
    cairo_destroy(paint);
    if (status != CAIRO_STATUS_SUCCESS || cairo_surface_status(surface) != CAIRO_STATUS_SUCCESS) {
        cairo_surface_destroy(surface);
        return nullptr;
    }
    cairo_surface_flush(surface);
    return surface;
}
inline cairo_surface_t* eye() { return icon("eye"); }
} // namespace Hyprveil::Spoiler
