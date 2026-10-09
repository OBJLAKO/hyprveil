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
    auto rejects = [&](bool optional = false) {
        try {
            if (optional) Hyprveil::requireOptionalLiveMarker(runtime.string(), getpid(), signature, now);
            else Hyprveil::requireLiveMarker(runtime.string(), getpid(), signature, now);
        } catch (const std::runtime_error&) {
            ++passed;
            return;
        }
        throw std::runtime_error("invalid live marker was accepted");
    };
    try {
        const auto rejectsAbi = [&](std::string_view compositor, std::string_view client) {
            try { Hyprveil::requireReviewedAbi(compositor, client); }
            catch (const std::runtime_error&) { ++passed; return; }
            throw std::runtime_error("unreviewed or mismatched native ABI was accepted");
        };
        Hyprveil::requireReviewedAbi(Hyprveil::REVIEWED_ABI, Hyprveil::REVIEWED_ABI);
        ++passed;
        rejectsAbi(Hyprveil::REVIEWED_ABI, "other-client");
        rejectsAbi("newer-matching-compositor", "newer-matching-compositor");
        const auto changedDependency = std::string{Hyprveil::REVIEWED_ABI} + "_different-dependency";
        rejectsAbi(changedDependency, changedDependency);
        rejectsAbi({}, {});
        rejects(); // Missing file.
        Hyprveil::requireOptionalLiveMarker(runtime.string(), getpid(), signature, now);
        if (std::filesystem::exists(marker)) throw std::runtime_error("standard admission created a trial marker");
        ++passed;
        write(getpid(), signature, "black", now + 3600);
        Hyprveil::requireLiveMarker(runtime.string(), getpid(), signature, now);
        ++passed;
        Hyprveil::requireOptionalLiveMarker(runtime.string(), getpid(), signature, now);
        ++passed;
        write(getpid(), signature, "cancelled", 0);
        rejects(true); // Cancellation cannot become a standard no-marker load.
        write(getpid(), signature, "black", now);
        rejects(true); // Existing expired optional markers also deny admission.
        write(getpid(), signature, "black", now + 3600);
        chmod(marker.c_str(), 0644);
        rejects();
        rejects(true);
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
        rejects(true);
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
        std::cout << "session guard: " << passed << " ABI/filesystem checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        chmod(runtime.c_str(), 0700);
        std::filesystem::remove_all(runtime);
        std::cerr << "session guard: " << error.what() << '\n';
        return 1;
    }
}
