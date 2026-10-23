# Batched DMA submission (experimental)

With `--pcie-mode dma`, `STRATA_DMA_BATCH=1` submits a verifier group's independent
expert uploads through `cudaMemcpyBatchAsync` on CUDA 13.0 or later. The default
is `0`, retaining the existing per-expert copies. The normal kernel-copy mode
is unchanged. This is an experiment to reduce host submission overhead, not a
demonstrated increase in model tokens per second.

`STRATA_DMA_BATCH=2` also sets CUDA 13.4's
`cudaMemcpyFlagPreferOverlapWithCompute`. It is a scheduling hint, not a
guarantee of overlap. On CUDA 13.0–13.3 this mode behaves like `1`. Older CUDA
builds and HIP retain individual copies. A single upload or a group larger than
128 also uses individual copies.

The sources must be immutable pinned host blobs, and destinations must be
disjoint. `expert_source.cpp` already restricts the DMA group to pinned experts.
The helper resolves device-visible aliases for registered host allocations;
Windows registered host pointers need not equal their GPU aliases. No extra
VRAM or weight conversion is introduced. All copies remain on the existing
stream, before the callback that signals flag B. A submission error fails the
engine rather than signalling readiness for stale weights.

## Tests

With the normal CUDA build and `STRATA_BUILD_TESTS=ON`:

```sh
cmake --build build --target dma_batch_parity
ctest --test-dir build -R dma_batch --output-on-failure
```

The test checks bytes and cross-stream event ordering for 0, 1, 4, 17, 64, 128,
and 129 uploads, at 37 bytes, 4 KiB and 2 MiB per upload, in all three modes.
Windows also runs the same cases with `VirtualAlloc` plus `cudaHostRegister`.
It prints interleaved diagnostic timings separately for submission and total
transfer time. These times overlap; they must not be added. On Windows it also
reports submitting-thread cycles using `QueryThreadCycleTime`. Cycle counts
are neither whole-process CPU utilization nor an energy measurement.

The 63-case matrix passed with both host allocation methods on CUDA 13.4 and
13.0, and with the individual-copy fallback on CUDA 12.6, all using the same
RTX 4070 Laptop and driver 616.56. This checks the older-toolkit fallback;
it is not a HIP runtime test.

## Initial measurement

RTX 4070 Laptop 8 GB, Ryzen 9 7940HS, 64 GB DDR5-5600, Windows, CUDA 13.4:
16 independent registered-host uploads of 2 MiB each, five measured repetitions
after warmup, alternating mode order:

| Path | Median host submission | Median total transfer |
| --- | ---: | ---: |
| Individual copies | 0.0936 ms | 2.5589 ms |
| Batch | 0.0162 ms | 2.5248 ms |
| Batch with overlap hint | 0.0155 ms | 2.5206 ms |

Batch submission latency fell 82.7%; total transfer latency fell only 1.3%.
Submission latency is measured wall time around the API calls, not whole-process
CPU utilization or energy consumption. The initial full-model screen on Swift
1.5 IQ3_XXS at 64K allocated context did not establish a generation-speed gain:
one coding request measured 30.3 tokens/s with individual DMA and 30.0 with
batching. Its generated lengths differed, so total task times cannot isolate
the transfer change. This evidence does not justify changing the default.

A separate CUDA 13.4 repetition with submitting-thread cycle measurements gave
449,824 cycles for individual copies and 67,939 for the batch, about 84.9% fewer.
Submission wall time was 0.1127 vs 0.0170 ms, while total transfer time was
2.5584 vs 2.5309 ms (about 1.1% lower). These are separate runs from the initial
table, not measurements to combine into a larger speedup.

## Model comparison on the isolated patch

An A-B-B-A comparison used only this patch on v0.1.39, with `--pcie-mode dma`.
Each fresh engine ran the same nine requests, including tool use, prompt reuse
and an independently checked interval-merging implementation. The coding request
used high reasoning, seed 4242, temperature 1, top-p 0.95, top-k 20 and a
3,072-token output limit. Context allocation was 65,536, INT8 KV with 20,480
resident cells, 1,040 cached experts, 7 workers plus the caller, PCIe fraction
0.35, and MTP 5 with English drafting and min-p 0.5.

| Run | Batch mode | Code decode tok/s | Output tokens including reasoning | Request wall time |
| --- | ---: | ---: | ---: | ---: |
| A1 | 0 | 29.7 | 928 | 32.98 s |
| B1 | 1 | 31.4 | 1,478 | 48.86 s |
| B2 | 1 | 30.3 | 1,314 | 45.12 s |
| A2 | 0 | 30.2 | 1,078 | 37.45 s |

All 36 requests passed. All four implementations passed 155 independent cases
and generator, invalid-input and non-mutation checks. Outputs differed even
between the two controls, and draft acceptance also differed. These runs do
not isolate a model speedup or show better time to a verified answer. The
byte-parity tests establish transfer correctness, not deterministic generated
text or general agent reliability.

A fifth run with batching enabled passed the same nine requests plus a
57,750-token prompt, auxiliary-call restoration, and three 1,024-square image
color checks including image-state restoration. An OpenAI-compatible HTTP
smoke request also passed. The long prompt read took 78.414 seconds; restoring
57,745 tokens after the side call reduced prompt processing to 0.301 seconds.
This validates the existing state cache with the patch, not a new cache speedup.
The five clean-patch runs passed 53 recorded requests plus the HTTP check.
Observed free physical VRAM stayed at least 347 MiB via NVML, and available
system RAM stayed above 4.30 GiB. The engine's separate CUDA free-memory warning
reported zero, so the NVML figure must not be read as a verified WDDM budget.
Linux and HIP runtime validation remains untested.

## Design reference

The batching idea follows NVIDIA NCCL's
[copy-engine implementation](https://github.com/NVIDIA/nccl/blob/12df1a11afad322be5a204a2db890161cbf8131d/src/ce_coll.cc)
and the [CUDA `cudaMemcpyBatchAsync` contract](https://docs.nvidia.com/cuda/cuda-runtime-api/group__CUDART__MEMORY.html).
The helper and tests here are new;
no NCCL source was transplanted. NCCL's multi-GPU performance claims are not
used as predictions for this single-GPU path.

## End-to-end A/B on the RTX 5070 (2026-10-22): the batch runs, and it buys nothing measurable

Two sweeps of `tools/strata_ab.py --sweep dma-batch`, six launches each (three rounds x two arms,
rounds alternating), instrumented engine `J:/Strata-work/build-0145/strata-instr.exe` rebuilt
2026-10-22 16:20, config `J:/Strata-work/strata-instr.json`, nine requests per launch, same model,
context, PCIe fraction and MTP as the tier-split batch. Run 1 `bench/results/ab-dma-batch`, the
baseline arm left the variable unset (so mode 0, the loop) and the other arm set 1. Run 2
`bench/results/ab-dma-batch-2`, both arms set explicitly: 0 for the loop, 1 for the batch.

The submission counters, read from the engine's own log `J:/Strata-work/strata.log` (the launch folders
hold only the startup excerpt), segmented by the per-launch PCIe-probe line, totals over the whole
launch, ~31 windows each:

| run | the arm that reaches the counter | submissions / blobs / batched | blobs per submission | % batched |
| --- | --- | --- | --- | --- |
| 1 | batch (= 1) | 1333 / 5626 / 1036 | 4.22 | 78 |
| 1 | batch (= 1) | 1484 / 6636 / 1160 (32 windows) | 4.47 | 78 |
| 1 | batch (= 1) | 1427 / 6524 / 1131 | 4.57 | 79 |
| 1 | unset (= 0, the loop) | 0 / 0 / 0, all three launches | - | - |
| 2 | batch (= 1) | 1341 / 5459 / 1018 | 4.07 | 76 |
| 2 | batch (= 1) | 1433 / 6484 / 1139 | 4.52 | 79 |
| 2 | batch (= 1) | 1296 / 5426 / 1000 (30 windows) | 4.19 | 77 |
| 2 | loop (= 0, set by hand) | 0 / 0 / 0, all three launches | - | - |

So the batched arm's half of the mechanism test is met, on all six of its launches across both runs:
about four blobs per submission and three quarters of submissions through `cudaMemcpyBatchAsync`. The
knob engages and the batch genuinely runs.

The other half — the rule as written, "both sets of counters non-zero" — is unsatisfiable as the
counters stand, and run 2 proves it: the loop arm was given the variable explicitly this time and read
exactly what the unset arm read in run 1, zero on every window of every launch. `verify.cpp:3050` runs
mode 0 as a plain loop of `cudaMemcpyAsync` with no counter on it; only the `else` branch reaches
`copy_expert_blobs`, which is where the counters live. Mode 0 cannot read non-zero. Its zeros mean "no
counter on this path", not "nothing ran" — it copies at normal speed.

Speed, `tools/read_sweep_warm.py`, warm medians against the batch as baseline:

| run | the loop arm's rows | largest gap among the arms | the batch's own noise (repeat rows) |
| --- | --- | ---: | ---: |
| 1 | +2.7%, +6.3%, +3.1% | 6.3% | 1.2% |
| 2 | +5.5%, -4.0%, -1.8% | 5.5% | 5.7% |

Neither run separates the arms: in both, the arm's per-turn distribution overlaps the repeat rows'
range, which is what the tool of record says out loud. Run 1's loop rows all lean one way and run 2's
do not, and run 2's own noise grew to 5.7%, bigger than the 5.5% it is being asked to beat.

**Verdict: no measured win, and no basis to run this a third time for the speed number.** The default
stays 0, which is what the code says it should be (0 until a measured win makes it 1). The counters did
their job on the arm they can see; to see the loop arm at all, `verify.cpp:3050` needs the same two
atomics the batch path has.

One asymmetry belongs to the code and cannot be arm-ed away: mode != 0 also turns adaptive overlap off
(`generate.cpp:9501`), so the arm is "batch AND overlap off" against "loop WITH overlap". If the batch
were free and overlap were worth a few percent, this is exactly what it would look like. The arm is the
product question; it is not a clean measurement of the batch alone.
