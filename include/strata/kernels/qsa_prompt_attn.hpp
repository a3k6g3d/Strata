// include/strata/kernels/qsa_prompt_attn.hpp - perf-review D-1: the prompt path's QSA attention on tensor cores.
//
// `qsa_decode_attn_batch` serves a prompt one query at a time with the decode kernel: FP32 dot products with a warp
// reduction per head and cell, 64-cell chunks whose partial sums go through global scratch to a second (merge)
// kernel. At 32K that is 21% of the prompt (5.6 s of 26 s on a 5070, Q2_0, int8 KV), and it is limited by
// instruction issue, not memory (~150 GB/s of logical reads, 3.4 TFLOP/s).
//
// This kernel keeps the same per-query selection (no masking, no union) and reads the same pools, but:
//   * one block per (query, KV head) walks all its selected cells in chunks of 32 with an online softmax: no
//     split-K scratch and no merge kernel;
//   * q.k and p.v are m16n8k16 FP16 MMAs with FP32 accumulation over the 12 query heads of the KV head (+4 pad rows);
//   * the stored values enter exactly: int8 codes are exact in FP16 and their per-64 scales are applied in FP32
//     (to the q.k partial of each 64-dim group, and folded into p for p.v); FP16 KV is used as is;
//   * q and p are split into FP16 hi + lo parts (two MMAs each), so they keep ~22 bits: the result differs from
//     the FP32 kernel by summation order and the exp2 rounding, not by an FP16 cast.
// Not bitwise equal to `qsa_decode_attn_batch`; `qsa_prompt_attn_parity` bounds the difference and the prompt
// quality gate (needles, teacher-forced top-1) checks it end to end. Q4_0 KV (mode 4): each block's codes enter
// exactly as int8 and its scale (one per 32 values) in FP32, as int8 KV's do (STRATA_PROMPT_ATTN_Q4=0: the old kernel).
// On sm_80+ both int8 and Q4_0 run the cp.async "v2" kernels: each warp gathers its own 64-dim slice of K and V
// (int8: 64 bytes; q4_0: its two blocks, 36 bytes, still packed) one chunk ahead while it computes the current one.
// Q4_0 moved from v1's mode 4 to its v2 kernel: 10.2 -> 5.6 ms per 2,048-query chunk at 64K (RTX 5070,
// qsa_prompt_attn_parity), faster than int8's 6.5 ms; STRATA_PROMPT_ATTN_V1=1 runs v1's mode 4 again (A/B).
#pragma once

#include "strata/kernels/qsa_decode_attn.hpp"

#include <cstdint>

namespace strata::kernels {

/// Same arguments and output as `qsa_decode_attn_batch` minus the scratch. Returns false (nothing launched) when the
/// geometry is not 24 heads / 2 KV heads / 256: the caller then uses the old kernel.
bool qsa_prompt_attn_batch(const float* q, const QsaAttnPools& pools, const int32_t* ids, const int32_t* steps,
                           int64_t cap, const QsaShapes& s, float* attn, int64_t n_q, void* stream);

/// Q4_0 KV (`--kv q4_0`): `fwht256(q)`, then `qsa_prompt_attn_batch`, then `fwht256(attn)` (kv_q4.hpp's rotation of the
/// queries and back of the output) in one kernel, bit for bit the same output: q comes in unrotated and is left as it
/// is, attn goes out rotated back. Saves the two passes over the chunk's 24 query rows in global memory. Returns false
/// (nothing launched) when that kernel does not run here; the caller then rotates and calls qsa_prompt_attn_batch.
bool qsa_prompt_attn_batch_rot(const float* q, const QsaAttnPools& pools, const int32_t* ids, const int32_t* steps,
                               int64_t cap, const QsaShapes& s, float* attn, int64_t n_q, void* stream);

/// The decode form for a verify window's few queries: `qsa_decode_attn_batch`'s arguments, scratch and output, on the
/// tensor cores of the int8 / q4_0 kernels above (sm_80+). Each (query, KV head) is split over ranges of its
/// selection so the window fills the GPU, and the ranges are merged as the FP32 kernel merges its chunks: FP32-level
/// accuracy, deterministic, not bitwise equal to it. Returns false (nothing launched) where it does not run: no
/// sm_80, fp16 or K8V4 KV, AMD, or STRATA_DECODE_ATTN_TC=0. Safe inside a CUDA graph capture (no shared-memory
/// opt-in, no host synchronization).
bool qsa_decode_attn_tc(const float* q, const QsaAttnPools& pools, const int32_t* ids, const int32_t* steps,
                        int64_t cap, const QsaShapes& s, float* scratch, float* attn, int64_t n_q, void* stream);
bool qsa_decode_attn_tc_usable(const QsaAttnPools& pools, const QsaShapes& s);

/// Whether qsa_prompt_attn_batch_rot runs on the current device (sm_80+, the Q4_0 v2 kernel not switched off), known
/// before the queries are rotated. STRATA_PROMPT_ATTN_ROT_FUSED=0: never (the separate fwht256 passes, an A/B arm).
bool qsa_prompt_attn_rot_fused(const QsaShapes& s);

}  // namespace strata::kernels
