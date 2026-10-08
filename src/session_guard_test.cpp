// Exercise the actual live-marker filesystem boundary in private /tmp dirs.
#include "SessionGuard.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>

int main() {
    char temporary[] = "/tmp/hv-guard-XXXXXX";
    const auto* created = mkdtemp(temporary);
    if (!created)
        return 1;
    const std::filesystem::path runtime{created};
    const auto marker = runtime / (".hyprveil-live-" + std::to_string(getpid()));
    constexpr std::uint64_t now = 100000;
    const std::string signature = "synthetic-instance";
    unsigned passed = 0;
    auto write = [&](pid_t process, const std::string& instance, const std::string& mode, std::uint64_t expiry) {
        std::filesystem::remove(marker);
        std::ofstream stream{marker};
        stream << "hyprveil-live-v1\n" << process << '\n' << instance << '\n' << mode << '\n' << expiry << '\n';
        stream.close();
        chmod(marker.c_str(), 0600);
    };
    auto rejects = [&]() {
        try {
            Hyprveil::requireLiveMarker(runtime.string(), getpid(), signature, now);
        } catch (const std::runtime_error&) {
            ++passed;
            return;
        }
        throw std::runtime_error("invalid live marker was accepted");
    };
    try {
        rejects(); // Missing file.
        write(getpid(), signature, "black", now + 3600);
        Hyprveil::requireLiveMarker(runtime.string(), getpid(), signature, now);
        ++passed;
        chmod(marker.c_str(), 0644);
        rejects();
        write(getpid() + 1, signature, "black", now + 3600);
        rejects();
        write(getpid(), "another-instance", "black", now + 3600);
        rejects();
        write(getpid(), signature, "omit", now + 3600);
        rejects();
        write(getpid(), signature, "black", now);
        rejects();
        write(getpid(), signature, "black", now + 14401);
        rejects();
        write(getpid(), signature, "black", now + 3600);
        const auto target = runtime / "target";
        std::filesystem::rename(marker, target);
        std::filesystem::create_symlink(target, marker);
        rejects();
        std::filesystem::remove(marker);
        std::filesystem::create_hard_link(target, marker);
        rejects();
        std::filesystem::remove(marker);
        std::filesystem::remove(target);
        write(getpid(), signature, "black", now + 3600);
        chmod(runtime.c_str(), 0755);
        rejects();
        chmod(runtime.c_str(), 0700);
        {
            std::ofstream append{marker, std::ios::app};
            append << "unexpected\n";
        }
        rejects();
        std::filesystem::remove_all(runtime);
        std::cout << "session guard: " << passed << " filesystem checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        chmod(runtime.c_str(), 0700);
        std::filesystem::remove_all(runtime);
        std::cerr << "session guard: " << error.what() << '\n';
        return 1;
    }
}
