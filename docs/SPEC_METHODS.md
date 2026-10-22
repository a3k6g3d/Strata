# Multi-token methods this engine has, measured on this machine (2026-10-21)

One sweep, `python tools/strata_ab.py --sweep spec-methods` (arms in `tools/strata_ab.py`), read with
`python tools/summarize_ab_sweep.py bench/results/ab-spec-methods`. The live configuration against one change per arm:
chat and agent tests, two runs per launch, i7-11700K, Swift 1.5 IQ3_S, `build-0145`, the Basalt MTP draft layer
(`--spec 4 --spec-min-p 0.7`, prompt-lookup suffix drafts at their default of 3).

The sweep was meant to take turns (A B C ... C B A) but its process tree was closed at launch 10 of 14 when the Claude
desktop app restarted at 16:53; the last four launches were run afterwards, one arm each, and the second launch of the
baseline came last, 92 minutes after the first. So the arms did not take turns the way the control wants, the machine's
position in the session is not balanced, and the noise floor below comes from two baseline launches only.

| arm (flag) | engine decode, tok/s, launch 1 / launch 2 | drafts accepted % | per-turn paired against the baseline, warm launch |
|---|---|---|---|
| as configured | 36.55 / 37.25 | 65.3 / 65.6 | - |
| MTP without prompt lookup (`--suffix-draft 0`) | 37.80 / 38.15 | 66.1 / 70.5 | +8.6% |
| original MTP head (`--mtp` the model's own) | 35.45 / 37.40 | 62.4 / 61.8 | -0.2%, inside noise |
| MTP + prompt-lookup chain (`--lookup-chain 4`) | 35.05 / 37.45 | 66.4 / 70.7 | -0.7%, inside noise |
| 4-bit draft layer (`--mtp-q4 all`) | 31.90 / 35.85 | 67.7 / 62.6 | -4.1% |
| wider window (`--spec 6`) | 34.15 / 33.90 | 57.7 / 58.3 | -11.7% |

The baseline's own two launches differ by 1.9%. What that supports:

- **`--spec 6` is worse.** Both of its launches (34.15 and 33.90) lie below both baseline launches (36.55 and 37.25), its
  draft acceptance falls to 58% from 65%, and the gap is 9-12% per turn. The window of 4 stays.
- **The 4-bit draft layer is no better and is noisier**: its launches span 31.9-35.9 and the warm one is 4.1% below the baseline.
- **The model's original MTP head and the prompt-lookup chain are indistinguishable from the configuration.** The original head
  accepts fewer drafts (62% against 65%); the chain accepts a few more (66-71%), and neither moves decode.
- **Prompt lookup off may be slightly faster**: both launches (37.80, 38.15) are above both baseline launches (36.55, 37.25), by
  3.4% and 2.4% on engine decode (the tool's per-turn figure of +8.6% is larger and rests on six turns). A 2-3% effect is about
  the size of this machine's warm engine-decode noise (2.1% on 2026-10-20), measured over two launches; it is a hint, not a
  finding, and would need the interleaved re-run before the setting is changed. The configuration is unchanged. (That re-run
  was made the same day, `lookup-off` below: the hint did not hold.)

## What could not be measured

**Speculation off.** The sweep's "no speculation" arm (`--spec 1`) did not start: a native (IQ) pack needs `--spec T` with
T >= 2 (`strata generate: ... is a native (IQ) pack: it needs --native SHARD1, --spec T (T >= 2) and --prefill CHUNK`), so no
flag turns the draft layer off on this model. The lowest setting the engine accepts is `--spec 2`, which is still MTP. How much
speculation buys over plain one-token decoding on this machine is therefore not measured here.

## The low end of the window, run (2026-10-21)

`python tools/strata_ab.py --sweep spec-low --rounds 3 --runs 6 --tests chat,agent` (the configuration's `--spec 4` against
`--spec 3` and `--spec 2`, nine launches taking turns), read with `python tools/summarize_ab_sweep.py bench/results/ab-spec-low`
and `python tools/read_sweep_warm.py bench/results/ab-spec-low`. `--spec 2` is the smallest window the engine accepts on this
pack - still MTP, one draft per check - so this bounds what the window contributes from below; it is not speculation off.

| arm | engine decode, tok/s, the three launches (cold ones first where flagged) | drafts accepted % | per-turn paired against `--spec 4` |
|---|---|---|---|
| `--spec 4` (as configured) | 37.80 / 40.60 / 36.60 | 66.9 / 72.0 / 68.3 | - |
| `--spec 3` | 41.10 / 40.40 / 39.55 | 75.3 / 74.4 / 75.6 | +6.8%, inside noise |
| `--spec 2` | 38.25 / 39.90 / 40.90 | 82.4 / 84.1 / 83.0 | +3.1%, inside noise |

- **A window of 2 or 3 is as fast as a window of 4 on this machine, within what the batch can resolve.** If anything, both are
  slightly ahead (medians 39.90 and 40.40 against 37.80), but the baseline's own three launches span 10.4% and its repeats'
  per-turn gaps reach 12.6%, so neither arm is separated (`read_sweep_warm.py`: the arms' per-turn gaps lie inside the repeats'
  range). Not a finding that a smaller window is better; a finding that the larger one is not buying anything measurable.
- **Draft acceptance rises as the window shrinks** (about 68% at 4, 75% at 3, 83% at 2), as it must: each further draft is less
  likely to be right. The extra drafts a window of 4 accepts are paid for in a longer verify window, and on this machine the two
  roughly cancel.
- **Together with the wider windows, decode is flat from 2 to 4 and falls above it** (`--spec 6` is 9-12% slower, in the
  `spec-methods` table above). `--spec 4` is therefore not an optimum so much as a point on a flat part of the curve; a
  re-run that could resolve 3-5% (more launches per arm, interleaved) would be needed before moving it, and the configuration
  is unchanged.

## Prompt lookup, re-checked interleaved (2026-10-22)

`python tools/strata_ab.py --sweep lookup-off --rounds 3 --runs 6 --tests chat,agent` (the configuration against
`--suffix-draft 0`, six launches taking turns), read with `python tools/summarize_ab_sweep.py bench/results/ab-lookup-off`
and `python tools/read_sweep_warm.py bench/results/ab-lookup-off`:

| arm | engine decode, tok/s, the three launches (cold ones flagged) | drafts accepted % |
|---|---|---|
| as configured | 33.55* / 38.20* / 39.10 | 66.1 / 65.7 / 76.3 |
| prompt lookup off | 41.55* / 38.60* / 39.10 | 74.3 / 60.4 / 70.1 |

The one launch of each arm that started after the machine's first twenty minutes measured the same, 39.10 tok/s, and the
per-turn paired difference over the batch is -3.3% (inside noise): the two arms' per-turn gaps (median 10.8%, range 1.6-17.2%)
lie inside the repeats' (median 4.2%, range 0.0-13.5%). **The 2-3% the first sweep hinted at was position and noise, not the
flag.** Prompt lookup stays on at its default of 3; turning it off buys nothing measurable here. The batch has one warm
launch per arm (the other two of each were flagged cold), so what it rules out is a gain of the size the hint claimed, not a
difference of a percent or two.

**Not in the engine, so not in the sweep:** EAGLE-style feature drafters, Medusa heads, a separate small draft model, and
diffusion or NCM drafters. The engine has MTP, prompt lookup (suffix drafts and the lookup chain) and the coupled/Gumbel
sampled drafting (`STRATA_SPEC_COUPLED`, `STRATA_SPEC_GUMBEL`), which acts only on requests sampled above temperature 0 and
so does not show in a greedy suite. NCM is a separate architecture trained per dataset (`J:\NCM-lab`, standalone), not a
drafter for this model.
