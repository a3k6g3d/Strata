# The new engine: primarily Rust (the plan)

Written 2026-10-23. This decides nothing about the split — [RUST_HOST.md](RUST_HOST.md) decided it on
2026-10-14: **Rust owns the host layer; the existing CUDA kernels stay C++ behind a C ABI facade.** What this
document adds is the order of work, and the branch it happens on: **a new branch off 0.1.45, with the Rust work
merged forward onto the newest engine.**

## What is promised, and what is not

Not speed. The floor is measured (BENCH_NOISE.md): **10.1% at the median, 27.0% at the p90, 51.9% worst**, over 78
warm pairs. The batched-DMA experiment bought 1.1% — inside that floor, and reported as such. Any milestone that
reports a speed win must clear the floor with the standard protocol (interleaved arms, >=8 runs, `decode_warm`,
paired turn-by-turn) or it is reported as noise.

What it is for is the safety of the async tier, which is where this engine's bugs have always lived:

- **Ownership over the buffers that outlive their calls.** The transient-blob lifetime rule
  (`include/strata/core/expert_source.hpp:200`, "stays valid for the layer it was asked in and the next one or two")
  is a comment today. It becomes a borrow.
- **Rings as values.** The IOCP completion ring, the pinned tier, the depth-1..4 prefetch window: each gets a type,
  a capacity and a drop impl, instead of a struct and a convention.

The physics is unchanged by any of this: a 2.5 GB/s NVMe (measured, not labelled), a Gen4 x16 link with ten times
the bandwidth the drive can supply, and a routing spread where only ~9.1% of experts cross the bus.

## Step 0 — the merge forward (the only step that is not Rust)

`rust-host/`, `include/strata/c/abi.h`, `src/facade/abi.cpp`, `tools/output_fingerprint.py`,
`tools/test_output_fingerprint.py` and `docs/RUST_HOST.md` are tracked on `speed-work-0.1.43`. `speed-work-0.1.45`
carries none of them — `git ls-tree` there returns nothing for those paths — so the merge is a real move, not a
formality, and this document travels with it.

Gate: `cargo test -p strata-abi -p strata-host` green on the merged tree, both arms (CPU and CUDA), and the skip
path run too — a machine with no built facade is told so, not passed in silence.

## The seam — built, paid for, not to be redesigned mid-flight

`STRATA_ABI_VERSION 1`, a hand-written `extern "C"` header, no bindgen, no cxx. Five exports today:
`strata_abi_version`, `strata_abi_last_error`, `strata_facade_probe_policy`, `strata_facade_cudart_version`,
`strata_facade_expert_apply`. Adding a field to a struct below means bumping the version and the Rust mirror in the
same commit. The build-time guard that keeps the batch cliff out
(`src/core/expert_source.cpp:3604`, `static_assert(MAXT * 10 <= 128)`) has a Rust-side twin in the ABI tests; both
must stay true.

Facts already paid for, kept where they are used: a completion port's key cannot tell one read from another (the
`OVERLAPPED` pointer keys the pending table); `push` hands back what it refuses; answers are compared as bits, not
floats; the facade's last-error buffer is per-thread; the build script finds the DLL and never builds it, and the
test loads it by absolute path, because cargo prunes anything it did not put in `deps/` itself.

## The order of work

**M1–M3, done.** Skeleton; the toolchain question (can MSVC, cudart and cargo coexist in one build — yes); the
vertical slice: Rust's own `NO_BUFFERING | OVERLAPPED` read path on its own IOCP, the first seam call carrying a
struct, running the existing `s2_expert_vnni` on bytes the caller read. 13 tests, two checks deliberately different
in kind — self-consistency, and structure (a read that always returns the wrong window is perfectly
self-consistent).

**M4, harness built.** `tools/output_fingerprint.py` (13 selftests, 10 pytest tests). It already answered the
question it was built for: the existing engine is not byte-identical to itself — 2, 5 and 4 distinct answers across
three prompts over five runs. So the gate as written compares only prompts each arm reproduces alone, splits cold
from warm, and never lets an errored run vote.

**M5, the ring in production.** The first real read path — one tier, one file, the pack the server actually uses —
behind the existing engine, opt-in, the default path byte-identical to the last release. Gate: the same answers as
today, from the fingerprint tool, on the prompts its rule allows.

*Done 2026-10-23.* The tier is the n-gram table - `platform::DirectFile`'s one production user
(`src/ngram/ple_reader.cpp`). `STRATA_HOST_DLL=<absolute path to strata_host.dll>` routes it through the Rust ring;
unset, the reader runs the code it ran before. Built as a DLL the engine loads (a staticlib needs `onecoreuap.lib`
in the engine's link; the commit message has the measurement). `abi.h` carries both directions, and both function
lists are now parsed by a drift test. `tests/platform/direct_file_win.cpp` passes on both readers.

The gate, run one server at a time (two do not fit in RAM), 5 runs per prompt, seed 1234, greedy, on an i7-11700K
and RTX 5070 with Swift 1.5 IQ3_S: arm A the branch's engine, default path; B the same exe through the Rust host
(its log says so); C the live 0.1.45 build; A2 arm A relaunched, the control. Thirteen prompts - the sweeps' three
plus ten exact-answer ones, added because the first run of this gate had only one prompt the engine reproduced:

| pair | match | mismatch | not comparable |
|---|---|---|---|
| A vs A2 (control) | 10 | 0 | 3 |
| **A vs B (the gate)** | **10** | **0** | 3 |
| A2 vs B | 10 | 0 | 3 |
| A vs C (default path vs the last build) | 10 | 0 | 3 |

The three not comparable are chat1, chat2 and one exact prompt ("strawberry" backwards, which screened stable on the
live server and then changed answer at run 3): no arm reproduced them, so the rule sets them aside. No speed is
claimed. One load-time probe each put the n-gram reads at 96,952 rows/s on the C++ pool and 83,336 through Rust,
which is a single number, not a measurement - and the expected direction, since the Rust ring issues every read
on the calling thread where the pool issues from four.

**M6, the lifetime rule as a borrow.** The blob the Rust side hands down is provably alive for the layer it was
asked in and the next one or two. Gate: M5's, plus a test that a consumer keeping a blob too long fails loudly
rather than silently.

*The type, 2026-10-23:* `rust-host/crates/strata-host/src/transient.rs` - a reference into a slot cannot live
across a layer advance (a `compile_fail` doctest pins E0502), and a lease kept past its window is refused with
`BadSlot`, never answered with the next blob's bytes (the gate's test). Not wired in.

*The audit, before wiring it in, 2026-10-23.* The question was whether today's C++ breaks the rule at all - which
decides whether M6 is a fix or a rail. `STRATA_STAGE_AUDIT=1` (opt-in; off, one counter per fill that nothing reads)
checks the stage buffers of the native GGUF source from both sides: the source counts a recycle of a buffer it handed
out fewer than three layers ago; the CPU pool stamps every blob before it reads it and checks the stamp after. One
bench suite (chat + agent, two runs, 5.3 min, Swift 1.5 IQ3_S, 0.1.45 + this branch, 36.8 tok/s chat decode):

| | |
|---|---|
| layer steps | 32,768 |
| pool reads checked | 4,980 |
| **pool reads whose buffer was refilled under them** | **0** |
| recycles | 12,355 |
| recycles 0 / 1 / 2 layers after the last hand-out | 5,418 / 2 / 0 |

So the source *does* break the comment's promise - but where nobody is holding a pointer. The recycle rule is "three
layers *or* 256 assemblies, whichever comes first", and the layer clock only ticks in decode (`begin_layer`); at
startup (the GPU cache fill: 2,066 of them before the first decode) and while a long prompt is read (the rest arrive
with the 647- and 3,972-token prompts), the clock stands still and the 256-assembly rule recycles at "age 0". Those
blobs are copied the moment they are handed out (cache fills), and the prompt path never holds a transient pointer -
it has the source copy each blob (`prefill.cpp`, CS-T). In decode, two recycles one layer after hand-out in 32,768
steps, and no pool read disturbed.

Reading: **no consumer breaks the rule today**; the comment describes it less exactly than the code does ("layers"
is only a clock in decode). Wiring the Rust ring in would therefore be a rail against a future consumer, not the fix
of a present bug - which is worth knowing before paying for it on the hot path.

Found on the way, not fixed (a default-path change, so its own decision): the reset that hands the startup's stage
buffers back when the mapped tier takes over (`expert_source.cpp`, after `close_direct`) clears `stage_buf_` but not
`stage_idx_` or the per-buffer events, where the other reset clears all three (`stage_free_events`). A later
`note_async_read` with a pointer that matches a stale entry indexes `stage_buf_[v]` before it checks anything.

**M7, the planner and the window.** The depth-1..4 prefetch window becomes a type with a capacity and a drop impl.
The invariant "a prediction only ever warms" gets a test: a prediction must never change *what* is computed, only
*when* the bytes arrive.

**M8, the GEN endpoint.** The Rust host answers the line protocol the Python server already speaks — `GEN <max_new>
<ids>` in, `T <id>` out (`serve/server.py:681`), same port. "`serve/` unchanged" is the property that makes the
whole thing safe to run while it is built, so it is kept to the end.

## What each step must not do

- No kernel rewritten in Rust. No Rust CUDA bindings crate. Rust never compiles a kernel, it calls one.
- No new default. Every step opt-in; the default path stays byte-identical to the last release.
- No speed claim that has not cleared the floor.

## What is not in this plan

The desktop shell (`desktop/tauri`, Rust, two source files) is a window around the webui and stays separate; it
opens `http://127.0.0.1:8080` and is in daily use. The 63 `.cu` files stay C++. If the kernels are ever to move,
that is a separate decision with its own bindings argument — not this plan, and not a milestone of it.
