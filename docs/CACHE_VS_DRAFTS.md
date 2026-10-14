# What the speed on this machine actually comes from (2026-10-14)

Two features make a MoE model fast on a small card: the expert cache, which keeps the experts a token needs in VRAM,
and the drafts (MTP and the suffix drafter), which let several tokens be verified in one pass.  They are usually turned
on together, so nobody knows which one is doing the work.  This run takes them away one at a time.

## The arms, and what each one cost

RTX 5070 12 GiB, Qwen3.8-Flash-Next IQ3_S (35.4 GiB of experts in system RAM), INT8 KV, 65,536-token context,
`--expert-cache auto` (1,951 slots, 3,840 MiB), `--spec 4 --spec-min-p 0.5` with an MTP runtime offering 4 drafts.
Same model, same prompts, same number of runs; one engine argument changed per arm.  Temperature 0, 3 runs each,
medians.  Full reports: `bench/results/ab-cache-vs-drafts/`.

| arm | chat decode tok/s | fresh 4K prompt tok/s | first token | cache hit | over PCIe | drafts accepted |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| as configured | 28.60 | 479.2 | 1.68 s | 0.0% | 6.9% | 68.8% |
| `--expert-cache 0` | 28.10 (-1.7%) | 476.7 (-0.5%) | 1.66 s | 0.0% | 7.6% | 68.1% |
| `--mtp-max-t 1 --suffix-draft 0` | 23.20 (-18.9%) | 457.6 (-4.5%) | 1.71 s | 0.0% | 4.2% | - |
| both off | 21.20 (-25.9%) | 448.9 (-6.3%) | 2.07 s | 0.0% | 8.5% | - |

The run-to-run spread inside one arm was 26.8-32.1 tok/s, so -1.7% is nothing; -18.9% is not noise.  Taking the drafts
away costs almost a fifth of the decode and, on the long prompt, 4.5% of the read.  Taking the cache away changes
nothing that this suite can see.

## Why the cache shows nothing here, and what this does NOT prove

The engine reports a **0.0% expert hit rate in every arm**, including the one where the cache is on.  That is not the
readout being broken - the PCIe share is non-zero and moves between arms, so the cache is being consulted.  It is the
tests: every prompt in the suite is fresh by construction (each carries a unique tag, and the 4K prompts report
`0 reused`), so there is no repeated prefix whose routed experts could already be resident.  A cache that only ever
helps when the same experts come back has nothing to hit, and the arm that removes it loses nothing.

So this measures the value of the expert cache on work that never repeats itself, which on this machine is nil, and it
measures the value of the drafts on that same work, which is 19%.  It says **nothing** about the workload the cache is
built for - the agent session that resends the same conversation turn after turn - because that test was not run here
(`--tests chat,4k`).  The suite has it; run `--tests agent` (or the default suite) before drawing any conclusion about
the cache.

Two secondary readings worth keeping: the drafts-off arm put **fewer** experts over PCIe (4.2% against 6.9%), because
a rejected draft wastes a pass over the experts it routed through; and both-off is worse than drafts-off alone
(-25.9% against -18.9%), so with no drafts the cache still earns something even on fresh prompts.

## The next arm

The honest follow-up is the same four arms on `--tests agent`, where the conversation is resent and the hit rate should
be well above zero.  If the cache still shows nothing there, the claim that the expert cache carries work on this class
of model needs revisiting; if it shows a large gain, then this suite simply never asked the question.

Run it with `Run-AB.cmd` (which stops the server the chat is served through, runs the arms on an empty GPU and starts
it again), or directly:

```
py -3.11 tools/strata_ab.py --config <strata.json> --tests agent --dry-run   # see the arms first
py -3.11 tools/strata_ab.py --config <strata.json> --tests agent             # needs a free GPU
```
