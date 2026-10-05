# Speed branch: Q4_0 KV, decode attention, adaptive drafts, tool-call streaming

Changes on top of this fork's `best`. Measured on an RTX 5070 12 GB, i5-10600K, 64 GB RAM, CUDA 13.4; the kernel
numbers come from the repo's parity tests (`qsa_prompt_attn_parity`, `kv_q4_parity`, `draft_policy_test`).

## What changed

- **`--kv q4_0` prompt attention** ([`qsa_prompt_attn.cu`](../src/kernels/cuda/qsa_prompt_attn.cu)). Q4_0 now runs a
  pipelined kernel like int8's (each warp gathers its two q4_0 blocks per row with `cp.async`, one chunk ahead):
  10.2 -> 5.6 ms per 2,048-query chunk at 64K context (int8: 6.5 ms). The queries' and the output's Hadamard rotations
  run inside the kernel (`qsa_prompt_attn_batch_rot`, bit-identical to rotate -> attend -> rotate, 3-7% faster).
  `STRATA_PROMPT_ATTN_V1=1` brings back the old kernel, `STRATA_PROMPT_ATTN_ROT_FUSED=0` the separate rotation passes.
- **Decode attention on tensor cores** (`qsa_decode_attn_tc`, int8 and q4_0 KV, sm_80+; `STRATA_DECODE_ATTN_TC=0` turns
  it off). A verify window's few queries are split over their selections to fill the GPU: a 5-token window's attention
  takes 0.033 ms instead of 0.098 ms per layer. FP32-level accuracy, not bitwise equal to `qsa_decode_attn_batch`.
  Safe inside CUDA graph capture (tested); non-resident KV blocks are masked like the old kernel's (tested).
- **Verify windows with Q4_0 KV** take the batched path (it was kept off Q4_0 from before the batched queries were
  rotated), and a window's Q4_0 cells are appended in one launch (`kv_append_q4_steps`, same bytes, tested). Not in a
  batch of slots (`batch_rec_`), where every row has its own K/V state.
- **`--spec-adaptive`** (opt-in). The MTP window's length by expected committed tokens per millisecond from the
  measured round costs and the acceptance by draft probability (`DraftPolicy::choose_mtp`), instead of
  `--spec-min-p`'s threshold. **Not a win on the PC above with an SSD-bound model:** simulated +3% to +11%
  (`draft_policy_test`, steady round costs), but Qwen3.8-Flash-Next Q4_K_M with 38 GiB of experts in RAM and the rest
  read from a SATA SSD gave a median 3.2 tok/s against 4.6 tok/s for `--spec-min-p 0.5` (round costs swing 300-700 ms
  with the SSD, the policy learns that and drafts too little). Leave it off unless every expert is in RAM or VRAM.
- **Tool-call streaming in the server** (`serve/frontend.py`): `call_end` resumes where the previous call stopped, so
  scanning a long call is linear. A 53K-token, 212 KB file write: 7.4 s -> 0.46 s of server time; the events are
  identical to the old parser's over 6,000 randomized streamed parses (`LinearToolCallScan` in
  `serve/test_server.py`).
- **`tools/mtp_from_gguf.py`**: the MTP draft block from a llama.cpp GGUF that carries it (finetunes and abliterations
  whose head ships only in the GGUF), in the form `tools/mtp_fetch.py` writes, so `mtp_pack.py` and `mtp_rt.py` build
  the runtime files from it.

## Not measured

No end-to-end tokens/s for the kernel changes: the model on this PC is SSD-bound (about 30 GB of reads per 256-token
reply), which hides GPU-side gains. Kernel timings are the evidence.
