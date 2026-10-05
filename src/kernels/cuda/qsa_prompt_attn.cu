// src/kernels/cuda/qsa_prompt_attn.cu - see include/strata/kernels/qsa_prompt_attn.hpp.
#include "strata/core/emulate.hpp"
#include <cstdlib>
#include <cstring>
#include "strata/kernels/qsa_prompt_attn.hpp"
#include "strata/kernels/kv_q8.hpp"
#include "strata/kernels/kv_q4.hpp"

#include <cuda_fp16.h>
#include <cuda_runtime.h>
#include <algorithm>
#include <cfloat>
#include <cmath>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <type_traits>

namespace strata::kernels {
namespace {

constexpr int HD = 256;           // head_dim
constexpr int G = 12;             // query heads per KV head
#ifndef D1_CH
#define D1_CH 32
#endif
constexpr int CH = D1_CH;         // cells per chunk
constexpr int THREADS = 128;      // 4 warps: scores by cell (8 each), p.v by dimension (64 each = one int8 scale group)
constexpr int QS = HD + 8;        // q row stride in halves (bank-conflict-free fragment loads)

// The MMA below needs sm_75 or newer (Turing runs it as two k=8 steps); cp.async needs sm_80. Builds for pre-sm_75
// cards compile the MMA to a trap; qsa_prompt_attn_batch refuses such a device at run time, so the old kernel runs
// there.  Turing compiles cp_async16 to a trap as well and takes the v1 kernel instead of launch_i8.
#if defined(__HIPCC__)          // AMD: no mma.sync / cp.async; the host keeps the old kernel (below)
#define STRATA_PA_SM80 0
#elif !defined(__CUDA_ARCH__) || __CUDA_ARCH__ >= 800
#define STRATA_PA_SM80 1
#else
#define STRATA_PA_SM80 0
#endif

// m16n8k16 with f16 inputs needs sm_80.  Turing (sm_75) has m16n8k8 with the SAME A/B/C register mapping, so the
// k=16 step is two k=8 steps on the fragments as they are already laid out: a[0]/a[1] are rows gid/gid+8 at k columns
// 2*tig..2*tig+1 (b[0]'s k rows), a[2]/a[3] the same rows at k columns 2*tig+8..2*tig+9 (b[1]'s k rows).  The
// products then add into the same FP32 C registers in the order hi-part-0, hi-part-1, which is the order the k16
// instruction accumulates in as well - but the sum now rounds twice, so the two paths do not agree bit for bit.
__device__ __forceinline__ void mma16816(float* c, const uint32_t* a, const uint32_t* b) {
#if !STRATA_PA_SM80 && (defined(__HIPCC__) || !defined(__CUDA_ARCH__) || __CUDA_ARCH__ < 750)
    __trap();   // AMD and pre-Turing builds: no mma.sync (the host keeps the old kernel there)
#elif !STRATA_PA_SM80
    asm volatile("mma.sync.aligned.m16n8k8.row.col.f32.f16.f16.f32 {%0,%1,%2,%3}, {%4,%5}, {%6}, {%0,%1,%2,%3};\n"
                 : "+f"(c[0]), "+f"(c[1]), "+f"(c[2]), "+f"(c[3])
                 : "r"(a[0]), "r"(a[1]), "r"(b[0]));
    asm volatile("mma.sync.aligned.m16n8k8.row.col.f32.f16.f16.f32 {%0,%1,%2,%3}, {%4,%5}, {%6}, {%0,%1,%2,%3};\n"
                 : "+f"(c[0]), "+f"(c[1]), "+f"(c[2]), "+f"(c[3])
                 : "r"(a[2]), "r"(a[3]), "r"(b[1]));
#else
    asm volatile("mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32 {%0,%1,%2,%3}, {%4,%5,%6,%7}, {%8,%9}, "
                 "{%0,%1,%2,%3};\n"
                 : "+f"(c[0]), "+f"(c[1]), "+f"(c[2]), "+f"(c[3])
                 : "r"(a[0]), "r"(a[1]), "r"(a[2]), "r"(a[3]), "r"(b[0]), "r"(b[1]));
#endif
}

// Two int8 codes (low byte first) as an exact half2: 1024 + (c + 128) built in the mantissa, minus 1152.
__device__ __forceinline__ uint32_t i8x2_to_h2(uint32_t x) {
    uint32_t y = ((x & 0xffu) | ((x & 0xff00u) << 8)) ^ 0x00800080u;
    y |= 0x64006400u;
    __half2 h = *reinterpret_cast<__half2*>(&y);
    h = __hsub2(h, __halves2half2(__float2half(1152.f), __float2half(1152.f)));
    return *reinterpret_cast<uint32_t*>(&h);
}

__device__ __forceinline__ uint32_t pack_h2(float lo_k, float hi_k) {   // element k in the low half
    __half2 h = __floats2half2_rn(lo_k, hi_k);
    return *reinterpret_cast<uint32_t*>(&h);
}

// KV_MODE 1: int8 codes + fp16 scale per 64 values. KV_MODE 0: fp16 values (scales 1).
// KV_MODE 3 (hybrid K8V4): K as mode 1, V as mode 0 - the row's q4_0 blocks are dequantized to fp16 at
// gather, so everything downstream of the load is the mode-0 V path; the caller un-rotates the output.
// KV_MODE 4 (Q4_0 K and V, `--kv q4_0`): mode 1 with a scale per 32 values - each q4_0 block's codes enter as exact
// int8 (code - 8) and its fp16 scale is applied in FP32, as mode 1's are; the caller rotates q and un-rotates the
// output (kv_q4.hpp).
template <int KV_MODE>
struct Smem {
    using KElem = typename std::conditional<KV_MODE == 0, __half, int8_t>::type;
    using VElem = typename std::conditional<KV_MODE == 1 || KV_MODE == 4, int8_t, __half>::type;
    static constexpr int KROW = KV_MODE == 0 ? HD + 8 : HD + 16;   // elements; 16-byte aligned rows, banks spread
    static constexpr int VROW = (KV_MODE == 1 || KV_MODE == 4) ? HD + 16 : HD + 8;
    static constexpr int NG = KV_MODE == 4 ? HD / QK4_0 : 4;      // scale groups per row (q4_0: 8 of 32, else 4 of 64)
    __half qh[16][QS];
    __half ql[16][QS];
    KElem k[CH][KROW];
    VElem v[CH][VROW];
    float ks[CH][NG];
    float vs[CH][NG];
    float s[16][CH + 1];
    float qmax[THREADS / 32];
    float alpha[16];
    float lsum[16];
    float mrow[16];
    long long row[CH];
};

template <int KV_MODE>
__global__ void __launch_bounds__(THREADS) prompt_attn_kernel(const float* __restrict__ q, QsaAttnPools p,
                                                              const int32_t* __restrict__ ids,
                                                              const int32_t* __restrict__ steps, int n_kv_heads,
                                                              int page_size, float scale_log2, float* __restrict__ attn,
                                                              int cap) {
    extern __shared__ __align__(16) unsigned char smem_raw[];
    Smem<KV_MODE>& S = *reinterpret_cast<Smem<KV_MODE>*>(smem_raw);
    const int qi = blockIdx.x, kvh = blockIdx.y;
    const int n_head = n_kv_heads * G;
    q += (size_t) qi * n_head * HD + (size_t) kvh * G * HD;
    attn += (size_t) qi * n_head * HD + (size_t) kvh * G * HD;
    ids += (size_t) qi * cap;
    const int n = __ldg(steps + (size_t) qi * kStepCount + kStepWidth);
    const int t = threadIdx.x, lane = t & 31, warp = t >> 5;
    const int gid = lane >> 2, tig = lane & 3;

    // q: 12 heads + 4 zero rows, scaled by a power of two that puts its largest value near 2^14 (exact, and the
    // lo halves stay out of FP16's subnormal range), then split into hi + lo halves
    float qm = 0.0f;
    for (int i = t; i < G * HD; i += THREADS) qm = fmaxf(qm, fabsf(q[i]));
#pragma unroll
    for (int o = 16; o > 0; o >>= 1) qm = fmaxf(qm, __shfl_xor_sync(0xffffffffu, qm, o));
    if (lane == 0) S.qmax[warp] = qm;
    __syncthreads();
    qm = fmaxf(fmaxf(S.qmax[0], S.qmax[1]), fmaxf(S.qmax[2], S.qmax[3]));
    int qe = 0;
    if (qm > 0.0f) frexpf(qm, &qe);                 // qm < 2^qe
    const float qup = ldexpf(1.0f, 14 - qe), qdown = ldexpf(scale_log2, qe - 14);
    for (int i = t; i < 16 * HD; i += THREADS) {
        const int h = i / HD, d = i % HD;
        const float x = h < G ? q[(size_t) h * HD + d] * qup : 0.0f;
        const __half hi = __float2half_rn(x);
        S.qh[h][d] = hi;
        S.ql[h][d] = __float2half_rn(x - __half2float(hi));
    }
    if (t < 16) { S.mrow[t] = -INFINITY; S.lsum[t] = 0.0f; }

    float acc[8][4];
#pragma unroll
    for (int j = 0; j < 8; ++j) acc[j][0] = acc[j][1] = acc[j][2] = acc[j][3] = 0.0f;

    for (int c0 = 0; c0 < n; c0 += CH) {
        const int nh = min(CH, n - c0);
        if (t < CH) {
            long long r = -1;
            if (t < nh) {
                const int cell = ids[c0 + t];
                const long long page = (long long) p.page_table[cell / page_size];
                r = (page * n_kv_heads + kvh) * page_size + (cell % page_size);
            }
            S.row[t] = r;
        }
        __syncthreads();   // rows ready; the previous chunk's p.v is done with k, v, s
        // gather the chunk's K and V rows (16-byte pieces; K8V4's V as q4_0 blocks dequantized to fp16)
        // and their scales
        if constexpr (KV_MODE == 4) {
            // q4_0 K and V: one (K or V, cell, block) per item - the block's 16 code bytes (2-byte aligned: eight
            // 16-bit loads) become 32 exact int8 codes (element j in the low nibble of byte j, j + 16 in the high
            // one), its fp16 scale goes to ks/vs
            constexpr int BLKS = HD / QK4_0;
            constexpr int BYTES = BLKS * (int) sizeof(block_q4_0);
            for (int i = t; i < 2 * CH * BLKS; i += THREADS) {
                const int kv = i / (CH * BLKS), rem = i % (CH * BLKS), c = rem / BLKS, b = rem % BLKS;
                const long long r = S.row[c];
                uint4 lo = make_uint4(0, 0, 0, 0), hi = make_uint4(0, 0, 0, 0);
                float d = 0.0f;
                if (r >= 0) {
                    const block_q4_0* blk = reinterpret_cast<const block_q4_0*>((kv == 0 ? p.k_q4 : p.v_q4) + r * BYTES) + b;
                    d = __half2float(__ushort_as_half(__ldg(&blk->d)));
                    const uint16_t* q16 = reinterpret_cast<const uint16_t*>(blk->qs);
                    uint32_t w[4];
#pragma unroll
                    for (int j = 0; j < 4; ++j)
                        w[j] = (uint32_t) __ldg(q16 + 2 * j) | ((uint32_t) __ldg(q16 + 2 * j + 1) << 16);
                    // per byte: the low nibble minus 8 is element j, the high one minus 8 element j + 16
                    lo = make_uint4(__vsub4(w[0] & 0x0F0F0F0Fu, 0x08080808u), __vsub4(w[1] & 0x0F0F0F0Fu, 0x08080808u),
                                    __vsub4(w[2] & 0x0F0F0F0Fu, 0x08080808u), __vsub4(w[3] & 0x0F0F0F0Fu, 0x08080808u));
                    hi = make_uint4(__vsub4((w[0] >> 4) & 0x0F0F0F0Fu, 0x08080808u),
                                    __vsub4((w[1] >> 4) & 0x0F0F0F0Fu, 0x08080808u),
                                    __vsub4((w[2] >> 4) & 0x0F0F0F0Fu, 0x08080808u),
                                    __vsub4((w[3] >> 4) & 0x0F0F0F0Fu, 0x08080808u));
                }
                int8_t* dst = kv == 0 ? &S.k[c][b * QK4_0] : &S.v[c][b * QK4_0];
                reinterpret_cast<uint4*>(dst)[0] = lo;
                reinterpret_cast<uint4*>(dst)[1] = hi;
                (kv == 0 ? S.ks : S.vs)[c][b] = d;
            }
        } else {
            constexpr int KPIECES = HD * (int) sizeof(typename Smem<KV_MODE>::KElem) / 16;   // per K row
            for (int i = t; i < CH * KPIECES; i += THREADS) {
                const int c = i / KPIECES, pc = i % KPIECES;
                const long long r = S.row[c];
                uint4 kx = make_uint4(0, 0, 0, 0);
                if (r >= 0) {
                    if constexpr (KV_MODE == 0)
                        kx = __ldg(reinterpret_cast<const uint4*>(p.k_pool + r * HD) + pc);
                    else   // modes 1 and 3: the K side is INT8
                        kx = __ldg(reinterpret_cast<const uint4*>(p.k_q + r * HD) + pc);
                }
                *reinterpret_cast<uint4*>(reinterpret_cast<unsigned char*>(&S.k[c][0]) + pc * 16) = kx;
            }
            if constexpr (KV_MODE == 3) {   // V: dequantize the row's q4_0 blocks straight into the fp16 V row
                constexpr int BLKS = HD / QK4_0;
                constexpr int BYTES = BLKS * (int) sizeof(block_q4_0);
                for (int i = t; i < CH * BLKS; i += THREADS) {
                    const int c = i / BLKS, b = i % BLKS;
                    const long long r = S.row[c];
#pragma unroll
                    for (int j = 0; j < QK4_0; ++j) S.v[c][b * QK4_0 + j] = __half(0);
                    if (r >= 0) {
                        const block_q4_0* blk = reinterpret_cast<const block_q4_0*>(p.v_q4 + r * BYTES) + b;
                        const float d = __half2float(__ushort_as_half(blk->d));
#pragma unroll
                        for (int j = 0; j < QK4_0 / 2; ++j) {
                            S.v[c][b * QK4_0 + j] = __float2half_rn((float) ((int)(blk->qs[j] & 0x0F) - 8) * d);
                            S.v[c][b * QK4_0 + j + QK4_0 / 2] =
                                __float2half_rn((float) ((int)(blk->qs[j] >> 4) - 8) * d);
                        }
                    }
                }
            } else {
                constexpr int VPIECES = HD * (int) sizeof(typename Smem<KV_MODE>::VElem) / 16;   // per V row
                for (int i = t; i < CH * VPIECES; i += THREADS) {
                    const int c = i / VPIECES, pc = i % VPIECES;
                    const long long r = S.row[c];
                    uint4 vx = make_uint4(0, 0, 0, 0);
                    if (r >= 0) {
                        if constexpr (KV_MODE == 1)
                            vx = __ldg(reinterpret_cast<const uint4*>(p.v_q + r * HD) + pc);
                        else
                            vx = __ldg(reinterpret_cast<const uint4*>(p.v_pool + r * HD) + pc);
                    }
                    *reinterpret_cast<uint4*>(reinterpret_cast<unsigned char*>(&S.v[c][0]) + pc * 16) = vx;
                }
            }
            for (int i = t; i < CH * 4; i += THREADS) {
                const int c = i / 4, g = i % 4;
                const long long r = S.row[c];
                float a = 0.0f, b = 0.0f;
                if (r >= 0) {
                    if constexpr (KV_MODE == 1) {
                        a = __half2float(__ushort_as_half(p.k_scale[r * (HD / KV_Q8_GROUP) + g]));
                        b = __half2float(__ushort_as_half(p.v_scale[r * (HD / KV_Q8_GROUP) + g]));
                    } else if constexpr (KV_MODE == 3) {   // K as int8, V dequantized to fp16 (scale 1)
                        a = __half2float(__ushort_as_half(p.k_scale[r * (HD / KV_Q8_GROUP) + g]));
                        b = 1.0f;
                    } else {
                        a = b = 1.0f;
                    }
                }
                S.ks[c][g] = a;
                S.vs[c][g] = b;
            }
        }
        __syncthreads();
        // scores: warp w takes cells 8w..8w+7 (one n-tile) over all 256 dims, per scale group (64 dims; q4_0's 32)
        constexpr int NG = Smem<KV_MODE>::NG, KPG = HD / 16 / NG;   // groups per row, 16-dim MMA steps per group
#pragma unroll
        for (int nt = 0; nt < CH / 32; ++nt) {
            const int cb = (warp + 4 * nt) * 8;
            float sc[4] = {0.f, 0.f, 0.f, 0.f};
#pragma unroll
            for (int g = 0; g < NG; ++g) {
                float tg[4] = {0.f, 0.f, 0.f, 0.f};
#pragma unroll
                for (int kk = 0; kk < KPG; ++kk) {
                    const int k0 = (g * KPG + kk) * 16;
                    uint32_t ah[4], al[4], b[2];
                    ah[0] = *reinterpret_cast<const uint32_t*>(&S.qh[gid][k0 + 2 * tig]);
                    ah[1] = *reinterpret_cast<const uint32_t*>(&S.qh[gid + 8][k0 + 2 * tig]);
                    ah[2] = *reinterpret_cast<const uint32_t*>(&S.qh[gid][k0 + 2 * tig + 8]);
                    ah[3] = *reinterpret_cast<const uint32_t*>(&S.qh[gid + 8][k0 + 2 * tig + 8]);
                    al[0] = *reinterpret_cast<const uint32_t*>(&S.ql[gid][k0 + 2 * tig]);
                    al[1] = *reinterpret_cast<const uint32_t*>(&S.ql[gid + 8][k0 + 2 * tig]);
                    al[2] = *reinterpret_cast<const uint32_t*>(&S.ql[gid][k0 + 2 * tig + 8]);
                    al[3] = *reinterpret_cast<const uint32_t*>(&S.ql[gid + 8][k0 + 2 * tig + 8]);
                    if constexpr (KV_MODE != 0) {   // modes 1 and 3: the K side is INT8 codes
                        b[0] = i8x2_to_h2(*reinterpret_cast<const uint16_t*>(&S.k[cb + gid][k0 + 2 * tig]));
                        b[1] = i8x2_to_h2(*reinterpret_cast<const uint16_t*>(&S.k[cb + gid][k0 + 2 * tig + 8]));
                    } else {
                        b[0] = *reinterpret_cast<const uint32_t*>(&S.k[cb + gid][k0 + 2 * tig]);
                        b[1] = *reinterpret_cast<const uint32_t*>(&S.k[cb + gid][k0 + 2 * tig + 8]);
                    }
                    mma16816(tg, ah, b);
#ifndef D1_NO_QLO
                    mma16816(tg, al, b);
#endif
                }
                const float s0 = S.ks[cb + 2 * tig][g], s1 = S.ks[cb + 2 * tig + 1][g];
                sc[0] = fmaf(tg[0], s0, sc[0]);
                sc[1] = fmaf(tg[1], s1, sc[1]);
                sc[2] = fmaf(tg[2], s0, sc[2]);
                sc[3] = fmaf(tg[3], s1, sc[3]);
            }
            const int c = cb + 2 * tig;
            S.s[gid][c] = c < nh ? sc[0] * qdown : -INFINITY;
            S.s[gid][c + 1] = c + 1 < nh ? sc[1] * qdown : -INFINITY;
            S.s[gid + 8][c] = c < nh ? sc[2] * qdown : -INFINITY;
            S.s[gid + 8][c + 1] = c + 1 < nh ? sc[3] * qdown : -INFINITY;
        }
        __syncthreads();
        // online softmax: row t/8, 4 cells per thread, 8 threads per row (lanes 8r..8r+7 of a warp)
        {
            constexpr int PER = CH / 8;
            const int r = t >> 3, sub = t & 7;
            float x[PER], mx = -INFINITY;
#pragma unroll
            for (int j = 0; j < PER; ++j) { x[j] = S.s[r][sub * PER + j]; mx = fmaxf(mx, x[j]); }
#pragma unroll
            for (int o = 1; o < 8; o <<= 1) mx = fmaxf(mx, __shfl_xor_sync(0xffffffffu, mx, o));
            const float m_old = S.mrow[r];
            const float m_new = fmaxf(m_old, mx);
            float sum = 0.0f;
#pragma unroll
            for (int j = 0; j < PER; ++j) {
                const float e = x[j] == -INFINITY ? 0.0f : exp2f(x[j] - m_new);
                S.s[r][sub * PER + j] = e;
                sum += e;
            }
#pragma unroll
            for (int o = 1; o < 8; o <<= 1) sum += __shfl_xor_sync(0xffffffffu, sum, o);
            __syncwarp();
            if (sub == 0) {
                const float a = m_old == -INFINITY ? 0.0f : exp2f(m_old - m_new);
                S.alpha[r] = a;
                S.lsum[r] = fmaf(S.lsum[r], a, sum);
                S.mrow[r] = m_new;
            }
        }
        __syncthreads();
        // p.v: warp w owns dims [64w, 64w+64), which is int8 scale group w (q4_0: groups 2w and 2w+1, four n-tiles
        // each). A group's scale is folded into p relative to the chunk's largest magnitude (q4_0's scales are signed:
        // ggml's d = max / -8), times 2^14 (|p'| <= 2^14: inside FP16's range, its lo half out of the subnormals); the
        // chunk's sum is then added to the running one in FP32 with the factor taken back out
        {
            constexpr int GPW = NG / 4, JPG = 8 / GPW;   // scale groups per warp, n-tiles per group
            float vdown_g[GPW];
            float tmp[8][4];
#pragma unroll
            for (int j = 0; j < 8; ++j) tmp[j][0] = tmp[j][1] = tmp[j][2] = tmp[j][3] = 0.0f;
#pragma unroll
            for (int gi = 0; gi < GPW; ++gi) {
            const int vg = warp * GPW + gi;
            float vmax = 0.0f;
#pragma unroll
            for (int c = lane; c < CH; c += 32) vmax = fmaxf(vmax, fabsf(S.vs[c][vg]));
#pragma unroll
            for (int o = 16; o > 0; o >>= 1) vmax = fmaxf(vmax, __shfl_xor_sync(0xffffffffu, vmax, o));
            const float vup = vmax > 0.0f ? 16384.0f / vmax : 0.0f;
            vdown_g[gi] = vmax * (1.0f / 16384.0f);
#pragma unroll
            for (int ks = 0; ks < CH / 16; ++ks) {
                const int cA = ks * 16 + 2 * tig, cB = cA + 8;
                const float w0 = S.vs[cA][vg] * vup, w1 = S.vs[cA + 1][vg] * vup, w2 = S.vs[cB][vg] * vup,
                            w3 = S.vs[cB + 1][vg] * vup;
                const float p00 = S.s[gid][cA] * w0, p01 = S.s[gid][cA + 1] * w1;
                const float p10 = S.s[gid + 8][cA] * w0, p11 = S.s[gid + 8][cA + 1] * w1;
                const float p02 = S.s[gid][cB] * w2, p03 = S.s[gid][cB + 1] * w3;
                const float p12 = S.s[gid + 8][cB] * w2, p13 = S.s[gid + 8][cB + 1] * w3;
                uint32_t ah[4], al[4];
                ah[0] = pack_h2(p00, p01);
                ah[1] = pack_h2(p10, p11);
                ah[2] = pack_h2(p02, p03);
                ah[3] = pack_h2(p12, p13);
                {
                    const __half2* h = reinterpret_cast<const __half2*>(ah);
                    float2 f;
                    f = __half22float2(h[0]); al[0] = pack_h2(p00 - f.x, p01 - f.y);
                    f = __half22float2(h[1]); al[1] = pack_h2(p10 - f.x, p11 - f.y);
                    f = __half22float2(h[2]); al[2] = pack_h2(p02 - f.x, p03 - f.y);
                    f = __half22float2(h[3]); al[3] = pack_h2(p12 - f.x, p13 - f.y);
                }
#pragma unroll
                for (int jj = 0; jj < JPG; ++jj) {
                    const int j = gi * JPG + jj;
                    const int d = warp * 64 + j * 8 + gid;
                    uint32_t b[2];
                    if constexpr (KV_MODE == 1 || KV_MODE == 4) {
                        const uint32_t x0 = (uint8_t) S.v[cA][d] | ((uint32_t) (uint8_t) S.v[cA + 1][d] << 8);
                        const uint32_t x1 = (uint8_t) S.v[cB][d] | ((uint32_t) (uint8_t) S.v[cB + 1][d] << 8);
                        b[0] = i8x2_to_h2(x0);
                        b[1] = i8x2_to_h2(x1);
                    } else {
                        const __half2 h0 = __halves2half2(S.v[cA][d], S.v[cA + 1][d]);
                        const __half2 h1 = __halves2half2(S.v[cB][d], S.v[cB + 1][d]);
                        b[0] = *reinterpret_cast<const uint32_t*>(&h0);
                        b[1] = *reinterpret_cast<const uint32_t*>(&h1);
                    }
                    mma16816(tmp[j], ah, b);
#ifndef D1_NO_PLO
                    mma16816(tmp[j], al, b);
#endif
                }
            }
            }
            const float a0 = S.alpha[gid], a1 = S.alpha[gid + 8];
#pragma unroll
            for (int j = 0; j < 8; ++j) {
                const float vdown = vdown_g[j / JPG];
                acc[j][0] = fmaf(acc[j][0], a0, tmp[j][0] * vdown);
                acc[j][1] = fmaf(acc[j][1], a0, tmp[j][1] * vdown);
                acc[j][2] = fmaf(acc[j][2], a1, tmp[j][2] * vdown);
                acc[j][3] = fmaf(acc[j][3], a1, tmp[j][3] * vdown);
            }
        }
    }
    __syncthreads();
    const float l0 = S.lsum[gid], l1 = S.lsum[gid + 8];
    const float i0 = l0 > 0.0f ? 1.0f / l0 : 0.0f, i1 = l1 > 0.0f ? 1.0f / l1 : 0.0f;
#pragma unroll
    for (int j = 0; j < 8; ++j) {
        const int d = warp * 64 + j * 8 + 2 * tig;
        *reinterpret_cast<float2*>(attn + (size_t) gid * HD + d) = make_float2(acc[j][0] * i0, acc[j][1] * i0);
        if (gid + 8 < G)
            *reinterpret_cast<float2*>(attn + (size_t) (gid + 8) * HD + d) = make_float2(acc[j][2] * i1, acc[j][3] * i1);
    }
}

// ---- v2 (int8 KV): warp w owns dims [64w, 64w+64) for both q.k and p.v, which is also int8 scale group w. So a
// warp needs only its own 64-byte slice of each K and V row: it gathers it itself with cp.async into its own
// double-buffered stage while it computes the previous chunk, and q stays in registers. Only the q.k partial sums
// (one per dim group) cross warps, and they are added in a fixed order: the result is deterministic.
constexpr int CH2 = 32;

struct Smem2 {
    int8_t kv[2][4][2][CH2][64];   // stage, warp, K/V, cell, 64 dims in 16-byte pieces XOR-swizzled by the cell
    float sc[2][4][2][CH2];        // stage, warp, K/V scale of the cell for the warp's group
    float part[4][16][CH2 + 1];    // q.k per dim group
    float p[16][CH2 + 1];
    int ok[2][CH2];                // stage, cell: the cell has a pool row (past the selection or a block the KV
                                   // streaming left non-resident: no weight, as in the decode kernel)
    float qmax[4];
    float alpha[16];
    float lsum[16];
    float mrow[16];
};

__device__ __forceinline__ int swz(int cell, int byte) {   // byte offset of (cell, byte) in a stage slice
    return cell * 64 + ((((byte >> 4) ^ (cell >> 1)) & 3) << 4) + (byte & 15);
}
__device__ __forceinline__ void cp_async16(void* smem, const void* gmem, bool valid) {
#if !STRATA_PA_SM80
    __trap();
#else
    const unsigned sa = (unsigned) __cvta_generic_to_shared(smem);
    asm volatile("cp.async.cg.shared.global [%0], [%1], 16, %2;\n" ::"r"(sa), "l"(gmem), "r"(valid ? 16 : 0));
#endif
}
__device__ __forceinline__ void cp_async_commit() {
#if STRATA_PA_SM80
    asm volatile("cp.async.commit_group;\n" ::);
#endif
}
__device__ __forceinline__ void cp_async_wait1() {
#if STRATA_PA_SM80
    asm volatile("cp.async.wait_group 1;\n" ::);
#endif
}

// ---- The decode form (SPLIT, qsa_decode_attn_tc): a verify window has only a few queries, so one block per (query,
// KV head) leaves most of the GPU idle while each walks ~2,000 cells. SPLIT adds blockIdx.z: block z walks its own
// range of the query's chunks and writes that range's unnormalized output, its row maximum (natural-log units) and
// exp-sum - the split-K scratch layout of qsa_decode_attn_batch - and split_merge_kernel combines the ranges.
struct SplitOut {
    float* part = nullptr;   // per query: [acc: nsplit*n_head*HD][m: nsplit*n_head][l: nsplit*n_head]
    long long stride = 0;    // floats per query
    int nsplit = 1;
};

// This block's chunks: an equal share of the selection's capacity, cut at the query's own width
__device__ __forceinline__ void split_range(int n, int cap, int nsplit, int z, int& chunk0, int& n_chunks) {
    const int per = ((cap + CH2 - 1) / CH2 + nsplit - 1) / nsplit;
    chunk0 = z * per;
    n_chunks = max(0, min((n + CH2 - 1) / CH2 - chunk0, per));
}

// The range's partial result: acc as accumulated (rows gid, gid + 8 of the m16 tile; dims dim0 + 8j + 2tig + {0,1}),
// m = the row's running maximum in log2 units (ln 2 * it in the merge's natural units), l = its exp-sum
__device__ __forceinline__ void write_split(const float (&acc)[8][4], const float* mrow, const float* lsum, SplitOut so,
                                            int qi, int kvh, int n_kv_heads, int dim0, int gid, int tig, int t) {
    const int n_head = n_kv_heads * G, slot = kvh * so.nsplit + (int) blockIdx.z;
    float* base = so.part + (size_t) qi * (size_t) so.stride;
    float* pacc = base + (size_t) slot * G * HD;
    float* pm = base + (size_t) so.nsplit * n_head * HD + (size_t) slot * G;
    float* pl = base + (size_t) so.nsplit * n_head * HD + (size_t) so.nsplit * n_head + (size_t) slot * G;
#pragma unroll
    for (int j = 0; j < 8; ++j) {
        const int d = dim0 + j * 8 + 2 * tig;
        *reinterpret_cast<float2*>(pacc + (size_t) gid * HD + d) = make_float2(acc[j][0], acc[j][1]);
        if (gid + 8 < G) *reinterpret_cast<float2*>(pacc + (size_t) (gid + 8) * HD + d) = make_float2(acc[j][2], acc[j][3]);
    }
    if (t < G) {
        const bool any = mrow != nullptr && lsum[t] > 0.0f && mrow[t] != -INFINITY;
        pm[t] = any ? mrow[t] * 0.6931471805599453f : -FLT_MAX;
        pl[t] = any ? lsum[t] : 0.0f;
    }
}

// An empty range (past the query's width): no weight in the merge
__device__ __forceinline__ void write_split_empty(SplitOut so, int qi, int kvh, int n_kv_heads, int t) {
    const int n_head = n_kv_heads * G, slot = kvh * so.nsplit + (int) blockIdx.z;
    float* base = so.part + (size_t) qi * (size_t) so.stride;
    if (t < G) {
        base[(size_t) so.nsplit * n_head * HD + (size_t) slot * G + t] = -FLT_MAX;
        base[(size_t) so.nsplit * n_head * HD + (size_t) so.nsplit * n_head + (size_t) slot * G + t] = 0.0f;
    }
}

template <bool SPLIT = false>
__global__ void __launch_bounds__(THREADS) prompt_attn_i8_kernel(const float* __restrict__ q, QsaAttnPools p,
                                                                 const int32_t* __restrict__ ids,
                                                                 const int32_t* __restrict__ steps, int n_kv_heads,
                                                                 int page_size, float scale_log2,
                                                                 float* __restrict__ attn, int cap,
                                                                 SplitOut so = SplitOut{}) {
    extern __shared__ __align__(16) unsigned char smem_raw[];
    Smem2& S = *reinterpret_cast<Smem2*>(smem_raw);
    const int qi = blockIdx.x, kvh = blockIdx.y;
    const int n_head = n_kv_heads * G;
    q += (size_t) qi * n_head * HD + (size_t) kvh * G * HD;
    attn += (size_t) qi * n_head * HD + (size_t) kvh * G * HD;
    ids += (size_t) qi * cap;
    const int n = __ldg(steps + (size_t) qi * kStepCount + kStepWidth);
    const int t = threadIdx.x, lane = t & 31, warp = t >> 5;
    const int gid = lane >> 2, tig = lane & 3;
    const int dim0 = warp * 64;
    if constexpr (SPLIT) {
        int c0s, ncs;
        split_range(n, cap, so.nsplit, (int) blockIdx.z, c0s, ncs);
        if (ncs == 0) { write_split_empty(so, qi, kvh, n_kv_heads, t); return; }
    }

    // q: the power-of-two prescale over all 12 heads (as v1), then this warp's 64 dims as hi/lo A fragments
    float qm = 0.0f;
    for (int i = t; i < G * HD; i += THREADS) qm = fmaxf(qm, fabsf(q[i]));
#pragma unroll
    for (int o = 16; o > 0; o >>= 1) qm = fmaxf(qm, __shfl_xor_sync(0xffffffffu, qm, o));
    if (lane == 0) S.qmax[warp] = qm;
    if (t < 16) { S.mrow[t] = -INFINITY; S.lsum[t] = 0.0f; }
    __syncthreads();
    qm = fmaxf(fmaxf(S.qmax[0], S.qmax[1]), fmaxf(S.qmax[2], S.qmax[3]));
    int qe = 0;
    if (qm > 0.0f) frexpf(qm, &qe);
    const float qup = ldexpf(1.0f, 14 - qe), qdown = ldexpf(scale_log2, qe - 14);
    uint32_t qh[4][4], ql[4][4];
#pragma unroll
    for (int kk = 0; kk < 4; ++kk) {
#pragma unroll
        for (int r = 0; r < 4; ++r) {
            const int row = gid + (r & 1) * 8, col = dim0 + kk * 16 + 2 * tig + (r >> 1) * 8;
            float2 x = make_float2(0.f, 0.f);
            if (row < G) x = *reinterpret_cast<const float2*>(q + (size_t) row * HD + col);
            x.x *= qup;
            x.y *= qup;
            const __half2 hi = __floats2half2_rn(x.x, x.y);
            const float2 hf = __half22float2(hi);
            const __half2 lo = __floats2half2_rn(x.x - hf.x, x.y - hf.y);
            qh[kk][r] = *reinterpret_cast<const uint32_t*>(&hi);
            ql[kk][r] = *reinterpret_cast<const uint32_t*>(&lo);
        }
    }

    // the chunk pipeline: cells two chunks ahead, their pool rows one chunk ahead, the data (cp.async) one ahead
    int chunk0 = 0, n_chunks = (n + CH2 - 1) / CH2;
    if constexpr (SPLIT) split_range(n, cap, so.nsplit, (int) blockIdx.z, chunk0, n_chunks);
    auto cell_of = [&](int c) -> int { return c < n ? __ldg(ids + c) : -1; };
    auto row_of = [&](int cell) -> long long {
        if (cell < 0) return -1;
        const long long page = (long long) __ldg(p.page_table + cell / page_size);
        return page < 0 ? -1 : (page * n_kv_heads + kvh) * page_size + (cell % page_size);
    };
    auto issue = [&](long long r, int st, float& ksr, float& vsr) {
#pragma unroll
        for (int j = 0; j < 4; ++j) {
            const int idx = lane + 32 * j, cell = idx >> 2, pc = idx & 3;
            const long long rr = __shfl_sync(0xffffffffu, r, cell);
            const bool ok = rr >= 0;
            const size_t off = ok ? (size_t) rr * HD + dim0 + pc * 16 : 0;
            cp_async16(&S.kv[st][warp][0][0][0] + swz(cell, pc * 16), p.k_q + off, ok);
            cp_async16(&S.kv[st][warp][1][0][0] + swz(cell, pc * 16), p.v_q + off, ok);
        }
        if (warp == 0) S.ok[st][lane] = r >= 0;
        ksr = r >= 0 ? __half2float(__ushort_as_half(__ldg(p.k_scale + r * (HD / KV_Q8_GROUP) + warp))) : 0.0f;
        vsr = r >= 0 ? __half2float(__ushort_as_half(__ldg(p.v_scale + r * (HD / KV_Q8_GROUP) + warp))) : 0.0f;
    };
    float ksn, vsn;
    issue(row_of(cell_of(chunk0 * CH2 + lane)), 0, ksn, vsn);
    cp_async_commit();
    S.sc[0][warp][0][lane] = ksn;
    S.sc[0][warp][1][lane] = vsn;
    long long r_next = row_of(cell_of((chunk0 + 1) * CH2 + lane));
    int cell_next2 = cell_of((chunk0 + 2) * CH2 + lane);

    float acc[8][4];
#pragma unroll
    for (int j = 0; j < 8; ++j) acc[j][0] = acc[j][1] = acc[j][2] = acc[j][3] = 0.0f;

    for (int ci = 0; ci < n_chunks; ++ci) {
        const int st = ci & 1;
        const bool more = ci + 1 < n_chunks;
        if (more) issue(r_next, st ^ 1, ksn, vsn);
        cp_async_commit();
        r_next = row_of(cell_next2);
        cell_next2 = cell_of((chunk0 + ci + 3) * CH2 + lane);
        cp_async_wait1();
        __syncwarp();
        const int8_t* K = &S.kv[st][warp][0][0][0];
        const int8_t* V = &S.kv[st][warp][1][0][0];
        // q.k over this warp's 64 dims, times the cell's K scale for this group
#pragma unroll
        for (int nt = 0; nt < CH2 / 8; ++nt) {
            float tg[4] = {0.f, 0.f, 0.f, 0.f};
            const int cell = nt * 8 + gid;
#pragma unroll
            for (int kk = 0; kk < 4; ++kk) {
                uint32_t b[2];
                b[0] = i8x2_to_h2(*reinterpret_cast<const uint16_t*>(K + swz(cell, kk * 16 + 2 * tig)));
                b[1] = i8x2_to_h2(*reinterpret_cast<const uint16_t*>(K + swz(cell, kk * 16 + 2 * tig + 8)));
                mma16816(tg, qh[kk], b);
                mma16816(tg, ql[kk], b);
            }
            const int c = nt * 8 + 2 * tig;
            const float s0 = S.sc[st][warp][0][c], s1 = S.sc[st][warp][0][c + 1];
            S.part[warp][gid][c] = tg[0] * s0;
            S.part[warp][gid][c + 1] = tg[1] * s1;
            S.part[warp][gid + 8][c] = tg[2] * s0;
            S.part[warp][gid + 8][c + 1] = tg[3] * s1;
        }
        __syncthreads();
        // online softmax over the four groups' sum (fixed order): row t/8, 4 cells per thread
        {
            const int r = t >> 3, sub = t & 7;
            float x[4], mx = -INFINITY;
#pragma unroll
            for (int j = 0; j < 4; ++j) {
                const int c = sub * 4 + j;
                x[j] = S.ok[st][c] ? (((S.part[0][r][c] + S.part[1][r][c]) + S.part[2][r][c]) + S.part[3][r][c]) * qdown
                                   : -INFINITY;
                mx = fmaxf(mx, x[j]);
            }
#pragma unroll
            for (int o = 1; o < 8; o <<= 1) mx = fmaxf(mx, __shfl_xor_sync(0xffffffffu, mx, o));
            const float m_old = S.mrow[r];
            const float m_new = fmaxf(m_old, mx);
            float sum = 0.0f;
#pragma unroll
            for (int j = 0; j < 4; ++j) {
                const float e = x[j] == -INFINITY ? 0.0f : exp2f(x[j] - m_new);
                S.p[r][sub * 4 + j] = e;
                sum += e;
            }
#pragma unroll
            for (int o = 1; o < 8; o <<= 1) sum += __shfl_xor_sync(0xffffffffu, sum, o);
            __syncwarp();
            if (sub == 0) {
                const float a = m_old == -INFINITY ? 0.0f : exp2f(m_old - m_new);
                S.alpha[r] = a;
                S.lsum[r] = fmaf(S.lsum[r], a, sum);
                S.mrow[r] = m_new;
            }
        }
        __syncthreads();
        // p.v over this warp's 64 dims (as v1)
        {
            float vmax = S.sc[st][warp][1][lane];
#pragma unroll
            for (int o = 16; o > 0; o >>= 1) vmax = fmaxf(vmax, __shfl_xor_sync(0xffffffffu, vmax, o));
            const float vup = vmax > 0.0f ? 16384.0f / vmax : 0.0f, vdown = vmax * (1.0f / 16384.0f);
            float tmp[8][4];
#pragma unroll
            for (int j = 0; j < 8; ++j) tmp[j][0] = tmp[j][1] = tmp[j][2] = tmp[j][3] = 0.0f;
#pragma unroll
            for (int ks = 0; ks < CH2 / 16; ++ks) {
                const int cA = ks * 16 + 2 * tig, cB = cA + 8;
                const float w0 = S.sc[st][warp][1][cA] * vup, w1 = S.sc[st][warp][1][cA + 1] * vup,
                            w2 = S.sc[st][warp][1][cB] * vup, w3 = S.sc[st][warp][1][cB + 1] * vup;
                const float p00 = S.p[gid][cA] * w0, p01 = S.p[gid][cA + 1] * w1;
                const float p10 = S.p[gid + 8][cA] * w0, p11 = S.p[gid + 8][cA + 1] * w1;
                const float p02 = S.p[gid][cB] * w2, p03 = S.p[gid][cB + 1] * w3;
                const float p12 = S.p[gid + 8][cB] * w2, p13 = S.p[gid + 8][cB + 1] * w3;
                uint32_t ah[4], al[4];
                ah[0] = pack_h2(p00, p01);
                ah[1] = pack_h2(p10, p11);
                ah[2] = pack_h2(p02, p03);
                ah[3] = pack_h2(p12, p13);
                {
                    const __half2* h = reinterpret_cast<const __half2*>(ah);
                    float2 f;
                    f = __half22float2(h[0]); al[0] = pack_h2(p00 - f.x, p01 - f.y);
                    f = __half22float2(h[1]); al[1] = pack_h2(p10 - f.x, p11 - f.y);
                    f = __half22float2(h[2]); al[2] = pack_h2(p02 - f.x, p03 - f.y);
                    f = __half22float2(h[3]); al[3] = pack_h2(p12 - f.x, p13 - f.y);
                }
#pragma unroll
                for (int j = 0; j < 8; ++j) {
                    const int d = j * 8 + gid;
                    const uint32_t x0 = (uint8_t) V[swz(cA, d)] | ((uint32_t) (uint8_t) V[swz(cA + 1, d)] << 8);
                    const uint32_t x1 = (uint8_t) V[swz(cB, d)] | ((uint32_t) (uint8_t) V[swz(cB + 1, d)] << 8);
                    uint32_t b[2];
                    b[0] = i8x2_to_h2(x0);
                    b[1] = i8x2_to_h2(x1);
                    mma16816(tmp[j], ah, b);
                    mma16816(tmp[j], al, b);
                }
            }
            const float a0 = S.alpha[gid], a1 = S.alpha[gid + 8];
#pragma unroll
            for (int j = 0; j < 8; ++j) {
                acc[j][0] = fmaf(acc[j][0], a0, tmp[j][0] * vdown);
                acc[j][1] = fmaf(acc[j][1], a0, tmp[j][1] * vdown);
                acc[j][2] = fmaf(acc[j][2], a1, tmp[j][2] * vdown);
                acc[j][3] = fmaf(acc[j][3], a1, tmp[j][3] * vdown);
            }
        }
        if (more) {
            S.sc[st ^ 1][warp][0][lane] = ksn;
            S.sc[st ^ 1][warp][1][lane] = vsn;
        }
        __syncwarp();
    }
    __syncthreads();
    if constexpr (SPLIT) {
        write_split(acc, S.mrow, S.lsum, so, qi, kvh, n_kv_heads, dim0, gid, tig, t);
        return;
    }
    const float l0 = S.lsum[gid], l1 = S.lsum[gid + 8];
    const float i0 = l0 > 0.0f ? 1.0f / l0 : 0.0f, i1 = l1 > 0.0f ? 1.0f / l1 : 0.0f;
#pragma unroll
    for (int j = 0; j < 8; ++j) {
        const int d = dim0 + j * 8 + 2 * tig;
        *reinterpret_cast<float2*>(attn + (size_t) gid * HD + d) = make_float2(acc[j][0] * i0, acc[j][1] * i0);
        if (gid + 8 < G)
            *reinterpret_cast<float2*>(attn + (size_t) (gid + 8) * HD + d) = make_float2(acc[j][2] * i1, acc[j][3] * i1);
    }
}

// ---- v2 for Q4_0 KV (`--kv q4_0`): the int8 v2 design above on q4_0 blocks. Warp w owns dims [64w, 64w+64), which
// are q4_0 blocks 2w and 2w+1 of each row: 36 contiguous bytes (two {fp16 d, 16 code bytes}) at byte 36w of the
// 144-byte row, 4-byte aligned, so a warp gathers its slice of K and V with 4-byte cp.async (nine words per cell)
// into its own double-buffered stage while it computes the previous chunk. The codes stay packed in shared memory and
// become exact FP16 (code - 8) as the MMA fragments are built; each block's scale is applied in FP32 as in v1's mode
// 4, and the q.k partials of the four warps are added in a fixed order: deterministic, FP32-level accuracy, another
// summation order than v1 (not bitwise equal to it).
constexpr int Q4W = 9;   // 32-bit words per cell and warp: two q4_0 blocks of 18 bytes
#ifndef D1_Q4S
#define D1_Q4S 9
#endif
constexpr int Q4S = D1_Q4S;   // the staged cell stride in words

struct Smem4 {
    uint32_t kv[2][4][2][CH2][Q4S];   // stage, warp, K/V, cell, the warp's two blocks as stored
    float part[4][16][CH2 + 1];       // q.k per dim group (warp)
    float p[16][CH2 + 1];
    int ok[2][CH2];                   // stage, cell: the cell has a pool row (as Smem2's)
    float qmax[4];
    float alpha[16];
    float lsum[16];
    float mrow[16];
};

__device__ __forceinline__ void cp_async4(void* smem, const void* gmem, bool valid) {
#if !STRATA_PA_SM80
    __trap();
#else
    const unsigned sa = (unsigned) __cvta_generic_to_shared(smem);
    asm volatile("cp.async.ca.shared.global [%0], [%1], 4, %2;\n" ::"r"(sa), "l"(gmem), "r"(valid ? 4 : 0));
#endif
}

// Two 4-bit codes (bits [0,4) and [16,20) of x) as an exact half2 of (code - 8): 1024 + code built in the mantissa,
// minus 1032.
__device__ __forceinline__ uint32_t nib2_to_h2(uint32_t x) {
    uint32_t y = (x & 0x000f000fu) | 0x64006400u;
    __half2 h = *reinterpret_cast<__half2*>(&y);
    h = __hsub2(h, __halves2half2(__float2half(1032.f), __float2half(1032.f)));
    return *reinterpret_cast<uint32_t*>(&h);
}

// The scale of block bi (0 or 1) of a staged cell slice
__device__ __forceinline__ float q4_scale(const uint32_t* cell, int bi) {
    const uint16_t bits = reinterpret_cast<const uint16_t*>(cell)[bi * 9];   // byte 18 * bi
    return __half2float(__ushort_as_half(bits));
}

// One 256-value row through the orthonormal Walsh-Hadamard transform by one warp, 8 values per lane: the same
// butterflies in the same order as kv_q4.cu's fwht256_kernel, so the same bits.
__device__ __forceinline__ void fwht256_row_warp(const float* src, float* dst, int lane) {
    float reg[8];
#pragma unroll
    for (int i = 0; i < 8; ++i) reg[i] = src[i * 32 + lane] * (1.0f / 16.0f);
#pragma unroll
    for (int h = 1; h < 32; h *= 2) {
#pragma unroll
        for (int j = 0; j < 8; ++j) {
            const float val = reg[j];
            const float val2 = __shfl_xor_sync(0xffffffffu, val, h, 32);
            reg[j] = (lane & h) == 0 ? val + val2 : val2 - val;
        }
    }
#pragma unroll
    for (int h = 32; h < HD; h *= 2) {
        const int step = h / 32;
#pragma unroll
        for (int j = 0; j < 8; j += 2 * step) {
#pragma unroll
            for (int k = 0; k < step; ++k) {
                const float x = reg[j + k];
                const float y = reg[j + k + step];
                reg[j + k] = x + y;
                reg[j + k + step] = x - y;
            }
        }
    }
#pragma unroll
    for (int i = 0; i < 8; ++i) dst[i * 32 + lane] = reg[i];
}

// ROT: q comes in unrotated and the output goes out rotated back (kv_q4.hpp's H on both, done here in shared memory
// instead of two fwht256 passes over the whole chunk in global memory). The stage buffers hold the 12 rows while the
// pipeline is not running (before the first gather and after the last), so the shared memory and occupancy stay.
template <bool ROT, bool SPLIT = false>
__global__ void __launch_bounds__(THREADS) prompt_attn_q4_kernel(const float* __restrict__ q, QsaAttnPools p,
                                                                 const int32_t* __restrict__ ids,
                                                                 const int32_t* __restrict__ steps, int n_kv_heads,
                                                                 int page_size, float scale_log2,
                                                                 float* __restrict__ attn, int cap,
                                                                 SplitOut so = SplitOut{}) {
    static_assert(!(ROT && SPLIT), "the split form leaves the rotations to the caller");
    extern __shared__ __align__(16) unsigned char smem_raw[];
    Smem4& S = *reinterpret_cast<Smem4*>(smem_raw);
    static_assert(sizeof(S.kv) >= sizeof(float) * G * HD, "the stage buffers hold the 12 rotated rows");
    float* const QR = reinterpret_cast<float*>(&S.kv[0][0][0][0][0]);   // [G][HD] while the pipeline is idle
    constexpr int ROWB = (HD / QK4_0) * (int) sizeof(block_q4_0);   // 144 bytes per row
    const int qi = blockIdx.x, kvh = blockIdx.y;
    const int n_head = n_kv_heads * G;
    q += (size_t) qi * n_head * HD + (size_t) kvh * G * HD;
    attn += (size_t) qi * n_head * HD + (size_t) kvh * G * HD;
    ids += (size_t) qi * cap;
    const int n = __ldg(steps + (size_t) qi * kStepCount + kStepWidth);
    const int t = threadIdx.x, lane = t & 31, warp = t >> 5;
    const int gid = lane >> 2, tig = lane & 3;
    const int dim0 = warp * 64;
    if constexpr (SPLIT) {
        int c0s, ncs;
        split_range(n, cap, so.nsplit, (int) blockIdx.z, c0s, ncs);
        if (ncs == 0) { write_split_empty(so, qi, kvh, n_kv_heads, t); return; }
    }

    // q: as v2 (ROT: rotated first, into shared memory)
    const float* qs = q;
    if constexpr (ROT) {
        for (int r = warp; r < G; r += THREADS / 32) fwht256_row_warp(q + (size_t) r * HD, QR + r * HD, lane);
        __syncthreads();
        qs = QR;
    }
    float qm = 0.0f;
    for (int i = t; i < G * HD; i += THREADS) qm = fmaxf(qm, fabsf(qs[i]));
#pragma unroll
    for (int o = 16; o > 0; o >>= 1) qm = fmaxf(qm, __shfl_xor_sync(0xffffffffu, qm, o));
    if (lane == 0) S.qmax[warp] = qm;
    if (t < 16) { S.mrow[t] = -INFINITY; S.lsum[t] = 0.0f; }
    __syncthreads();
    qm = fmaxf(fmaxf(S.qmax[0], S.qmax[1]), fmaxf(S.qmax[2], S.qmax[3]));
    int qe = 0;
    if (qm > 0.0f) frexpf(qm, &qe);
    const float qup = ldexpf(1.0f, 14 - qe), qdown = ldexpf(scale_log2, qe - 14);
    uint32_t qh[4][4], ql[4][4];
#pragma unroll
    for (int kk = 0; kk < 4; ++kk) {
#pragma unroll
        for (int r = 0; r < 4; ++r) {
            const int row = gid + (r & 1) * 8, col = dim0 + kk * 16 + 2 * tig + (r >> 1) * 8;
            float2 x = make_float2(0.f, 0.f);
            if (row < G) x = *reinterpret_cast<const float2*>(qs + (size_t) row * HD + col);
            x.x *= qup;
            x.y *= qup;
            const __half2 hi = __floats2half2_rn(x.x, x.y);
            const float2 hf = __half22float2(hi);
            const __half2 lo = __floats2half2_rn(x.x - hf.x, x.y - hf.y);
            qh[kk][r] = *reinterpret_cast<const uint32_t*>(&hi);
            ql[kk][r] = *reinterpret_cast<const uint32_t*>(&lo);
        }
    }
    if constexpr (ROT) __syncthreads();   // every warp has its q fragments: the stage buffers go to the gathers

    // the chunk pipeline (as v2): cells two chunks ahead, their pool rows one chunk ahead, the data one ahead
    int chunk0 = 0, n_chunks = (n + CH2 - 1) / CH2;
    if constexpr (SPLIT) split_range(n, cap, so.nsplit, (int) blockIdx.z, chunk0, n_chunks);
    auto cell_of = [&](int c) -> int { return c < n ? __ldg(ids + c) : -1; };
    auto row_of = [&](int cell) -> long long {
        if (cell < 0) return -1;
        const long long page = (long long) __ldg(p.page_table + cell / page_size);
        return page < 0 ? -1 : (page * n_kv_heads + kvh) * page_size + (cell % page_size);
    };
    const uint8_t* kbase = p.k_q4 + warp * 36;
    const uint8_t* vbase = p.v_q4 + warp * 36;
    auto issue = [&](long long r, int st) {
        if (warp == 0) S.ok[st][lane] = r >= 0;
#pragma unroll
        for (int j = 0; j < Q4W; ++j) {   // 32 cells x 9 words: word idx = lane + 32 j
            const int idx = lane + 32 * j, cell = idx / Q4W, w = idx % Q4W;
            const long long rr = __shfl_sync(0xffffffffu, r, cell);
            const bool ok = rr >= 0;
            const size_t off = ok ? (size_t) rr * ROWB + w * 4 : 0;
            cp_async4(&S.kv[st][warp][0][cell][w], kbase + off, ok);
            cp_async4(&S.kv[st][warp][1][cell][w], vbase + off, ok);
        }
    };
    issue(row_of(cell_of(chunk0 * CH2 + lane)), 0);
    cp_async_commit();
    long long r_next = row_of(cell_of((chunk0 + 1) * CH2 + lane));
    int cell_next2 = cell_of((chunk0 + 2) * CH2 + lane);

    float acc[8][4];
#pragma unroll
    for (int j = 0; j < 8; ++j) acc[j][0] = acc[j][1] = acc[j][2] = acc[j][3] = 0.0f;

    for (int ci = 0; ci < n_chunks; ++ci) {
        const int st = ci & 1;
        if (ci + 1 < n_chunks) issue(r_next, st ^ 1);
        cp_async_commit();
        r_next = row_of(cell_next2);
        cell_next2 = cell_of((chunk0 + ci + 3) * CH2 + lane);
        cp_async_wait1();
        __syncwarp();
        const uint32_t(*K)[Q4S] = S.kv[st][warp][0];
        const uint32_t(*V)[Q4S] = S.kv[st][warp][1];
        // q.k over this warp's 64 dims (two q4_0 blocks), each block's sum times the cell's scale for it
#pragma unroll
        for (int nt = 0; nt < CH2 / 8; ++nt) {
            const int cell = nt * 8 + gid;
            const uint16_t* kc = reinterpret_cast<const uint16_t*>(K[cell]);
            float sc0 = 0.f, sc1 = 0.f, sc2 = 0.f, sc3 = 0.f;
#pragma unroll
            for (int bi = 0; bi < 2; ++bi) {
                float tg[4] = {0.f, 0.f, 0.f, 0.f};
                // the block's code bytes 2tig, 2tig+1 (b[0]) and 2tig+8, 2tig+9 (b[1]) at byte 18 bi + 2
                const uint32_t x0 = kc[bi * 9 + 1 + tig], x1 = kc[bi * 9 + 5 + tig];
                const uint32_t y0 = (x0 & 0xffu) | (x0 << 8 & 0xff0000u), y1 = (x1 & 0xffu) | (x1 << 8 & 0xff0000u);
#pragma unroll
                for (int nh = 0; nh < 2; ++nh) {   // elements 0..15 (low nibbles), then 16..31 (high)
                    const int kk = bi * 2 + nh;
                    uint32_t b[2];
                    b[0] = nib2_to_h2(y0 >> (4 * nh));
                    b[1] = nib2_to_h2(y1 >> (4 * nh));
                    mma16816(tg, qh[kk], b);
                    mma16816(tg, ql[kk], b);
                }
                const int c = nt * 8 + 2 * tig;
                const float s0 = q4_scale(K[c], bi), s1 = q4_scale(K[c + 1], bi);
                sc0 = fmaf(tg[0], s0, sc0);
                sc1 = fmaf(tg[1], s1, sc1);
                sc2 = fmaf(tg[2], s0, sc2);
                sc3 = fmaf(tg[3], s1, sc3);
            }
            const int c = nt * 8 + 2 * tig;
            S.part[warp][gid][c] = sc0;
            S.part[warp][gid][c + 1] = sc1;
            S.part[warp][gid + 8][c] = sc2;
            S.part[warp][gid + 8][c + 1] = sc3;
        }
        __syncthreads();
        // online softmax over the four groups' sum (fixed order): row t/8, 4 cells per thread (as v2)
        {
            const int r = t >> 3, sub = t & 7;
            float x[4], mx = -INFINITY;
#pragma unroll
            for (int j = 0; j < 4; ++j) {
                const int c = sub * 4 + j;
                x[j] = S.ok[st][c] ? (((S.part[0][r][c] + S.part[1][r][c]) + S.part[2][r][c]) + S.part[3][r][c]) * qdown
                                   : -INFINITY;
                mx = fmaxf(mx, x[j]);
            }
#pragma unroll
            for (int o = 1; o < 8; o <<= 1) mx = fmaxf(mx, __shfl_xor_sync(0xffffffffu, mx, o));
            const float m_old = S.mrow[r];
            const float m_new = fmaxf(m_old, mx);
            float sum = 0.0f;
#pragma unroll
            for (int j = 0; j < 4; ++j) {
                const float e = x[j] == -INFINITY ? 0.0f : exp2f(x[j] - m_new);
                S.p[r][sub * 4 + j] = e;
                sum += e;
            }
#pragma unroll
            for (int o = 1; o < 8; o <<= 1) sum += __shfl_xor_sync(0xffffffffu, sum, o);
            __syncwarp();
            if (sub == 0) {
                const float a = m_old == -INFINITY ? 0.0f : exp2f(m_old - m_new);
                S.alpha[r] = a;
                S.lsum[r] = fmaf(S.lsum[r], a, sum);
                S.mrow[r] = m_new;
            }
        }
        __syncthreads();
        // p.v over this warp's 64 dims: block bi's 32 dims are n-tiles 4bi..4bi+3, with its scales folded into p
        // relative to the chunk's largest magnitude (as v1's mode 4; q4_0's scales are signed)
        {
            float tmp[8][4];
#pragma unroll
            for (int j = 0; j < 8; ++j) tmp[j][0] = tmp[j][1] = tmp[j][2] = tmp[j][3] = 0.0f;
            float vdown_b[2];
#pragma unroll
            for (int bi = 0; bi < 2; ++bi) {
                float vmax = fabsf(q4_scale(V[lane], bi));
#pragma unroll
                for (int o = 16; o > 0; o >>= 1) vmax = fmaxf(vmax, __shfl_xor_sync(0xffffffffu, vmax, o));
                const float vup = vmax > 0.0f ? 16384.0f / vmax : 0.0f;
                vdown_b[bi] = vmax * (1.0f / 16384.0f);
#pragma unroll
                for (int ks = 0; ks < CH2 / 16; ++ks) {
                    const int cA = ks * 16 + 2 * tig, cB = cA + 8;
                    const float w0 = q4_scale(V[cA], bi) * vup, w1 = q4_scale(V[cA + 1], bi) * vup,
                                w2 = q4_scale(V[cB], bi) * vup, w3 = q4_scale(V[cB + 1], bi) * vup;
                    const float p00 = S.p[gid][cA] * w0, p01 = S.p[gid][cA + 1] * w1;
                    const float p10 = S.p[gid + 8][cA] * w0, p11 = S.p[gid + 8][cA + 1] * w1;
                    const float p02 = S.p[gid][cB] * w2, p03 = S.p[gid][cB + 1] * w3;
                    const float p12 = S.p[gid + 8][cB] * w2, p13 = S.p[gid + 8][cB + 1] * w3;
                    uint32_t ah[4], al[4];
                    ah[0] = pack_h2(p00, p01);
                    ah[1] = pack_h2(p10, p11);
                    ah[2] = pack_h2(p02, p03);
                    ah[3] = pack_h2(p12, p13);
                    {
                        const __half2* h = reinterpret_cast<const __half2*>(ah);
                        float2 f;
                        f = __half22float2(h[0]); al[0] = pack_h2(p00 - f.x, p01 - f.y);
                        f = __half22float2(h[1]); al[1] = pack_h2(p10 - f.x, p11 - f.y);
                        f = __half22float2(h[2]); al[2] = pack_h2(p02 - f.x, p03 - f.y);
                        f = __half22float2(h[3]); al[3] = pack_h2(p12 - f.x, p13 - f.y);
                    }
                    const uint8_t* vA0 = reinterpret_cast<const uint8_t*>(V[cA]) + bi * 18 + 2;
                    const uint8_t* vA1 = reinterpret_cast<const uint8_t*>(V[cA + 1]) + bi * 18 + 2;
                    const uint8_t* vB0 = reinterpret_cast<const uint8_t*>(V[cB]) + bi * 18 + 2;
                    const uint8_t* vB1 = reinterpret_cast<const uint8_t*>(V[cB + 1]) + bi * 18 + 2;
#pragma unroll
                    for (int jj = 0; jj < 4; ++jj) {   // dims e = 8 jj + gid of the block: byte e & 15, nibble e >> 4
                        const int e = jj * 8 + gid, by = e & 15, sh = (e >> 4) * 4;
                        uint32_t b[2];
                        b[0] = nib2_to_h2(((uint32_t) vA0[by] | ((uint32_t) vA1[by] << 16)) >> sh);
                        b[1] = nib2_to_h2(((uint32_t) vB0[by] | ((uint32_t) vB1[by] << 16)) >> sh);
                        mma16816(tmp[bi * 4 + jj], ah, b);
                        mma16816(tmp[bi * 4 + jj], al, b);
                    }
                }
            }
            const float a0 = S.alpha[gid], a1 = S.alpha[gid + 8];
#pragma unroll
            for (int j = 0; j < 8; ++j) {
                const float vdown = vdown_b[j >> 2];
                acc[j][0] = fmaf(acc[j][0], a0, tmp[j][0] * vdown);
                acc[j][1] = fmaf(acc[j][1], a0, tmp[j][1] * vdown);
                acc[j][2] = fmaf(acc[j][2], a1, tmp[j][2] * vdown);
                acc[j][3] = fmaf(acc[j][3], a1, tmp[j][3] * vdown);
            }
        }
        __syncwarp();
    }
    __syncthreads();   // (ROT: every warp is done with the stage buffers; the last gather group was waited for)
    if constexpr (SPLIT) {
        write_split(acc, S.mrow, S.lsum, so, qi, kvh, n_kv_heads, dim0, gid, tig, t);
        return;
    }
    const float l0 = S.lsum[gid], l1 = S.lsum[gid + 8];
    const float i0 = l0 > 0.0f ? 1.0f / l0 : 0.0f, i1 = l1 > 0.0f ? 1.0f / l1 : 0.0f;
    float* const out = ROT ? QR : attn;   // ROT: the rows in shared memory, rotated back below
#pragma unroll
    for (int j = 0; j < 8; ++j) {
        const int d = dim0 + j * 8 + 2 * tig;
        *reinterpret_cast<float2*>(out + (size_t) gid * HD + d) = make_float2(acc[j][0] * i0, acc[j][1] * i0);
        if (gid + 8 < G)
            *reinterpret_cast<float2*>(out + (size_t) (gid + 8) * HD + d) = make_float2(acc[j][2] * i1, acc[j][3] * i1);
    }
    if constexpr (ROT) {
        __syncthreads();
        for (int r = warp; r < G; r += THREADS / 32) fwht256_row_warp(QR + r * HD, attn + (size_t) r * HD, lane);
    }
}

template <bool ROT>
bool launch_q4(const float* q, const QsaAttnPools& pools, const int32_t* ids, const int32_t* steps, int64_t cap,
               const QsaShapes& s, float* attn, int64_t n_q, cudaStream_t st) {
    static bool attr[64] = {};   // per device and instance, as launch_i8
    int dev = 0;
    cudaGetDevice(&dev);
    const int bytes = (int) sizeof(Smem4);
    if (dev < 0 || dev >= 64) return false;
    if (!attr[dev]) {
        if (cudaFuncSetAttribute(prompt_attn_q4_kernel<ROT>, cudaFuncAttributeMaxDynamicSharedMemorySize, bytes) !=
            cudaSuccess) {
            cudaGetLastError();
            return false;
        }
        attr[dev] = true;
    }
    const float scale_log2 = 1.4426950408889634f / sqrtf((float) HD);
    for (int64_t q0 = 0; q0 < n_q; q0 += 65535) {
        const int64_t nb = n_q - q0 < 65535 ? n_q - q0 : 65535;
        prompt_attn_q4_kernel<ROT><<<dim3((unsigned) nb, (unsigned) s.n_head_kv), THREADS, bytes, st>>>(
            q + q0 * s.n_head * HD, pools, ids + q0 * cap, steps + q0 * kStepCount, (int) s.n_head_kv,
            (int) s.page_size, scale_log2, attn + q0 * s.n_head * HD, (int) cap);
    }
    const cudaError_t e = cudaGetLastError();
    if (e != cudaSuccess) {
        std::fprintf(stderr, "qsa_prompt_attn_batch: %s\n", cudaGetErrorString(e));
        std::exit(1);
    }
    return true;
}

bool launch_i8(const float* q, const QsaAttnPools& pools, const int32_t* ids, const int32_t* steps, int64_t cap,
               const QsaShapes& s, float* attn, int64_t n_q, cudaStream_t st) {
    static bool attr[64] = {};   // the shared-memory opt-in is per device (a layer split runs this on several)
    int dev = 0;
    cudaGetDevice(&dev);
    const int bytes = (int) sizeof(Smem2);
    if (dev < 0 || dev >= 64) return false;
    if (!attr[dev]) {
        if (cudaFuncSetAttribute(prompt_attn_i8_kernel<false>, cudaFuncAttributeMaxDynamicSharedMemorySize, bytes) !=
            cudaSuccess) {
            cudaGetLastError();
            return false;
        }
        attr[dev] = true;
    }
    const float scale_log2 = 1.4426950408889634f / sqrtf((float) HD);
    for (int64_t q0 = 0; q0 < n_q; q0 += 65535) {
        const int64_t nb = n_q - q0 < 65535 ? n_q - q0 : 65535;
        prompt_attn_i8_kernel<false><<<dim3((unsigned) nb, (unsigned) s.n_head_kv), THREADS, bytes, st>>>(
            q + q0 * s.n_head * HD, pools, ids + q0 * cap, steps + q0 * kStepCount, (int) s.n_head_kv,
            (int) s.page_size, scale_log2, attn + q0 * s.n_head * HD, (int) cap);
    }
    const cudaError_t e = cudaGetLastError();
    if (e != cudaSuccess) {
        std::fprintf(stderr, "qsa_prompt_attn_batch: %s\n", cudaGetErrorString(e));
        std::exit(1);
    }
    return true;
}

template <int KV_MODE>
bool launch(const float* q, const QsaAttnPools& pools, const int32_t* ids, const int32_t* steps, int64_t cap,
            const QsaShapes& s, float* attn, int64_t n_q, cudaStream_t st) {
    static bool attr[64] = {};   // per device, as above
    int dev = 0;
    cudaGetDevice(&dev);
    const int bytes = (int) sizeof(Smem<KV_MODE>);
    if (dev < 0 || dev >= 64) return false;
    if (!attr[dev]) {
        if (cudaFuncSetAttribute(prompt_attn_kernel<KV_MODE>, cudaFuncAttributeMaxDynamicSharedMemorySize, bytes) !=
            cudaSuccess) {
            cudaGetLastError();
            return false;
        }
        attr[dev] = true;
    }
    const float scale_log2 = 1.4426950408889634f / sqrtf((float) HD);
    for (int64_t q0 = 0; q0 < n_q; q0 += 65535) {
        const int64_t nb = n_q - q0 < 65535 ? n_q - q0 : 65535;
        prompt_attn_kernel<KV_MODE><<<dim3((unsigned) nb, (unsigned) s.n_head_kv), THREADS, bytes, st>>>(
            q + q0 * s.n_head * HD, pools, ids + q0 * cap, steps + q0 * kStepCount, (int) s.n_head_kv,
            (int) s.page_size, scale_log2, attn + q0 * s.n_head * HD, (int) cap);
    }
    const cudaError_t e = cudaGetLastError();
    if (e != cudaSuccess) {
        std::fprintf(stderr, "qsa_prompt_attn_batch: %s\n", cudaGetErrorString(e));
        std::exit(1);
    }
    return true;
}

#if defined(__HIPCC__)
// ---- S6: the int8-KV prompt attention on RDNA4 matrix cores (opt-in: STRATA_HIP_WMMA=1, gfx12 only). The design of
// the v2 kernel above with gfx12's v_wmma_f32_16x16x16_f16 (wave32) in place of m16n8k16: wave w owns dims
// [64w, 64w+64) (int8 scale group w) for q.k and p.v; q and p are split into FP16 hi + lo parts, the int8 codes enter
// exactly as FP16, the scales are applied in FP32 and the four groups' q.k partials are added in a fixed order.
// FP32-level accuracy, deterministic, but not bitwise equal to qsa_decode_attn_batch (another summation order).
// Fragment layout (16x16x16, wave32, checked on gfx1201): A lane l holds A[l % 16][(l / 16) * 8 + i], B lane l holds
// B[(l / 16) * 8 + i][l % 16], C/D lane l holds D[(l / 16) * 8 + i][l % 16], i = 0..7.
#if defined(__gfx1200__) || defined(__gfx1201__)
#define STRATA_PA_WMMA 1
#else
#define STRATA_PA_WMMA 0
#endif
typedef _Float16 wh8 __attribute__((ext_vector_type(8)));
typedef float wf8 __attribute__((ext_vector_type(8)));
constexpr int WCH = 32;          // cells per chunk (two 16-cell tiles)
constexpr int WVS = 80;          // staged V row stride in bytes (64 codes, 16-byte aligned, banks spread)
struct alignas(16) SmemW {
    float part[4][16][WCH + 1];  // q.k per dim group
    float p[16][WCH + 1];
    float vs[4][WCH];            // V scale per (group, cell)
    int valid[WCH];              // the cell has a resident pool row
    uint8_t v[4][WCH][WVS];      // per wave: the chunk's V codes of its group, one row per cell
    float qmax[4];
    float alpha[16];
    float lsum[16];
    float mrow[16];
};

__device__ __forceinline__ wf8 wmma_f16(wh8 a, wh8 b, wf8 c) {
#if STRATA_PA_WMMA
    return __builtin_amdgcn_wmma_f32_16x16x16_f16_w32_gfx12(a, b, c);
#else
    __builtin_trap();
    return c;
#endif
}

__device__ __forceinline__ wh8 i8x8_to_h8(uint2 x) {   // exact: |code| <= 128
    wh8 h;
#pragma unroll
    for (int i = 0; i < 4; ++i) {
        h[i] = (_Float16) (int) (int8_t) (x.x >> (8 * i));
        h[4 + i] = (_Float16) (int) (int8_t) (x.y >> (8 * i));
    }
    return h;
}

__global__ void __launch_bounds__(THREADS) prompt_attn_wmma_kernel(const float* __restrict__ q, QsaAttnPools p,
                                                                   const int32_t* __restrict__ ids,
                                                                   const int32_t* __restrict__ steps, int n_kv_heads,
                                                                   int page_size, float scale_log2,
                                                                   float* __restrict__ attn, int cap) {
#if STRATA_PA_WMMA
    __shared__ SmemW S;
    const int qi = blockIdx.x, kvh = blockIdx.y;
    const int n_head = n_kv_heads * G;
    q += (size_t) qi * n_head * HD + (size_t) kvh * G * HD;
    attn += (size_t) qi * n_head * HD + (size_t) kvh * G * HD;
    ids += (size_t) qi * cap;
    const int n = __ldg(steps + (size_t) qi * kStepCount + kStepWidth);
    const int t = threadIdx.x, lane = t & 31, warp = t >> 5;
    const int col = lane & 15, half = lane >> 4;   // fragment column / k half
    const int dim0 = warp * 64;

    float qm = 0.0f;
    for (int i = t; i < G * HD; i += THREADS) qm = fmaxf(qm, fabsf(q[i]));
#pragma unroll
    for (int o = 16; o > 0; o >>= 1) qm = fmaxf(qm, __shfl_xor_sync(0xffffffffu, qm, o));
    if (lane == 0) S.qmax[warp] = qm;
    if (t < 16) { S.mrow[t] = -INFINITY; S.lsum[t] = 0.0f; }
    __syncthreads();
    qm = fmaxf(fmaxf(S.qmax[0], S.qmax[1]), fmaxf(S.qmax[2], S.qmax[3]));
    int qe = 0;
    if (qm > 0.0f) frexpf(qm, &qe);
    const float qup = ldexpf(1.0f, 14 - qe), qdown = ldexpf(scale_log2, qe - 14);
    // q A fragments of this wave's 64 dims: row = col (heads 12..15 zero), k = dim0 + kk*16 + half*8 + i
    wh8 qh[4], ql[4];
#pragma unroll
    for (int kk = 0; kk < 4; ++kk) {
        float x[8];
        if (col < G) {
            const float4* src = reinterpret_cast<const float4*>(q + (size_t) col * HD + dim0 + kk * 16 + half * 8);
            const float4 a = src[0], b = src[1];
            x[0] = a.x; x[1] = a.y; x[2] = a.z; x[3] = a.w; x[4] = b.x; x[5] = b.y; x[6] = b.z; x[7] = b.w;
        } else {
#pragma unroll
            for (int i = 0; i < 8; ++i) x[i] = 0.0f;
        }
#pragma unroll
        for (int i = 0; i < 8; ++i) {
            const float v = x[i] * qup;
            const _Float16 hi = (_Float16) v;
            qh[kk][i] = hi;
            ql[kk][i] = (_Float16) (v - (float) hi);
        }
    }

    wf8 acc[4];
#pragma unroll
    for (int j = 0; j < 4; ++j) acc[j] = wf8{0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 0.f};

    const int n_chunks = (n + WCH - 1) / WCH;
    for (int ci = 0; ci < n_chunks; ++ci) {
        const int c0 = ci * WCH;
        // this lane's cell of the chunk (lane = cell index): its pool row, or -1 (past the selection, or a block the
        // KV streaming left non-resident: masked as in the decode kernel)
        long long myrow = -1;
        if (c0 + lane < n) {
            const int cell = __ldg(ids + c0 + lane);
            const long long page = (long long) __ldg(p.page_table + cell / page_size);
            if (page >= 0) myrow = (page * n_kv_heads + kvh) * page_size + (cell % page_size);
        }
        // stage this wave's V slice of the chunk (64 bytes of each row) and the V scale; the K fragments come straight
        // from global memory (8 consecutive codes per lane)
        {
            uint4 v0 = make_uint4(0, 0, 0, 0), v1 = v0, v2 = v0, v3 = v0;
            float vsc = 0.0f;
            if (myrow >= 0) {
                const uint4* src = reinterpret_cast<const uint4*>(p.v_q + myrow * HD + dim0);
                v0 = __ldg(src); v1 = __ldg(src + 1); v2 = __ldg(src + 2); v3 = __ldg(src + 3);
                vsc = __half2float(__ushort_as_half(__ldg(p.v_scale + myrow * (HD / KV_Q8_GROUP) + warp)));
            }
            uint4* dst = reinterpret_cast<uint4*>(&S.v[warp][lane][0]);
            dst[0] = v0; dst[1] = v1; dst[2] = v2; dst[3] = v3;
            S.vs[warp][lane] = vsc;
            if (warp == 0) S.valid[lane] = myrow >= 0;
        }
        // q.k over this wave's 64 dims for two 16-cell tiles, times the cell's K scale for this group
#pragma unroll
        for (int nt = 0; nt < 2; ++nt) {
            const long long rr = __shfl(myrow, nt * 16 + col);
            wf8 s = wf8{0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 0.f};
            float ksc = 0.0f;
            if (rr >= 0) ksc = __half2float(__ushort_as_half(__ldg(p.k_scale + rr * (HD / KV_Q8_GROUP) + warp)));
#pragma unroll
            for (int kk = 0; kk < 4; ++kk) {
                uint2 kx = make_uint2(0, 0);
                if (rr >= 0) kx = __ldg(reinterpret_cast<const uint2*>(p.k_q + rr * HD + dim0 + kk * 16 + half * 8));
                const wh8 b = i8x8_to_h8(kx);
                s = wmma_f16(qh[kk], b, s);
                s = wmma_f16(ql[kk], b, s);
            }
#pragma unroll
            for (int i = 0; i < 8; ++i) S.part[warp][half * 8 + i][nt * 16 + col] = s[i] * ksc;
        }
        __syncthreads();
        // online softmax over the four groups' sum (fixed order): row t/8, 4 cells per thread
        {
            const int r = t >> 3, sub = t & 7;
            float x[4], mx = -INFINITY;
#pragma unroll
            for (int j = 0; j < 4; ++j) {
                const int c = sub * 4 + j;
                // past the selection, or a masked cell (non-resident page, as in the decode kernel): no weight
                x[j] = S.valid[c] ? (((S.part[0][r][c] + S.part[1][r][c]) + S.part[2][r][c]) + S.part[3][r][c]) * qdown
                                  : -INFINITY;
                mx = fmaxf(mx, x[j]);
            }
#pragma unroll
            for (int o = 1; o < 8; o <<= 1) mx = fmaxf(mx, __shfl_xor_sync(0xffffffffu, mx, o));
            const float m_old = S.mrow[r];
            const float m_new = fmaxf(m_old, mx);
            float sum = 0.0f;
#pragma unroll
            for (int j = 0; j < 4; ++j) {
                const float e = x[j] == -INFINITY ? 0.0f : exp2f(x[j] - m_new);
                S.p[r][sub * 4 + j] = e;
                sum += e;
            }
#pragma unroll
            for (int o = 1; o < 8; o <<= 1) sum += __shfl_xor_sync(0xffffffffu, sum, o);
            if (sub == 0) {
                const float a = m_old == -INFINITY ? 0.0f : exp2f(m_old - m_new);
                S.alpha[r] = a;
                S.lsum[r] = fmaf(S.lsum[r], a, sum);
                S.mrow[r] = m_new;
            }
        }
        __syncthreads();
        // p.v over this wave's 64 dims: 4 tiles of 16 dims, 2 k-steps of 16 cells
        {
            float vmax = S.vs[warp][lane];
#pragma unroll
            for (int o = 16; o > 0; o >>= 1) vmax = fmaxf(vmax, __shfl_xor_sync(0xffffffffu, vmax, o));
            const float vup = vmax > 0.0f ? 16384.0f / vmax : 0.0f, vdown = vmax * (1.0f / 16384.0f);
            wf8 tmp[4];
#pragma unroll
            for (int j = 0; j < 4; ++j) tmp[j] = wf8{0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 0.f, 0.f};
#pragma unroll
            for (int ks = 0; ks < 2; ++ks) {
                const int cb = ks * 16 + half * 8;   // this lane's 8 cells
                wh8 ah, al;
#pragma unroll
                for (int i = 0; i < 8; ++i) {
                    const float pv = S.p[col][cb + i] * (S.vs[warp][cb + i] * vup);
                    const _Float16 hi = (_Float16) pv;
                    ah[i] = hi;
                    al[i] = (_Float16) (pv - (float) hi);
                }
#pragma unroll
                for (int j = 0; j < 4; ++j) {
                    const int d = j * 16 + col;
                    wh8 b;
#pragma unroll
                    for (int i = 0; i < 8; ++i) b[i] = (_Float16) (int) (int8_t) S.v[warp][cb + i][d];
                    tmp[j] = wmma_f16(ah, b, tmp[j]);
                    tmp[j] = wmma_f16(al, b, tmp[j]);
                }
            }
            float a[8];
#pragma unroll
            for (int i = 0; i < 8; ++i) a[i] = S.alpha[half * 8 + i];
#pragma unroll
            for (int j = 0; j < 4; ++j)
#pragma unroll
                for (int i = 0; i < 8; ++i) acc[j][i] = fmaf(acc[j][i], a[i], tmp[j][i] * vdown);
        }
        // no barrier here: `v`/`vs` are this wave's own, and `p`/`alpha`/`valid` are rewritten only after the next
        // chunk's first barrier (which every wave reaches after its p.v)
    }
    float inv[8];
#pragma unroll
    for (int i = 0; i < 8; ++i) {
        const float l = S.lsum[half * 8 + i];
        inv[i] = l > 0.0f ? 1.0f / l : 0.0f;
    }
#pragma unroll
    for (int j = 0; j < 4; ++j)
#pragma unroll
        for (int i = 0; i < 8; ++i) {
            const int row = half * 8 + i;
            if (row < G) attn[(size_t) row * HD + dim0 + j * 16 + col] = acc[j][i] * inv[i];
        }
#else
    __builtin_trap();
#endif
}

// gfx12 (RDNA4) only, and only on request: the output differs from the default kernel's in its last bits
bool hip_wmma_usable() {
    static const bool want = [] {
        const char* e = std::getenv("STRATA_HIP_WMMA");
        return e != nullptr && e[0] == '1';
    }();
    if (!want) return false;
    static int arch[64] = {};   // 0 unknown, 1 gfx12, 2 other
    int dev = 0;
    if (hipGetDevice(&dev) != hipSuccess || dev < 0 || dev >= 64) { (void) hipGetLastError(); return false; }
    if (arch[dev] == 0) {
        hipDeviceProp_t prop{};
        if (hipGetDeviceProperties(&prop, dev) != hipSuccess) { (void) hipGetLastError(); return false; }
        arch[dev] = std::strncmp(prop.gcnArchName, "gfx12", 5) == 0 ? 1 : 2;
        static bool told = false;
        if (!told) {
            told = true;
            std::fprintf(stderr, "strata: STRATA_HIP_WMMA: the prompt attention on matrix cores %s (%s)\n",
                         arch[dev] == 1 ? "on" : "unavailable", prop.gcnArchName);
        }
    }
    return arch[dev] == 1;
}

bool launch_wmma(const float* q, const QsaAttnPools& pools, const int32_t* ids, const int32_t* steps, int64_t cap,
                 const QsaShapes& s, float* attn, int64_t n_q, cudaStream_t st) {
    const float scale_log2 = 1.4426950408889634f / sqrtf((float) HD);
    for (int64_t q0 = 0; q0 < n_q; q0 += 65535) {
        const int64_t nb = n_q - q0 < 65535 ? n_q - q0 : 65535;
        prompt_attn_wmma_kernel<<<dim3((unsigned) nb, (unsigned) s.n_head_kv), THREADS, 0, st>>>(
            q + q0 * s.n_head * HD, pools, ids + q0 * cap, steps + q0 * kStepCount, (int) s.n_head_kv,
            (int) s.page_size, scale_log2, attn + q0 * s.n_head * HD, (int) cap);
    }
    const cudaError_t e = cudaGetLastError();
    if (e != cudaSuccess) {
        std::fprintf(stderr, "qsa_prompt_attn_batch (wmma): %s\n", cudaGetErrorString(e));
        std::exit(1);
    }
    return true;
}
#endif

// The CURRENT device's compute capability as the kernels see it (10 * major + minor; 0 when unknown), per device: a
// layer split can mix Turing with newer cards.  sm_75 or newer: the MMA above compiles for both.  sm_80+ runs the
// cp.async kernels (launch_i8, launch_q4); Turing has no cp.async, so it runs the v1 kernel (launch<1> / launch<4>,
// same accuracy, another summation order).  An older card keeps the old kernel.
// #371: the compute capability with its minor - sm_70 (V100) has no m16n8k8 (the kernels trap below sm_75)
int device_cc() {
    static int cc[64] = {};
    int dev = 0;
    if (cudaGetDevice(&dev) != cudaSuccess || dev < 0 || dev >= 64) { cudaGetLastError(); return 0; }
    if (cc[dev] == 0) {
        int major = 0, minor = 0;
        if (cudaDeviceGetAttribute(&major, cudaDevAttrComputeCapabilityMajor, dev) != cudaSuccess ||
            cudaDeviceGetAttribute(&minor, cudaDevAttrComputeCapabilityMinor, dev) != cudaSuccess) {
            cudaGetLastError();
            return 0;
        }
        // STRATA_QSA_WARP=1|attn (an A/B arm): the pre-sm_80 kernels on any card, as RTX 20 runs them
        const char* w = std::getenv("STRATA_QSA_WARP");
        cc[dev] = w && (!std::strcmp(w, "1") || !std::strcmp(w, "attn")) ? 75
                  : 10 * strata::cc_major_of(major) + strata::cc_minor_of(minor);
    }
    return cc[dev];
}

// Q4_0 KV's cp.async kernel runs (launch_q4): sm_80+, not switched off (STRATA_PROMPT_ATTN_Q4=0 the old kernel,
// STRATA_PROMPT_ATTN_V1=1 v1's mode 4), the real geometry.  Never on AMD (the tensor-core kernels are compiled out).
bool q4_v2_runs(const QsaShapes& s) {
#if defined(__HIPCC__)
    (void) s;
    return false;
#else
    static const bool q4_off = [] {
        const char* v = std::getenv("STRATA_PROMPT_ATTN_Q4");
        return v != nullptr && v[0] == '0';
    }();
    static const bool v1 = std::getenv("STRATA_PROMPT_ATTN_V1") != nullptr;
    return !q4_off && !v1 && s.head_dim == HD && s.n_head == (int64_t) G * s.n_head_kv && device_cc() >= 80;
#endif
}

#if !defined(__HIPCC__)
// The ranges of qsa_decode_attn_tc into the output: the same combination as qsa_decode_attn_batch's merge
__global__ void __launch_bounds__(HD) split_merge_kernel(const float* __restrict__ part, long long stride, int nsplit,
                                                         float* __restrict__ attn) {
    const int h = blockIdx.x, n_head = gridDim.x, qi = blockIdx.y, d = threadIdx.x;
    const int kvh = h / G, hl = h % G;
    const float* base = part + (size_t) qi * (size_t) stride;
    const float* pm = base + (size_t) nsplit * n_head * HD;
    const float* pl = pm + (size_t) nsplit * n_head;
    float M = -FLT_MAX;
    for (int z = 0; z < nsplit; ++z) M = fmaxf(M, pm[(kvh * nsplit + z) * G + hl]);
    float L = 0.0f, acc = 0.0f;
    for (int z = 0; z < nsplit; ++z) {
        const int slot = kvh * nsplit + z;
        const float m = pm[slot * G + hl];
        if (m == -FLT_MAX) continue;
        const float w = __expf(m - M);
        L = fmaf(pl[slot * G + hl], w, L);
        acc = fmaf(base[((size_t) slot * G + hl) * HD + d], w, acc);
    }
    attn[((size_t) qi * n_head + h) * HD + d] = L > 0.0f ? acc / L : 0.0f;
}

int device_sms() {
    static int sms[64] = {};
    int dev = 0;
    if (cudaGetDevice(&dev) != cudaSuccess || dev < 0 || dev >= 64) { cudaGetLastError(); return 0; }
    if (sms[dev] == 0 && cudaDeviceGetAttribute(&sms[dev], cudaDevAttrMultiProcessorCount, dev) != cudaSuccess) {
        cudaGetLastError();
        sms[dev] = 0;
    }
    return sms[dev];
}
#endif

}  // namespace

bool qsa_decode_attn_tc_usable(const QsaAttnPools& pools, const QsaShapes& s) {
#if defined(__HIPCC__)
    (void) pools; (void) s;
    return false;
#else
    static const bool off = [] {
        const char* v = std::getenv("STRATA_DECODE_ATTN_TC");
        return v != nullptr && v[0] == '0';
    }();
    if (off || s.head_dim != HD || s.n_head != (int64_t) G * s.n_head_kv || !pools.page_table || device_cc() < 80)
        return false;
    const bool i8 = pools.k_q && pools.v_q && pools.k_scale && pools.v_scale && !pools.k_q4 && !pools.v_q4;
    const bool q4 = pools.k_q4 && pools.v_q4 && !pools.k_q;
    return i8 || q4;
#endif
}

bool qsa_decode_attn_tc(const float* q, const QsaAttnPools& pools, const int32_t* ids, const int32_t* steps,
                        int64_t cap, const QsaShapes& s, float* scratch, float* attn, int64_t n_q, void* stream) {
#if defined(__HIPCC__)
    (void) q; (void) ids; (void) steps; (void) cap; (void) scratch; (void) attn; (void) n_q; (void) stream;
    return qsa_decode_attn_tc_usable(pools, s);
#else
    if (n_q <= 0) return true;
    if (!qsa_decode_attn_tc_usable(pools, s) || cap <= 0 || !ids || !steps || !scratch || n_q > 65535) return false;
    // enough blocks to fill the GPU (~4 per SM), within the selection's chunks and the decode scratch's ranges
    static_assert(sizeof(Smem2) <= 48 * 1024 && sizeof(Smem4) <= 48 * 1024,
                  "no shared-memory opt-in: the launch may be captured into a graph");
    const int max_split = (int) std::min<int64_t>((cap + CH2 - 1) / CH2, (cap + 63) / 64);
    const int64_t per_split = n_q * s.n_head_kv;
    const int target = 4 * std::max(device_sms(), 1);
    const int nsplit = (int) std::max<int64_t>(1, std::min<int64_t>(max_split, (target + per_split - 1) / per_split));
    SplitOut so;
    so.part = scratch;
    so.stride = (long long) qsa_decode_attn_scratch_floats(cap, s);
    so.nsplit = nsplit;
    const float scale_log2 = 1.4426950408889634f / sqrtf((float) HD);
    cudaStream_t st = (cudaStream_t) stream;
    const dim3 grid((unsigned) n_q, (unsigned) s.n_head_kv, (unsigned) nsplit);
    if (pools.k_q4 != nullptr)
        prompt_attn_q4_kernel<false, true><<<grid, THREADS, sizeof(Smem4), st>>>(
            q, pools, ids, steps, (int) s.n_head_kv, (int) s.page_size, scale_log2, attn, (int) cap, so);
    else
        prompt_attn_i8_kernel<true><<<grid, THREADS, sizeof(Smem2), st>>>(
            q, pools, ids, steps, (int) s.n_head_kv, (int) s.page_size, scale_log2, attn, (int) cap, so);
    split_merge_kernel<<<dim3((unsigned) s.n_head, (unsigned) n_q), HD, 0, st>>>(scratch, so.stride, nsplit, attn);
    const cudaError_t e = cudaGetLastError();
    if (e != cudaSuccess) {
        std::fprintf(stderr, "qsa_decode_attn_tc: %s\n", cudaGetErrorString(e));
        std::exit(1);
    }
    return true;
#endif
}

bool qsa_prompt_attn_rot_fused(const QsaShapes& s) {
    static const bool off = [] {
        const char* v = std::getenv("STRATA_PROMPT_ATTN_ROT_FUSED");
        return v != nullptr && v[0] == '0';
    }();
    return !off && q4_v2_runs(s);
}

bool qsa_prompt_attn_batch_rot(const float* q, const QsaAttnPools& pools, const int32_t* ids, const int32_t* steps,
                               int64_t cap, const QsaShapes& s, float* attn, int64_t n_q, void* stream) {
    if (n_q <= 0) return true;
    if (!qsa_prompt_attn_rot_fused(s) || pools.k_q4 == nullptr || pools.v_q4 == nullptr || cap <= 0 || !ids ||
        !steps || !pools.page_table)
        return false;
#if defined(__HIPCC__)
    (void) q; (void) attn; (void) stream;
    return false;
#else
    return launch_q4<true>(q, pools, ids, steps, cap, s, attn, n_q, (cudaStream_t) stream);
#endif
}

bool qsa_prompt_attn_batch(const float* q, const QsaAttnPools& pools, const int32_t* ids, const int32_t* steps,
                           int64_t cap, const QsaShapes& s, float* attn, int64_t n_q, void* stream) {
    if (n_q <= 0) return true;
    const int cc = device_cc();
    if (cc < 75) return false;
    const bool turing = cc < 80;
#if defined(__HIPCC__)
    // the tensor-core kernels are compiled out on AMD (its major version is not a CUDA sm); RDNA4 has its own int8-KV
    // matrix-core kernel, opt-in (STRATA_HIP_WMMA=1); everything else keeps the old kernel
    (void) turing;
    if (pools.k_q != nullptr && pools.v_q != nullptr && pools.k_scale != nullptr && pools.v_scale != nullptr &&
        pools.k_q4 == nullptr && pools.v_q4 == nullptr && s.head_dim == HD && s.n_head == (int64_t) G * s.n_head_kv &&
        cap > 0 && ids && steps && pools.page_table && hip_wmma_usable())
        return launch_wmma(q, pools, ids, steps, cap, s, attn, n_q, (cudaStream_t) stream);
    return false;
#endif
    if (s.head_dim != HD || s.n_head != (int64_t) G * s.n_head_kv || cap <= 0 || !ids || !steps || !pools.page_table)
        return false;
    cudaStream_t st = (cudaStream_t) stream;
    if (pools.k_q4 != nullptr) {   // Q4_0 K and V (--kv q4_0): mode 4.  STRATA_PROMPT_ATTN_Q4=0: the old kernel (A/B)
        static const bool q4_off = [] {
            const char* v = std::getenv("STRATA_PROMPT_ATTN_Q4");
            return v != nullptr && v[0] == '0';
        }();
        if (q4_off || pools.v_q4 == nullptr) return false;
        // sm_80+: the cp.async kernel (launch_q4); Turing, or STRATA_PROMPT_ATTN_V1=1 (an A/B arm): v1's mode 4
        if (q4_v2_runs(s)) return launch_q4<false>(q, pools, ids, steps, cap, s, attn, n_q, st);
        return launch<4>(q, pools, ids, steps, cap, s, attn, n_q, st);
    }
    if (pools.k_q != nullptr && pools.v_q4 != nullptr) {   // hybrid K8V4: int8 K + dequantized-q4 V
        if (!pools.k_scale) return false;
        return launch<3>(q, pools, ids, steps, cap, s, attn, n_q, st);
    }
    if (pools.k_q != nullptr) {
        if (!pools.v_q || !pools.k_scale || !pools.v_scale) return false;
        // STRATA_PROMPT_ATTN_V1=1 (debug): the first version, same accuracy, another summation order - the control
        // for how far the model amplifies an FP32-level change.  Turing always takes it: v2's cp.async does not
        // exist before sm_80.
        static const bool v1 = std::getenv("STRATA_PROMPT_ATTN_V1") != nullptr;
        if (v1 || turing) return launch<1>(q, pools, ids, steps, cap, s, attn, n_q, st);
        return launch_i8(q, pools, ids, steps, cap, s, attn, n_q, st);
    }
    if (!pools.k_pool || !pools.v_pool) return false;
    return launch<0>(q, pools, ids, steps, cap, s, attn, n_q, st);
}

}  // namespace strata::kernels
