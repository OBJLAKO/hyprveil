#include "RenderAssets.hpp"
#include <cassert>
#include <filesystem>
#include <fstream>
#include <vector>

int main() {
    using namespace Hyprveil::Assets;
    const auto wrapped = wrapShader("vec4 hyprveil(vec2 uv) { return vec4(uv, sin(time), .1); }");
    assert(wrapped && wrapped->find("fragColor = vec4(clamp(value.rgb, 0.0, 1.0), 1.0)") != std::string::npos);
    assert(wrapped->find("any(isnan(value.rgb))") != std::string::npos);
    for (const auto source : {"uniform sampler2D window;", "samplerCube original;", "isampler2D data;",
                              "#extension GL_OES_EGL_image_external : require", "#define hyprveil main",
                              "layout(binding=0) buffer Secret { int x; };", "uniform float unsupported;",
                              "imageStore(privatePixels, ivec2(0), vec4(1));", "/* unterminated"})
        assert(!wrapShader(source));
    assert(wrapShader("// texture sampler2D is not supplied\nvec4 hyprveil(vec2 uv) {return vec4(uv,0.,1.);}"));
    assert(wrapShader("/* #version 300 es */ vec4 hyprveil(vec2 uv) {return vec4(1.);}"));
    assert(!wrapShader(std::string(MAX_SHADER_BYTES + 1, ' ')));
    assert(!wrapShader(std::string("void\0main", 9)));
    assert(!wrapShader(""));
    assert(imageFit("stretch") == 0 && imageFit("fill") == 1 && imageFit("fit") == 2 && imageFit("tile") == 3);

    char temporary[] = "/tmp/hyprveil-shader-read-XXXXXX";
    const char* directory = mkdtemp(temporary);
    assert(directory);
    const auto root = std::filesystem::path{directory};
    const auto source = root / "source.frag";
    { std::ofstream file{source}; file << "vec4 hyprveil(vec2 uv) { return vec4(1.); }"; }
    assert(readShader(source.string()));
    std::filesystem::create_symlink(source, root / "link.frag");
    assert(!readShader((root / "link.frag").string()));
    std::filesystem::create_directory_symlink(root, root / "parent-link");
    assert(!readShader((root / "parent-link" / "source.frag").string()));
    assert(!readShader(root.string()));
    assert(!readShader("relative.frag"));
    assert(mkfifo((root / "pipe.frag").c_str(), 0600) == 0);
    assert(!readShader((root / "pipe.frag").string())); // never blocks on FIFO
    { std::ofstream file{source}; file << std::string(MAX_SHADER_BYTES + 1, 'x'); }
    assert(!readShader(source.string()));
    { std::ofstream file{source}; file << std::string("valid\0tail", 10); }
    assert(!readShader(source.string()));
    std::filesystem::remove_all(root);
}
