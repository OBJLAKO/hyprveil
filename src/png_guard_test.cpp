// Test the real pixel-only boundary with Cairo, including compressed metadata.
#include "PngGuard.hpp"
#include <cairo/cairo.h>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>

using Bytes = std::vector<unsigned char>;
namespace {
void be32(Bytes& out, uint32_t value) {
    for (unsigned shift : {24U, 16U, 8U, 0U}) out.push_back((value >> shift) & 255);
}
void chunk(Bytes& out, const char* type, const Bytes& data) {
    be32(out, data.size());
    const auto crcOffset = out.size();
    out.insert(out.end(), type, type + 4);
    out.insert(out.end(), data.begin(), data.end());
    be32(out, crc32(0, out.data() + crcOffset, data.size() + 4));
}
Bytes compressed(const Bytes& input) {
    Bytes output(compressBound(input.size()));
    uLongf size = output.size();
    if (compress2(output.data(), &size, input.data(), input.size(), 9) != Z_OK) throw std::runtime_error("zlib test compression failed");
    output.resize(size);
    return output;
}
Bytes png(uint32_t width = 1, uint32_t height = 1, unsigned depth = 8, unsigned type = 6, Bytes palette = {}, Bytes alpha = {},
          std::uint16_t alpha16 = 65535) {
    Bytes output{137, 80, 78, 71, 13, 10, 26, 10};
    Bytes header;
    be32(header, width); be32(header, height);
    header.insert(header.end(), {static_cast<unsigned char>(depth), static_cast<unsigned char>(type), 0, 0, 0});
    chunk(output, "IHDR", header);
    if (!palette.empty()) chunk(output, "PLTE", palette);
    if (!alpha.empty()) chunk(output, "tRNS", alpha);
    const Bytes pixels = type == 3 ? Bytes{0, 1} : depth == 16 ? Bytes{0, 255, 255, 0, 0, 0, 0,
        static_cast<unsigned char>(alpha16 >> 8), static_cast<unsigned char>(alpha16)} : Bytes{0, 255, 0, 0, 255};
    chunk(output, "IDAT", compressed(pixels));
    chunk(output, "IEND", {});
    return output;
}
struct Input { const Bytes& bytes; size_t offset = 0; };
cairo_status_t read(void* closure, unsigned char* data, unsigned int length) {
    auto& input = *static_cast<Input*>(closure);
    if (length > input.bytes.size() - input.offset) return CAIRO_STATUS_READ_ERROR;
    std::copy_n(input.bytes.data() + input.offset, length, data);
    input.offset += length;
    return CAIRO_STATUS_SUCCESS;
}
bool decode(const Bytes& bytes, bool transparent = false) {
    Input input{bytes};
    auto* image = cairo_image_surface_create_from_png_stream(read, &input);
    bool ok = cairo_surface_status(image) == CAIRO_STATUS_SUCCESS && cairo_image_surface_get_width(image) == 1 && cairo_image_surface_get_height(image) == 1;
    if (ok && transparent) {
        cairo_surface_flush(image);
        const auto* pixel = cairo_image_surface_get_data(image);
        ok = cairo_image_surface_get_format(image) == CAIRO_FORMAT_ARGB32 && pixel[0] == 0 && pixel[1] == 0 && pixel[2] == 0 && pixel[3] == 0;
    }
    cairo_surface_destroy(image);
    return ok;
}
bool upload(const Bytes& bytes, std::uint32_t expected) {
    Input input{bytes};
    auto* image = cairo_image_surface_create_from_png_stream(read, &input);
    auto* pixels = Hyprveil::Png::uploadPixels(image);
    // Drop the decoder first to verify that the upload reference owns its data.
    cairo_surface_destroy(image);
    bool ok = pixels && cairo_surface_status(pixels) == CAIRO_STATUS_SUCCESS &&
        cairo_image_surface_get_format(pixels) == CAIRO_FORMAT_ARGB32 &&
        cairo_image_surface_get_stride(pixels) == 4;
    if (ok) {
        std::uint32_t pixel = 0;
        std::memcpy(&pixel, cairo_image_surface_get_data(pixels), sizeof(pixel));
        ok = pixel == expected;
    }
    if (pixels) cairo_surface_destroy(pixels);
    return ok;
}
} // namespace

int main() {
    unsigned passed = 0;
    const auto check = [&passed](bool condition, const char* name) {
        if (!condition) throw std::runtime_error(name);
        ++passed;
    };
    try {
        const auto simple = png();
        check(decode(Hyprveil::Png::pixelOnly(simple)), "ordinary pixels must decode");
        const auto palette = png(1, 1, 8, 3, {255, 0, 0, 0, 0, 255}, {255, 0});
        check(decode(Hyprveil::Png::pixelOnly(palette), true), "palette transparency must survive");
        check(decode(Hyprveil::Png::pixelOnly(png(1, 1, 16))), "small 16-bit image must decode");
        check(upload(Hyprveil::Png::pixelOnly(simple), 0xffff0000), "8-bit upload preserves opaque red and owns its pixels");
        check(upload(Hyprveil::Png::pixelOnly(png(1, 1, 16)), 0xffff0000), "16-bit floating-point RGBA normalizes to opaque red upload bytes");
        check(upload(Hyprveil::Png::pixelOnly(png(1, 1, 16, 6, {}, {}, 0x8080)), 0x80800000), "16-bit upload preserves premultiplied partial alpha");
        check(upload(Hyprveil::Png::pixelOnly(png(1, 1, 16, 6, {}, {}, 0)), 0), "16-bit upload preserves transparent pixels");
        check(upload(Hyprveil::Png::pixelOnly(palette), 0), "upload preserves indexed transparency");
        check(!Hyprveil::Png::uploadPixels(nullptr), "missing decoded image cannot upload");
        auto* invalidSurface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, -1, 1);
        check(!Hyprveil::Png::uploadPixels(invalidSurface), "failed decoded surface cannot upload");
        cairo_surface_destroy(invalidSurface);

        Bytes metadata(simple.begin(), simple.begin() + 33);
        Bytes text{'k', 0, 0};
        const auto expansion = compressed(Bytes(512 * 1024, 'x'));
        text.insert(text.end(), expansion.begin(), expansion.end());
        for (unsigned i = 0; i < 64; ++i) chunk(metadata, "zTXt", text);
        metadata.insert(metadata.end(), simple.begin() + 33, simple.end());
        const auto filtered = Hyprveil::Png::pixelOnly(metadata);
        check(metadata.size() < 40000 && filtered == simple && decode(filtered), "metadata expansion must not reach decoder");

        for (const char* type : {"iCCP", "iTXt", "tEXt", "gAMA", "sRGB", "eXIf", "acTL", "fdAT"}) {
            Bytes value(simple.begin(), simple.begin() + 33);
            chunk(value, type, {1, 2, 3, 4});
            value.insert(value.end(), simple.begin() + 33, simple.end());
            check(Hyprveil::Png::pixelOnly(value) == simple, "ancillary data must be removed");
        }
        check(Hyprveil::Png::pixelOnly(png(8193, 1)).empty(), "axis cap");
        check(Hyprveil::Png::pixelOnly(png(8192, 8192)).empty(), "pixel cap");
        check(Hyprveil::Png::pixelOnly(png(3840, 2160, 16)).empty(), "decoded floating-point allocation cap");
        check(Hyprveil::Png::pixelOnly(png(3840, 2160, 8)).size() > 0, "4K 8-bit declaration stays within budget");
        check(Hyprveil::Png::pixelOnly(png(0, 1)).empty(), "zero dimensions");
        Bytes bad = simple; bad[0] = 0;
        check(Hyprveil::Png::pixelOnly(bad).empty(), "bad signature");
        bad = simple; bad.resize(bad.size() - 1);
        check(Hyprveil::Png::pixelOnly(bad).empty(), "truncated chunk");
        bad = simple; bad.push_back(0);
        check(Hyprveil::Png::pixelOnly(bad).empty(), "trailing bytes");
        bad = simple; bad[8] = 255;
        check(Hyprveil::Png::pixelOnly(bad).empty(), "unbounded chunk length");
        bad = simple; bad[29] ^= 1;
        check(Hyprveil::Png::pixelOnly(bad).empty(), "bad essential CRC");
        bad.assign(simple.begin(), simple.begin() + 33); chunk(bad, "FAKE", {});
        bad.insert(bad.end(), simple.begin() + 33, simple.end());
        check(Hyprveil::Png::pixelOnly(bad).empty(), "unknown critical chunk");
        bad.assign(simple.begin(), simple.begin() + 33); chunk(bad, "PLTE", Bytes(771));
        bad.insert(bad.end(), simple.begin() + 33, simple.end());
        check(Hyprveil::Png::pixelOnly(bad).empty(), "oversized palette");
        bad.assign(simple.begin(), simple.begin() + 33); chunk(bad, "tRNS", Bytes(257));
        bad.insert(bad.end(), simple.begin() + 33, simple.end());
        check(Hyprveil::Png::pixelOnly(bad).empty(), "oversized alpha");
        bad.assign(simple.begin(), simple.begin() + 33);
        for (size_t i = 0; i < Hyprveil::Png::MAX_CHUNKS; ++i) chunk(bad, "tEXt", {});
        bad.insert(bad.end(), simple.begin() + 33, simple.end());
        check(Hyprveil::Png::pixelOnly(bad).empty(), "chunk count budget");
        bad.assign(Hyprveil::Png::MAX_BYTES + 1, 0);
        check(Hyprveil::Png::pixelOnly(bad).empty(), "encoded byte budget");
        std::cout << "PNG guard: " << passed << " boundary/actual Cairo checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "PNG guard: " << error.what() << '\n';
        return 1;
    }
}
