#pragma once

#include <charconv>
#include <cstdint>
#include <cerrno>
#include <fcntl.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/stat.h>
#include <unistd.h>

namespace Hyprveil {
// Hooks depend on exact native layouts and even the reviewed caller's
// ownership conventions. Matching two hashes only proves a local rebuild;
// it must never admit an otherwise unreviewed compositor/dependency ABI.
inline constexpr std::string_view REVIEWED_ABI =
    "efb50993780079460b0cbed1363e2166a2de1d9f_aq_0.15_hu_0.14_hg_0.5_hc_0.1_hlg_0.6";

inline void requireReviewedAbi(std::string_view compositor, std::string_view client) {
    if (compositor != client)
        throw std::runtime_error("hyprveil: exact Hyprland ABI mismatch; rebuild for this compositor");
    if (compositor != REVIEWED_ABI)
        throw std::runtime_error("hyprveil: unreviewed Hyprland ABI; this release only supports the reviewed compositor and dependency versions");
}

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

// Ordinary Hyprpm loads need no marker. If a legacy controller has initiated
// a trial, however, its explicit cancellation/expiry must still deny delayed
// permission responses. Presence (including a symlink) always selects the
// strict marker path; only ENOENT permits standard admission.
inline void requireOptionalLiveMarker(const std::string& runtime, pid_t pid, const std::string& signature, std::uint64_t now) {
    if (runtime.empty() || runtime.front() != '/' || pid <= 0 || signature.empty())
        throw std::runtime_error("hyprveil: missing live session identity");
    const auto path = runtime + "/.hyprveil-live-" + std::to_string(pid);
    struct stat info {};
    if (lstat(path.c_str(), &info) == 0) {
        requireLiveMarker(runtime, pid, signature, now);
        return;
    }
    if (errno != ENOENT)
        throw std::runtime_error("hyprveil: cannot inspect optional live trial marker");
}
} // namespace Hyprveil
