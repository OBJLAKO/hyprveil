#pragma once

#include <algorithm>
#include <array>
#include <cstdint>
#include <span>
#include <vector>
#include <zlib.h>

namespace Hyprveil::Png {
constexpr std::size_t MAX_BYTES = 16U * 1024U * 1024U;
constexpr std::uint64_t MAX_PIXELS = 16U * 1024U * 1024U;
constexpr std::uint32_t MAX_AXIS = 8192;
constexpr std::uint64_t MAX_DECODED_BYTES = 64U * 1024U * 1024U;
constexpr std::size_t MAX_CHUNKS = 16384;

// Cairo/libpng can inflate ancillary text/ICC data independently from image
// dimensions. Decode only essential pixel chunks and bounded palette/alpha.
// This also validates the actual byte stream, rather than a pathname snapshot.
inline std::vector<unsigned char> pixelOnly(std::span<const unsigned char> input) {
    constexpr std::array<unsigned char, 8> magic{137, 80, 78, 71, 13, 10, 26, 10};
    if (input.size() < 33 || input.size() > MAX_BYTES || !std::equal(magic.begin(), magic.end(), input.begin()))
        return {};
    const auto be32 = [&input](std::size_t offset) {
        return (std::uint32_t{input[offset]} << 24) | (std::uint32_t{input[offset + 1]} << 16) |
            (std::uint32_t{input[offset + 2]} << 8) | std::uint32_t{input[offset + 3]};
    };
    if (be32(8) != 13 || input[12] != 'I' || input[13] != 'H' || input[14] != 'D' || input[15] != 'R')
        return {};
    const auto width = be32(16), height = be32(20);
    if (!width || !height || width > MAX_AXIS || height > MAX_AXIS || std::uint64_t{width} * height > MAX_PIXELS)
        return {};
    // Cairo 1.18.4 may decode 16-bit PNG into RGBA128F (16 bytes/pixel).
    const auto depth = input[24];
    if ((depth != 1 && depth != 2 && depth != 4 && depth != 8 && depth != 16) ||
        std::uint64_t{width} * height * (depth == 16 ? 16U : 4U) > MAX_DECODED_BYTES)
        return {};

    std::vector<unsigned char> output{magic.begin(), magic.end()};
    const auto colorType = input[25];
    bool havePalette = false, haveAlpha = false, haveData = false, dataEnded = false;
    std::size_t paletteEntries = 0, chunks = 0, offset = 8;
    while (offset < input.size()) {
        if (++chunks > MAX_CHUNKS || input.size() - offset < 12)
            return {};
        const auto length = be32(offset);
        if (length > input.size() - offset - 12)
            return {};
        const auto* type = input.data() + offset + 4;
        for (unsigned i = 0; i < 4; ++i)
            if (!((type[i] >= 'A' && type[i] <= 'Z') || (type[i] >= 'a' && type[i] <= 'z')))
                return {};
        if (type[2] & 0x20) // PNG reserved bit must be zero.
            return {};
        const auto crc = crc32(0, type, static_cast<uInt>(length + 4));
        if (crc != be32(offset + 8 + length))
            return {};
        const auto is = [type](const char* name) { return std::equal(type, type + 4, name); };
        bool keep = false;
        if (is("IHDR")) {
            if (offset != 8 || length != 13)
                return {};
            keep = true;
        } else if (is("PLTE")) {
            if (havePalette || haveData || !length || length > 768 || length % 3 != 0)
                return {};
            havePalette = true;
            paletteEntries = length / 3;
            keep = true;
        } else if (is("tRNS")) {
            if (haveAlpha || haveData || !length || length > 256 ||
                (colorType == 0 && length != 2) || (colorType == 2 && length != 6) ||
                (colorType == 3 && (!havePalette || length > paletteEntries)) ||
                (colorType != 0 && colorType != 2 && colorType != 3))
                return {};
            haveAlpha = true;
            keep = true;
        } else if (is("IDAT")) {
            if (dataEnded || (colorType == 3 && !havePalette))
                return {};
            haveData = true;
            keep = true;
        } else if (is("IEND")) {
            if (length != 0 || !haveData || offset + 12 != input.size())
                return {};
            output.insert(output.end(), input.begin() + offset, input.end());
            return output;
        } else {
            if (!(type[0] & 0x20)) // Unknown critical chunks cannot be ignored.
                return {};
            if (haveData)
                dataEnded = true;
        }
        if (keep)
            output.insert(output.end(), input.begin() + offset, input.begin() + offset + length + 12);
        offset += length + 12;
    }
    return {}; // Missing IEND/truncated stream.
}
} // namespace Hyprveil::Png
