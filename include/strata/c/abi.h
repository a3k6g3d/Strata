/* strata C ABI - the only interface allowed between the Rust host and the C++ device layer.
 *
 * Hand-written on purpose: no bindgen, no cxx, no generated code in the build. The header IS the contract, and
 * the Rust crate's test suite parses this file and asserts the version constant matches its own. That guard is
 * the whole reason a hand-written header is worth its upkeep: the failure mode of a cross-language seam is a
 * field added on one side and a silent mismatch on the other, and a generated header cannot catch that.
 *
 * Rules:
 *   - C ABI only. No C++ type crosses this line. Buffers are (ptr, len), errors are this enum.
 *   - Nothing returned by the facade is owned by it. Anything Rust hands down must outlive the call.
 *   - Adding a field to a struct below means bumping STRATA_ABI_VERSION, and the Rust mirror, in the same commit.
 */
#ifndef STRATA_C_ABI_H
#define STRATA_C_ABI_H

#ifdef __cplusplus
extern "C" {
#endif

/* Keep in step with strata_abi::ABI_VERSION in rust-host/crates/strata-abi/src/lib.rs. */
#define STRATA_ABI_VERSION 1

/* Stable across releases: the Rust side matches on these by value, never by string. */
typedef enum strata_status {
    STRATA_OK = 0,
    STRATA_ERR_WOULD_BLOCK = 1,   /* the ring is full; the caller owns the retry, the entry was NOT consumed */
    STRATA_ERR_BUFFER_TOO_SHORT = 2,
    STRATA_ERR_BAD_SLOT = 3,
    STRATA_ERR_DEVICE_UNAVAILABLE = 4,
    STRATA_ERR_ABI_VERSION = 5    /* the two sides disagree about this header */
} strata_status;

/* One expert the host has decided the device may read. The host owns these bytes for exactly as long as the
 * device holds the borrow - which is the rule the C++ side states as a comment today (expert_source.hpp:200:
 * "stays valid for the layer it was asked in and the next one or two"). Here it is a borrow, not a comment. */
typedef struct strata_expert_ref {
    const unsigned char *bytes;
    unsigned long long len;
    int slot;
    int source_tier;   /* 0 = pinned RAM, 1 = NVMe, 2 = SSD, 3 = model file */
} strata_expert_ref;

/* The version handshake, and the only entry point the skeleton needs. The real device calls are added at
 * milestone 2, when the facade DLL is built from the existing strata_core/strata_kernels static libs. */
int strata_abi_version(void);
const char *strata_abi_last_error(void);

#ifdef __cplusplus
}
#endif

#endif /* STRATA_C_ABI_H */
