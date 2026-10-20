// src/facade/abi.cpp - the C++ side of the Rust/C++ seam: see docs/RUST_HOST.md and the header it implements.
//
// Compiled by MSVC, as an ordinary .cpp (not nvcc), and linked into strata_facade.dll.  That is deliberate:
// milestone 2 exists to answer the toolchain question the whole plan rests on - MSVC + cudart + cargo in one
// build - and a shim that only returns a constant answers nothing, because a linker is free to drop a static
// library whose symbols nobody referenced.  So each probe below calls into a library the facade links, and the
// Rust test asserts the answer that library gives, not an answer the shim could have made up.
//
//   probe_policy -> strata_spec (MSVC-built, pure C++, no CUDA).  DraftPolicy::choose on a policy with no
//       measurements yet is decided by the priors alone, which makes it a fixed answer on both sides of the
//       seam: choose(3, 4, 6) = lookup, window 4.  The Rust test asserts exactly that.
//   cudart_version -> CUDA::cudart, called from C++ that MSVC compiled.  Built without CUDA, this says so with
//       STRATA_ERR_DEVICE_UNAVAILABLE instead of pretending to have answered.
//   expert_apply -> strata_kernels_cpu (milestone 3).  The first call across the seam that runs a kernel on bytes
//       the *caller* read, and the first that carries a struct.  Built without the kernels linked, it declines.

#include "strata/c/abi.h"

#include "strata/spec/draft_policy.hpp"

#ifdef STRATA_FACADE_WITH_CUDART
#include <cuda_runtime.h>
#endif

#ifdef STRATA_FACADE_WITH_KERNELS
#include "strata/kernels/cpu/expert.hpp"
#endif

namespace {

// The rules say nothing the facade returns is owned by it, so this is a buffer the facade keeps - not something
// the caller frees - and it is written only on a failure path.
//
// Per thread, because the caller is a host with more than one thread and two of them can fail at once.  This is
// not theoretical: the Rust test suite found it, as two of its tests raced on a shared message and one read the
// other's failure.  A pointer to a string literal needs no destructor, so plain `thread_local` is enough.
thread_local const char *g_last_error = "";

void set_last_error(const char *what) { g_last_error = what; }

}  // namespace

extern "C" {

STRATA_API int strata_abi_version(void) { return STRATA_ABI_VERSION; }

STRATA_API const char *strata_abi_last_error(void) { return g_last_error; }

STRATA_API int strata_facade_probe_policy(int t_mtp, int lookup_k, int match,
                                          int *lookup_out, int *t_out) {
    if (lookup_out == nullptr || t_out == nullptr) {
        set_last_error("probe_policy: output pointer is null");
        return STRATA_ERR_BUFFER_TOO_SHORT;
    }
    // Write the caller's memory, own nothing that was written.  Initialised first so a path that returns early
    // can never leave the caller reading uninitialised memory.
    *lookup_out = -1;
    *t_out = -1;

    strata::spec::DraftPolicy policy(strata::spec::DraftPolicy::kMaxT);
    const strata::spec::DraftPolicy::Pick pick = policy.choose(t_mtp, lookup_k, match);
    *lookup_out = pick.lookup ? 1 : 0;
    *t_out = pick.t;
    return STRATA_OK;
}

STRATA_API int strata_facade_cudart_version(int *runtime_version_out) {
#ifdef STRATA_FACADE_WITH_CUDART
    if (runtime_version_out == nullptr) {
        set_last_error("cudart_version: output pointer is null");
        return STRATA_ERR_BUFFER_TOO_SHORT;
    }
    int version = 0;
    const int err = cudaRuntimeGetVersion(&version);
    if (err != 0) {                 // cudaSuccess is 0; the runtime's own code is passed straight through
        set_last_error("cudart_version: cudaRuntimeGetVersion failed");
        return STRATA_ERR_DEVICE_UNAVAILABLE;
    }
    *runtime_version_out = version;
    return STRATA_OK;
#else
    (void) runtime_version_out;
    set_last_error("cudart_version: this facade was built without CUDA");
    return STRATA_ERR_DEVICE_UNAVAILABLE;
#endif
}

STRATA_API int strata_facade_expert_apply(const strata_expert_ref *ref,
                                          const float *x, unsigned long long x_len,
                                          float *out, unsigned long long out_cap) {
#ifdef STRATA_FACADE_WITH_KERNELS
    // The header's rules, checked rather than trusted: (ptr, len) pairs are validated before anything is read
    // or written, so a bad call can never leave the caller with a partially-written `out` and a status that says
    // nothing about which half it wrote.
    if (ref == nullptr || ref->bytes == nullptr) {
        set_last_error("expert_apply: ref or ref->bytes is null");
        return STRATA_ERR_BUFFER_TOO_SHORT;
    }
    if (x == nullptr || out == nullptr) {
        set_last_error("expert_apply: x or out is null");
        return STRATA_ERR_BUFFER_TOO_SHORT;
    }
    // The kernel's geometry is fixed by the artifact, so these are exact, not minimums - a short `x` would make
    // the kernel read past the caller's buffer, and the caller would have no way to see that it had.
    if (ref->len != strata::kernels::cpu::BLOB || x_len != strata::kernels::cpu::H ||
        out_cap < strata::kernels::cpu::H) {
        set_last_error("expert_apply: buffer lengths are not the geometry the kernel was built for");
        return STRATA_ERR_BUFFER_TOO_SHORT;
    }
    // Refuse rather than run: code compiled with /arch:AVX512 can emit AVX-512 anywhere in its translation
    // unit, so on a CPU without it the choice is this status or an illegal instruction.  (expert.hpp:103 says
    // the same thing about the engine's startup check, for the same reason.)
    const strata::kernels::cpu::CpuFeatures feats = strata::kernels::cpu::cpu_features();
    if (!feats.usable()) {
        set_last_error(feats.reason());
        return STRATA_ERR_DEVICE_UNAVAILABLE;
    }

    // Owned by the facade, because the rules say the facade returns nothing it owns - and the kernel takes its
    // scratch as a reference to memory the caller owns, allocating nothing itself.  So this is the facade's own
    // caller-side scratch, and the call is not reentrant: one at a time, which is what a single ring and a
    // single serving thread mean anyway.
    static strata::kernels::cpu::ExpertScratch scratch;
    strata::kernels::cpu::s2_expert_vnni(ref->bytes, x, out, scratch);
    return STRATA_OK;
#else
    (void) ref;
    (void) x;
    (void) x_len;
    (void) out;
    (void) out_cap;
    set_last_error("expert_apply: this facade was built without the kernels");
    return STRATA_ERR_DEVICE_UNAVAILABLE;
#endif
}

}  // extern "C"
