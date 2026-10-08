#pragma once

#include <charconv>
#include <cstdint>
#include <cerrno>
#include <fcntl.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>

namespace Hyprveil {
namespace Detail {
class OwnedFd {
  public:
    explicit OwnedFd(int fd) : m_fd(fd) {}
    ~OwnedFd() { if (m_fd >= 0) close(m_fd); }
    OwnedFd(const OwnedFd&) = delete;
    OwnedFd& operator=(const OwnedFd&) = delete;
    int get() const { return m_fd; }
  private:
    int m_fd;
};
} // namespace Detail

// A live trial is pinned to the compositor process and its authoritative
// instance signature. The caller supplies those values, never marker contents
// or an inherited HYPRLAND_INSTANCE_SIGNATURE environment variable.
inline void requireLiveMarker(const std::string& runtime, pid_t pid, const std::string& signature, std::uint64_t now) {
    if (runtime.empty() || runtime.front() != '/' || pid <= 0 || signature.empty())
        throw std::runtime_error("hyprveil: missing live session identity");
    Detail::OwnedFd directory{open(runtime.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)};
    struct stat directoryInfo {};
    if (directory.get() < 0 || fstat(directory.get(), &directoryInfo) != 0 || !S_ISDIR(directoryInfo.st_mode) ||
        directoryInfo.st_uid != getuid() || (directoryInfo.st_mode & 0777) != 0700)
        throw std::runtime_error("hyprveil: live runtime must be an owned 0700 directory without a symlink");

    const auto name = ".hyprveil-live-" + std::to_string(pid);
    Detail::OwnedFd marker{openat(directory.get(), name.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)};
    struct stat markerInfo {};
    if (marker.get() < 0 || fstat(marker.get(), &markerInfo) != 0 || !S_ISREG(markerInfo.st_mode) ||
        markerInfo.st_uid != getuid() || (markerInfo.st_mode & 0777) != 0600 || markerInfo.st_nlink != 1 ||
        markerInfo.st_size <= 0 || markerInfo.st_size > 4096)
        throw std::runtime_error("hyprveil: live trial requires an owned 0600 regular session marker without links");

    std::string contents;
    char buffer[512];
    for (;;) {
        const auto count = read(marker.get(), buffer, sizeof(buffer));
        if (count < 0 && errno == EINTR)
            continue;
        if (count < 0)
            throw std::runtime_error("hyprveil: cannot read live trial marker");
        if (count == 0)
            break;
        contents.append(buffer, static_cast<std::size_t>(count));
        if (contents.size() > 4096)
            throw std::runtime_error("hyprveil: oversized live trial marker");
    }
    std::istringstream stream{contents};
    std::string version, process, instance, mode, expiry, extra;
    if (contents.empty() || contents.back() != '\n' || !std::getline(stream, version) || !std::getline(stream, process) ||
        !std::getline(stream, instance) || !std::getline(stream, mode) || !std::getline(stream, expiry) || std::getline(stream, extra) ||
        version != "hyprveil-live-v1" || process != std::to_string(pid) || instance != signature || mode != "black")
        throw std::runtime_error("hyprveil: live trial marker does not match this process, instance, or initial black mode");
    std::uint64_t expires = 0;
    const auto parsed = std::from_chars(expiry.data(), expiry.data() + expiry.size(), expires);
    if (parsed.ec != std::errc{} || parsed.ptr != expiry.data() + expiry.size() || expires <= now || expires - now > 4 * 60 * 60)
        throw std::runtime_error("hyprveil: live trial marker must expire within the next four hours");
}
} // namespace Hyprveil
