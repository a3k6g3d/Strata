// Windows port of tests/platform/direct_file_async.cpp (POSIX-only: mkstemp, setenv), plus a read past the end of
// the file.  The same checks run against whichever reader the process gets: the C++ pool by default, or the Rust
// host when STRATA_HOST_DLL names strata_host.dll by absolute path (RUST_ENGINE.md M5) - so passing both ways is
// the claim that the two readers keep the same contract.
//
// `check` takes the message by reference on purpose: `check(f.open(name, error), error.c_str())` may take c_str()
// before open() reallocates the string, and then a failure prints freed memory (the POSIX test has that pattern).
#include "strata/platform/direct_file.hpp"
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <cstdio>
using strata::platform::DirectFile;
using strata::platform::Completion;
static void check(bool ok, const std::string& why) { if (!ok) throw std::runtime_error(why); }
int main() {
    char tmp[L_tmpnam_s];
    tmpnam_s(tmp, sizeof tmp);
    std::string name = tmp;
    void* memory = DirectFile::alloc_aligned(4096 * 18);
    try {
        check(memory != nullptr, "buffer allocation");
        { std::ofstream f(name, std::ios::binary); std::string bytes(4096 * 16 + 17, 'x'); f.write(bytes.data(), bytes.size()); }
        DirectFile f;
        std::string error;
        check(f.open(name, error), error);
        check(!f.submit(1, memory, 4096, 0, error), "unaligned request accepted");
        for (unsigned i = 0; i < 17; ++i)
            check(f.submit(i * 4096, static_cast<char*>(memory) + i * 4096, 4096, i, error), error);
        std::vector<bool> seen(17);
        unsigned count = 0;
        while (count < 17) {
            Completion c[17]; int n = f.wait(c, 17, 5000);
            check(n > 0, "completion timeout");
            for (int i = 0; i < n; ++i) {
                check(c[i].tag < 17 && !seen[c[i].tag], "wrong or repeated completion");
                seen[c[i].tag] = true; ++count;
                check(c[i].ok && c[i].bytes == (c[i].tag == 16 ? 17u : 4096u), "incorrect full/EOF completion");
                check(static_cast<char*>(memory)[c[i].tag * 4096] == 'x', "read data mismatch");
            }
        }
        // Past the end of the file: still exactly one completion, ok with zero bytes (issue()'s rule).
        check(f.submit(20 * 4096, static_cast<char*>(memory) + 17 * 4096, 4096, 55, error), error);
        {
            Completion c[4]; int got = 0; bool found = false;
            for (int tries = 0; tries < 4 && !found; ++tries) {
                int n = f.wait(c, 4, 5000);
                for (int i = 0; i < n; ++i) {
                    if (c[i].tag == DirectFile::WAKE_TAG) continue;
                    check(c[i].tag == 55 && c[i].ok && c[i].bytes == 0, "past-EOF read not a zero-byte ok completion");
                    found = true; ++got;
                }
            }
            check(found, "past-EOF read produced no completion");
        }
        std::thread waker([&] { f.wake(); });
        Completion c; check(f.wait(&c, 1, 5000) == 1 && c.tag == DirectFile::WAKE_TAG, "wake lost");
        waker.join();
        check(f.submit(0, memory, 4096, 99, error), error);
        f.close();
        std::memset(memory, 0, 4096);
        check(f.open(name, error), error);
        check(f.wait(&c, 1, 0) == 0, "stale completion on reopen");
        check(f.submit(0, memory, 4096, 100, error), error);
        check(f.wait(&c, 1, 5000) == 1 && c.ok && c.tag == 100, "reopened read failed");
        check(static_cast<char*>(memory)[0] == 'x', "reopened read data mismatch");
        f.close();
        DirectFile::free_aligned(memory); std::remove(name.c_str());
        const char* dll = std::getenv("STRATA_HOST_DLL");
        std::cout << "direct_file_win [" << (dll && *dll ? "Rust host" : "C++ pool")
                  << "]: passed queued reads, short EOF, past-EOF, wake, close, reopen\n";
        return 0;
    } catch (const std::exception& e) {
        DirectFile::free_aligned(memory); std::remove(name.c_str());
        std::cerr << "FAILED: " << e.what() << '\n'; return 1;
    }
}
