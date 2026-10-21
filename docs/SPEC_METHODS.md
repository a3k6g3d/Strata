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
  finding, and would need the interleaved re-run before the setting is changed. The configuration is unchanged.

## What could not be measured

**Speculation off.** The sweep's "no speculation" arm (`--spec 1`) did not start: a native (IQ) pack needs `--spec T` with
T >= 2 (`strata generate: ... is a native (IQ) pack: it needs --native SHARD1, --spec T (T >= 2) and --prefill CHUNK`), so no
flag turns the draft layer off on this model. The lowest setting the engine accepts is `--spec 2`, which is still MTP. How much
speculation buys over plain one-token decoding on this machine is therefore not measured here.

**Not in the engine, so not in the sweep:** EAGLE-style feature drafters, Medusa heads, a separate small draft model, and
diffusion or NCM drafters. The engine has MTP, prompt lookup (suffix drafts and the lookup chain) and the coupled/Gumbel
sampled drafting (`STRATA_SPEC_COUPLED`, `STRATA_SPEC_GUMBEL`), which acts only on requests sampled above temperature 0 and
so does not show in a greedy suite. NCM is a separate architecture trained per dataset (`J:\NCM-lab`, standalone), not a
drafter for this model.
