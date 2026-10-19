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

#include "strata/c/abi.h"

#include "strata/spec/draft_policy.hpp"

#ifdef STRATA_FACADE_WITH_CUDART
#include <cuda_runtime.h>
#endif

namespace {

// The rules say nothing the facade returns is owned by it, so this is a buffer the facade keeps - not something
// the caller frees - and it is written only on a failure path.
const char *g_last_error = "";

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

}  // extern "C"
