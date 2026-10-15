# Proposals the tree already answers (2026-10-14)

An audit of this machine's configuration proposed five changes. Each was checked against the code **before** spending a
benchmark hour on it, and all five turned out to be either already handled, or wrong about what the engine does. They are
written down here so the same five are not re-litigated in a month, with the line numbers that settle each one.

Nothing in this file is a measured result. It is a record of assumptions that did not survive contact with the source.
The measured work is in `CACHE_VS_DRAFTS.md` and `bench/results/`.

## 1. "`--spec 4` is on, so drafts are being offered"

**Proposed:** treat `--spec 4` as the speculation setting under test, independent of the rest.

**What the tree does.** `--spec N` is the *MTP drafter's* verify window - how many tokens the draft layer proposes per
check (`generate.cpp:709` region, help text). It needs a draft layer to propose anything. When the MTP runtime is absent
the window is not merely unused, it is set to a sentinel: `c.mtp_window = o.mtp.empty() ? -1 : o.mtp_window`
(`generate.cpp:7786`).

**Why the assumption is falsified.** The live `strata.json` does pass `--mtp <work>/mtp-rt-basalt`, so drafts *are*
live here - but the point is that `--spec` alone carries no information about whether speculation is on. Two earlier arms
in `tools/strata_ab.py:48-50` have to turn drafts off by *three* flags together (`--mtp-max-t 1 --suffix-draft 0`) while
still loading the MTP runtime, precisely so the runtime's memory cost stays in the comparison. An arm that dropped
`--mtp` instead would have silently changed the model's VRAM footprint as well as its drafts, and the decode difference
could not have been attributed to either.

**Rule this leaves behind:** `--mtp` is an invariant, not a variable. To vary drafts, vary `--spec` / `--mtp-max-t` /
`--suffix-draft` and keep `--mtp` pointed at the runtime in every arm.

## 2. "Set `--pcie-frac` to a tuned value"

**Proposed:** pin the PCIe share, as if it were a free parameter to be swept.

**What the tree does.** Passing the flag *disables the measurement that would have chosen it*:
`const bool pcie_given = o.pcie_frac >= 0.0;` (`generate.cpp:2257`), whose own comment reads "the stages' own link probes
are skipped". Left unset, the engine probes the link (`probe_pcie_h2d_gbps`, `generate.cpp:1593`, called at `:2752`),
converts GB/s into a share (`pcie_frac_for_gbps`, `:1656`) and - on single-GPU serve - sets `pcie_frac_auto` (`:2760`) so
the share is *refined from what the CPU pool measures during the first decode windows* (PcieAuto, noted at `:1660`).

**Why the assumption is falsified.** `--pcie-frac` is not a knob sitting beside the probe; it is the switch that turns the
probe off. Pinning it on a machine whose bus speed is already measured throws away an adaptive value that knows this
card (and this cable, and what else is on the bus) better than a constant does. The default is also model-dependent -
0.2 direct for the Q2_0 pack, 0.55 DMA for native packs (`generate.cpp:575`) - so a single borrowed number would be wrong
for reasons unrelated to this machine.

**Rule:** leave `--pcie-frac` unset unless the experiment is *about* the bus share. The engine prints what it chose
(`"PCIe probe: %.1f GB/s host->device ... -> pcie_frac %.2f"`, `:2765`), so the value is still readable after the run.

## 3. "Set `--pool-workers` to the core count"

**Proposed:** name the CPU expert pool's worker count explicitly.

**What the tree does.** `--pool-workers N` defaults to 0, and 0 means "derive it": every physical core except the one the
host loop spins on (`--help`, and `setup.py:549`: "as the engine's pool counts them (detect_cpu_topology: physical cores,
by Windows' EfficiencyClass or Linux's ...)"). The derivation is `detect_cpu_topology()` (`src/kernels/cpu/pool.cpp:44`,
implementation `:59`, declared in `include/strata/kernels/cpu/pool.hpp:93`), and the same topology decides *placement*
through `pool_affinity_win.hpp` (`src/kernels/cpu/pool.cpp:20`), which `CpuPool` consumes at `:438`.

**Why the assumption is falsified.** A hard-coded worker count is a second, dumber copy of a decision the engine already
makes from real hardware data - and it is only half the decision, because it leaves affinity to disagree with it. On a
CPU with E-cores (this one is an i5-10600K, 6P/12T, so harmless; a hybrid chip is not) the count and the affinity mask
must come from the same enumeration or workers land on the slow cores.

**Rule:** leave `--pool-workers` unset. `--pool-affinity` is the flag to reach for if placement is the subject. The engine
reports the effective count in its INFO line (`pool_workers=%d`, `generate.cpp:8975` region), so an arm that changes it
still shows what it became.

## 4. "`--kv q4_0` to halve the KV"

**Proposed:** move from the configured `--kv int8` to `q4_0` to free VRAM for expert slots.

**What the tree does.** `--kv q4_0` is offered and honest about its cost: "4-bit K/V after a Hadamard rotation (PR #21):
half of int8's memory, slightly lower precision (see bench/results/2026-09-27-kv-q4)" - that results folder exists. There
is also a middle option the proposal did not know about: `--kv k8v4`, "INT8 K (exact attention scores) + rotated Q4_0 V,
816 B/cell (vs int8's 1,056); streams with `--kv-resident` too".

**Why the assumption is falsified.** It is not that `q4_0` is unavailable; it is that it changes the *arithmetic of
attention*, so an arm that used it could no longer be compared token-for-token with the baseline. `int8` keeps attention
scores exact. If the goal is VRAM, `k8v4` keeps K exact and only quantises V, and it is strictly closer to the baseline
than `q4_0` while still freeing a quarter of the KV.

**Rule:** `--kv int8` stays pinned for anything whose output must match the baseline. KV quantisation is its own
experiment, run with its own parity gate, not as a side effect of a memory sweep.

## 5. "`-ffast-math` for speed"

**Proposed:** enable fast-math to buy arithmetic throughput.

**What the tree does - and this one is backwards.** There is no `-ffast-math` anywhere in the build (searched every
`.py .txt .cmake .hpp .cpp .cu .json .md`: no `-ffast-math`, no `-Ofast`, no `/fp:fast`). What the tree does have is
`--use_fast_math`, applied **per CUDA translation unit, deliberately**, with the reason written down each time:

- `include/strata/kernels/mrope.hpp:33-34` - "`--use_fast_math` is 0.0014 rad off at 32K and ~0.02 at 262K, and the
  prompt path (precise libm) and decode (fast-math) disagree with each other."
- `src/kernels/cuda/native_ple_postops.cu:122` - under fast-math "a division by the constant N may become a
  multiplication by its reciprocal", which moves the last bits.
- `src/kernels/cuda/native_rope.cu:151` - the opt-in RoPE table's angles "differ from the fast-math ones in the last
  bits, so outputs move".
- `src/kernels/cuda/shared_expert.cu:67` and `:136` - the answer the tree actually chose: "explicit intrinsics reproduce
  its `--use_fast_math` operations without changing legacy kernels", i.e. fast-math *results* are reproduced
  instruction-by-instruction inside kernels that are not compiled with the flag.

**Why the assumption is falsified.** The engine's correctness machinery is bitwise. `STRATA_STATE_HASH=1` prints
`STATE_HASH L=... gdn=... ple=... tail=... pooled=... kv=... stale=... ple_prev=...`, and the A/B/A gates
(`tools/conversation_cache_parity.py:19`, `STATE_KEYS = ('L','gdn','ple','tail','dead','pooled','pooled_full','kv',
'ple_prev')`) require those hashes and the generated token ids to be *equal* between baseline and candidate
(`:112-116`). Any flag that reassociates floating-point arithmetic makes every hash differ on the first request, so the
gates stop being a signal and become a wall. The tree has already paid the price of that trade in the places where the
speed was worth it, TU by TU, with an oracle pinned beside each one (`native_mmvq.hpp:12`: "the translation unit must use
`--use_fast_math`, as the pinned CUDA oracle does").

**Rule:** fast-math is not a build-wide switch here. New use is per-kernel, needs its pinned oracle, and is not worth
proposing unless the kernel is measured as the bottleneck.

## What survived the audit

Three of the five items were not wrong in spirit, only in placement: the bus share, the pool width and the KV size are
all real levers - they are just already automatic, or already measured. Those are the ones worth sweeping, and the sweep
is deliberately built so that the automatic ones stay automatic:

- pinned in every arm: `--mtp`, `--spec 4`, `--spec-min-p 0.5`, `--resident-budget-gib 36`, `--expert-profile`,
  `--kv int8`, `--kv-grow`, `--max-context 65536`, temperature 0;
- left unset in every arm: `--pcie-frac`, `--pool-workers`, `--expert-cache` (so the probe, the topology and the fit
  calculation each answer for this machine);
- varied one at a time: `--adapt-swaps`, `--adapt-decay`, `--kv-resident`, `--vram-reserve-mib`.

One thing the audit did not expect, found while building that list, and it changes what the sweep can conclude:
`docs/SWEEP_ADAPT_KV.md`.
