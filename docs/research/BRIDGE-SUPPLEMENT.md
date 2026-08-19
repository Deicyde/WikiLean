# Bridge study — supplement (v3.1)

**Companion to `docs/research/BRIDGE-REPORT.md` (version 3.1, 2026-08-18).**
Everything the main paper's structure moved out lives here: the complete
preregistration execution inventory, raw-instrument tables and outage
bases, instrument-validation detail, exposure strata, sensitivity
analyses, the full tool-call census, the WF manual, review responses,
the changelog, and data availability. Section numbers §Sn are cited from
the main paper. Nothing here is new evidence; every table is
recomputable from the file named beside it.

## S1. Complete preregistration execution inventory

The table below lists every preregistered component, its status, and
the inferential consequence. Its sources are the preregistration
(`docs/research/BRIDGE-EXPERIMENT.md`, commit `0d36f266`) and the
claim-by-claim inventory in
`docs/research/review/verification-of-review-1.json` (prereg section).

| Preregistered component | Status | What actually happened | Inferential consequence |
|---|---|---|---|
| Five arms A–E, identical model/prompt/budget, tools-only difference | executed | 471 rows/arm (`bench/data/runs/`) | design intact |
| Per-tool call logging | executed | `tool_calls_by_name` every row; full traces eval C/D/E + fresh (deviation 7) | enables trace attribution |
| Tier 1a ProofNet# (371) | executed | 371/arm, scored | contaminated by construction (arm A 59.8% on eval-341); paired deltas only |
| Tier 1b fresh set (~100) | executed | 100 tasks | holdout narrower than claimed (Brain indexes only; one later-fold leak) — renamed post-Brain-index, 99/100 |
| Hallucinated-decl rate | executed | all tables | instrument required repair (§S3); repaired rates are the report's |
| McNemar D-vs-E / D-vs-C + discordant tables | executed | main §4 | run on grounded typecheck, not the preregistered endpoint |
| 10-min wall clock | executed | 600 s timeouts | — |
| Pre-campaign API requirements 1–8 | executed | implemented before campaign | — |
| 30-turn budget | **modified** | advisory only; overruns C 50/D 38/E 48 on the repaired rows (E 32 as-run — outage rows masked overruns), max 88 | budget uncontrolled; §S5 sensitivity |
| Determinacy pre-screen (exclude) | **modified** | post-hoc dual annotation (79%, κ≈0.20); 74-task subset, none excluded | subset analysis, not screened population |
| Arm-D production server | **modified** | production Worker code served locally over shipped shards | none material |
| Single-shot elaboration grading | **modified** | persistent REPL, same pins | none material |
| ≤4 typecheck calls in-loop | **modified** | no in-loop typechecking at all | halluc rate is a pure grounding measure |
| Skill/ToolSearch leak | **modified** | sealed before eval; 120 dev runs quarantined | dev split discarded, eval clean |
| Per-arm cost distributions | **modified** | means only (USD + wall-clock) | descriptive (§S10) |
| faithful@budget (BEq+ equivalence) | **not executed** | grader a stub; zero judge files in campaign | primary endpoint missing; zero confirmatory results |
| LLM judge + 50-item human calibration | **not executed** (campaign) | harness existed unused; an *uncalibrated* blind judge pass ran post-review (§S11) | main §4.2 is exploratory only |
| tokens-to-solve (half the success criterion) | **not executed** | never computed (tokens recorded per row) | P2 untested as specified |
| 3 reseeds + pass@k curves | **not executed** | one run per (task, arm) everywhere | no variance estimate anywhere |
| Second model class on primary set | **not executed** | Tier-1 all-Haiku; v2 all-Sonnet on different benchmarks | capability-band generality unknown |
| Preregistered success criterion | **not executed** | neither half graded as specified | zero confirmatory results |
| Tier 2 as specified (FATE-H + MPR-Prop proving, reflection loop) | **not executed** | replaced by exploratory v2: QR/MPR retrieval + one-shot SorryDB, arms N/F/W/U/WF | main §§5–6 exploratory |
| PutnamBench | **not executed** | — | — |
| Tier 3a offline Erdős set | **not executed** | — | — |
| Tier 3b live Erdős queue | **not executed** | — | — |

## S2. Tier-1: raw-instrument tables and analysis bases

Sources for this section are `bench/analysis/tier1_reanalysis.{py,json,md}`,
`part1_fresh100_v2.{json,md}`, `fresh_clustered.{py,json,md}`, and
`success_repaired.{py,json,md}`. Grading pins: eval-341 on Lean
v4.32.0-rc1 / Mathlib `a33a5ccd`; fresh-100 on Lean v4.33.0-rc1 /
Mathlib `9944fe29`; the checkout C/E's file tools read is `61a5e4f338`
(content ~2026-07-10); ProofNet# source pin
`PAug/ProofNetSharp @ a8da405f` (MIT).

### S2.1 The three arm-E bases (raw instrument)

E's 31 infrastructure-dead rows (fresh_069–099, contiguous session-limit
429s; A–D verified zero errors) can be counted three ways. Cells show
grounded typecheck as n/N = % [Wilson 95% CI], on the **raw oracle**:

| arm | errors-as-failures (n=100) | completed pairs (n=69) | post-repair (n=100) |
|---|---|---|---|
| A | 20/100 = 20.0% [13.3, 28.9] | 10/69 = 14.5% [8.1, 24.7] | 20/100 = 20.0% [13.3, 28.9] |
| B | 22/100 = 22.0% [15.0, 31.1] | 12/69 = 17.4% [10.2, 28.0] | 22/100 = 22.0% [15.0, 31.1] |
| C | 25/100 = 25.0% [17.5, 34.3] | 12/69 = 17.4% [10.2, 28.0] | 25/100 = 25.0% [17.5, 34.3] |
| D | 42/100 = 42.0% [32.8, 51.8] | 28/69 = 40.6% [29.8, 52.4] | 42/100 = 42.0% [32.8, 51.8] |
| E | 16/100 = 16.0% [10.1, 24.4] — 31 infra errors | 16/69 = 23.2% [14.8, 34.4] | 30/100 = 30.0% [21.9, 39.6] |

Raw McNemar (exact binomial two-sided): errors-as-failures D-vs-E 32/6
p=2.4e-5 (conflates outage with behavior — quarantined here);
completed-69 D-vs-E 18/6 p=0.023, D-vs-C 21/5 p=0.0025, D-vs-A 22/4
p=0.0005; post-repair full-100 D-vs-E 25/13 p=0.073, D-vs-C 28/11
p=0.0095, D-vs-A 29/7 p=0.0003, E-vs-A 19/9 p=0.087. Of the 31 repaired
E rows, 14 became successes.

**Wald/McNemar mismatch note.** On the post-repair full-100 raw table,
the paired-Wald D−E risk-difference interval was [+0.0015, +0.2385]
(barely excluding 0) while exact McNemar gave p=0.073 — a normal
approximation over all 100 paired differences versus a test conditioned
on the 38 discordant pairs, landing on opposite sides of α=.05 near the
boundary. This is why v3 replaces both with a single commit-clustered
bootstrap whose interval and p-value are read off the same resampling
distribution.

### S2.2 Raw vs repaired instrument, side by side

Repaired oracle = `halluc_validation.py classify_adjusted` (rules R1–R5,
§S3), folded into success by `success_repaired.py`; success is monotone
under the repair (flags are only ever cleared). Affected rows
(raw-flagged, repaired-clean) and how many then typecheck:

| arm | affected (fresh) | typecheck pass | affected (eval-341) |
|---|---|---|---|
| A | 17 | 1 | 39 |
| B | 18 | 0 | 45 |
| C | 38 | 8 | 68 |
| D | 17 | 6 | 47 |
| E | 39 | 7 | 81 |
| total | 129 | 22 | 280 |

| arm | raw k/n [Wilson 95] | repaired k/n [Wilson 95] | Δ |
|---|---|---|---|
| A | 20/100 [13.3, 28.9] | 21/100 [14.2, 30.0] | +1 |
| B | 22/100 [15.0, 31.1] | 22/100 [15.0, 31.1] | +0 |
| C | 25/100 [17.5, 34.3] | 33/100 [24.6, 42.7] | +8 |
| D | 42/100 [32.8, 51.8] | 48/100 [38.5, 57.7] | +6 |
| E | 30/100 [21.9, 39.6] | 37/100 [28.2, 46.8] | +7 |

McNemar, both instruments:

| pair | raw b/c | raw p | repaired b/c | repaired p | classification |
|---|---|---|---|---|---|
| D vs E | 25/13 | 0.073 | 28/17 | 0.135 | ns → ns |
| D vs C | 28/11 | 0.0095 | 32/17 | 0.044 | sig → sig |
| D vs A | 29/7 | 0.00031 | 34/7 | 2.5e-5 | sig → sig |
| C vs A | 16/11 | 0.442 | 23/11 | 0.058 | ns → ns |
| E vs A | 19/9 | 0.087 | 24/8 | **0.007** | **ns → sig** |

Commit-clustered paired bootstrap (44 clusters, B=10,000; raw column
asserted byte-identical to `fresh_clustered.json` before the repaired
column was computed):

| pair | raw RD [95% CI] p | repaired RD [95% CI] p | classification |
|---|---|---|---|
| D − E | +0.120 [−0.049, +0.271] p=0.192 | +0.110 [−0.089, +0.279] p=0.304 | ns → ns |
| D − C | +0.170 [+0.011, +0.314] p=0.040 | +0.150 [−0.023, +0.302] p=0.100 | **sig → ns** |
| D − A | +0.220 [+0.093, +0.336] p=0.0014 | +0.270 [+0.131, +0.395] p=0.0004 | sig → sig |

Note the two classification changes at α=.05 cut in opposite directions
(E-vs-A strengthens, D-vs-C weakens) — the repair is not a thumb on
either scale.

### S2.3 Cluster census and sensitivity collapses

The 100 tasks decompose into **44 distinct source commits**, **57
files**, and **59 name-stem families** (24 multi-member). The largest
commit clusters are
`87a6eccf` (9 tasks, the AntitoneOn sum–integral family), `49ed1b2d`
(8, bounded variation), `61303857` (5), two of size 4. On the raw
instrument, +7 of D's net +12 paired advantage over E comes from the
9-task commit (D 7/9, E 0/9) and +3 from the 8-task commit (D 4/8,
E 1/8) — 83% of the net effect in 2 of 44 commits.

All pairs × all clustering levels (raw instrument, paired bootstrap RD
[95% CI] p):

| pair | task-level (iid) | family-clustered | file-clustered | commit-clustered |
|---|---|---|---|---|
| D − E | +0.120 [+0.000, +0.240] p=0.062 | +0.120 [−0.044, +0.274] p=0.186 | +0.120 [−0.044, +0.274] p=0.178 | +0.120 [−0.049, +0.271] p=0.192 |
| D − C | +0.170 [+0.050, +0.290] p=0.0064 | +0.170 [+0.021, +0.311] p=0.032 | +0.170 [+0.021, +0.310] p=0.030 | +0.170 [+0.011, +0.314] p=0.040 |
| D − A | +0.220 [+0.110, +0.330] p=0.0002 | +0.220 [+0.080, +0.346] p=0.0028 | +0.220 [+0.080, +0.344] p=0.0026 | +0.220 [+0.093, +0.336] p=0.0014 |

Family/file/commit-collapsed sensitivity (one unit per cluster,
majority and any-success rules) is tabulated in
`bench/analysis/fresh_clustered.md` §4; under every collapse D−E is a
null (p ≥ 0.52), D−A survives the any-success collapses (p=0.027–0.041),
and the tie-free mean collapse gives D−E +0.043 [−0.104, +0.183]
p=0.554, D−A +0.157 [+0.043, +0.264] p=0.0064.

### S2.4 Eval-341 (ProofNet#) raw table and the repaired-instrument gap

| arm | grounded typecheck (raw) | Wilson 95% CI |
|---|---|---|
| A | 204/341 = 59.8% | [54.5, 64.9] |
| B | 198/341 = 58.1% | [52.8, 63.2] |
| C | 218/341 = 63.9% | [58.7, 68.9] |
| D | 219/341 = 64.2% | [59.0, 69.1] |
| E | 208/341 = 61.0% | [55.7, 66.0] |

The 280 raw-flagged repaired-clean eval rows (per-arm counts in S2.2)
exceeded the typecheck budget and were **not** re-typechecked; the
counts are hard upper bounds on per-arm gains, and the instrument-bias
direction (C/E over-flagged relative to D) applies to this table too.
Typecheck alone anti-correlates with grounding on this split (E led
typecheck at 83.3% while trailing on hallucinations), reproducing
TheoremGraph's typecheck-is-not-a-signal finding at 15× their n.

## S3. Hallucination-oracle validation detail

Sources for this section are `bench/analysis/halluc_validation.{py,json,md}`,
with blinded intermediates in `bench/analysis/halluc_blind/`.

**Blinded protocol.** The audit drew a seeded (20260801), stratified
sample of 60 distinct cited names over arm × oracle-verdict strata (per arm: 6 flagged
hallucinated, 5 exists, 1 renamed). The grader saw only the blinded
sample (name + one context line, shuffled, no verdicts) and raw
`git grep` evidence at the pinned trees (`61a5e4f338`, cross-checked at
`9944fe2973`), wrote truth labels, and only then unsealed the key.
Grading required namespace resolution, `to_dual`-generation tracing,
`_root_.` declarations, and structure-field projections — exactly the
care an exact-string oracle cannot apply.

**Truth labels:** real 46 · nonexistent 4 · not_a_citation 9 (extractor
noise) · renamed 1.

**Confusion (oracle → truth):**

| | truth real/renamed | truth nonexistent | truth not_a_citation |
|---|---|---|---|
| oracle hallucinated (30) | 18 | 4 | 8 |
| oracle exists (25) | 24 | 0 | 1 |
| oracle renamed (5) | 5 | 0 | 0 |

**Binary scores** (positive = hallucinated): strict precision 4/30 =
**13.3%** [5.3, 29.7], recall 4/4 = 100% [51.0, 100]; lenient (noise
rows excluded) precision 18.2% [7.3, 38.5]. None of the oracle's 30
negative verdicts (25 *exists* + 5 *renamed*) was a missed fabrication
(FN=0); read as verdicts rather than a pooled negative class, 24/25
*exists* names denoted real declarations (one was extractor noise) and
4 of the 5 *renamed* verdicts were in truth real names.

**The four confirmed fabrications** (zero-hit at both revs), all from
arms without formal tools: `FractionField` (A; real name
`FractionRing`), `Localization.At` (B; `Localization.AtPrime`),
`Ideal.vanishingLocus` (B; `zeroLocus`/`vanishingIdeal`),
`Nat.divisorSum` (B; `ArithmeticFunction.sigma`).

**The 26 false flags decompose into six mechanical modes:** namespace
short names standard under `open` (13), self-declared theorem headers
(4), dot-notation on a variable (5, e.g. `M.det`), comment prose (2),
an import-line module name (1), a mid-dot-chain fragment (1).

**Repair rules R1–R5:** drop comment tokens, import-line names,
self-declarations, and single-letter dot-notation heads; classify a
name as existing if some single-segment prefix `NS.` makes it an
indexed declaration. Validation: the repaired classifier agrees with
the blinded truth on **59/60** (sole miss: the mid-dot-chain fragment
`Laurent.coeff.support`).

**Run-level rates, both instruments** (n=100/arm; citation-level shown
for context — citations cluster within runs, so run-level carries the
inference):

| arm | raw runs ≥1 halluc | raw citation-level | repaired runs ≥1 | repaired citation-level | R5 reclassified | R1–R4 dropped |
|---|---|---|---|---|---|---|
| A | 54/100 [44.3, 63.4] | 94/443 = 21.2% | 37/100 [28.2, 46.8] | 50/428 = 11.7% | 29 | 15 |
| B | 48/100 [38.5, 57.7] | 76/429 = 17.7% | 30/100 [21.9, 39.6] | 38/416 = 9.1% | 25 | 13 |
| C | 49/100 [39.4, 58.7] | 108/517 = 20.9% | 11/100 [6.3, 18.6] | 26/500 = 5.2% | 65 | 17 |
| D | 23/100 [15.8, 32.2] | 32/472 = 6.8% | 6/100 [2.8, 12.5] | 7/457 = 1.5% | 10 | 15 |
| E | 49/100 [39.4, 58.7] | 107/505 = 21.2% | 10/100 [5.5, 17.4] | 14/483 = 2.9% | 71 | 22 |

Raw run-level McNemar: D-vs-E p=1.6e-4, D-vs-C p=6.9e-5 (run-level
ratio 2.13×, not the citation-level 3.1×). Repaired: D-vs-E p=0.454,
D-vs-C p=0.302, D-vs-A p=1.23e-7, D-vs-B p=3.9e-5, E-vs-C p=1.0.
Commit-clustered bootstrap versions of the significant contrasts
(`v3_gate_fixes.json`): D-vs-A −0.310 [−0.446, −0.193] p=0.0002,
E-vs-A −0.270 [−0.396, −0.155] p=0.0002; the null D-vs-E / D-vs-C stay
null (clustered p=0.44 / 0.31). The
R5 column is the citation-style artifact quantified: C/E write
namespace-short names (65/71 reclassifications), D copies
fully-qualified names out of tool payloads (10).

**Held-out revalidation of the repaired classifier (seed 20260802).**
The five rules were formulated after the 60-name audit unsealed, so
59/60 is an in-sample fit. A second blinded pass
(`bench/analysis/halluc_holdout.{py,json,md}`; blinded intermediates
in `bench/analysis/holdout_blind/`) sampled **40 distinct
cited names disjoint from the 60** (stratified per arm: 4
raw-hallucinated, 3 raw-exists, 1 raw-renamed; shortfalls → exists)
from the post-repair fresh rows, graded them blind under the same
evidence protocol (raw `git grep` at `61a5e4f338`, cross-check
`9944fe2973`, run outputs for self-declaration/variable resolution),
and only then unsealed both instruments' verdicts. Truth labels:
real 31 · nonexistent 3 · not_a_citation 5 · renamed 1.

| metric (strict, n=40) | raw oracle | repaired classifier |
|---|---|---|
| flagged-class precision | 3/20 = 15.0% [5.2, 36.0] | 3/7 = 42.9% [15.8, 75.0] |
| recall (true fabrications) | 3/3 = 100% | 3/3 = 100% |
| accuracy | 57.5% | 90.0% |
| binary agreement (the in-sample 59/60 measure) | 23/40 | **36/40 = 90.0%** [76.9, 96.0] |

Sensitivity: reading the borderline `OrderedSemiring` (a class removed
by the ordered-algebra refactor; labeled renamed on the
Basis→Module.Basis precedent) as nonexistent gives repaired precision
4/7 = 57.1%. The verdict: **directionally replicates** — the raw
oracle's invalidity reproduces almost exactly (13.3%→15.0% precision),
the repair transfers most of its value (FPs on sampled flags 17→4,
accuracy 57.5%→90.0%, zero FNs for either instrument) — but the
in-sample fit was optimistic (98.3%→90.0% binary agreement, Fisher
p=0.15), and a repaired flag is roughly a coin toss, not a
confirmation. The four residual FP modes are ones R1–R5 never saw:
multi-segment namespace short names (`IsZero` ←
`CategoryTheory.Limits.IsZero`), dot-notation on a namespaced def
value (`MeasureTheory.volume.restrict`), a multi-char local variable
(`M001`), and the removed-class borderline (`OrderedSemiring`).
Implications: the adjusted per-arm rates likely overstate true
hallucination by roughly 2× and should be read as **upper bounds**;
two of the four residual FP modes are namespace-style-correlated (both
held-out instances land in C/E-style short-name arms), so the residual
bias still runs *against* the free-text tool arms and cannot
manufacture D's advantage; and all three held-out confirmed
fabrications (`Subgroup.torsion`, `Localization.map`,
`Module.GeneralizedEigenspace`) sit in the no-formal-tools arms A/B,
as in-sample.

## S4. Fresh-set exposure strata (full)

The source is `bench/analysis/fresh_exposure.{py,json,md}` (original,
snapshot basis), recomputed on the post-repair rows by
`bench/analysis/v3_gate_fixes.{py,json}` §1. Pinned tree `61a5e4f338`
(content ~2026-07-10), the rev C/E's file tools read. Outcomes below
are the **post-repair rows** (E's 31 outage rows replaced by their
2026-07-27 reruns) on the **raw** instrument; the superseded
snapshot-basis table (E's 429 rows counted as failures) is preserved
in `fresh_exposure.md`.

Exposure counts (of 100): full dotted name in-tree verbatim **37**;
basename as a declaration header anywhere **64**; basename as header in
the task's own module **51** (the stratum basis); gold's commit an
ancestor of the pin **49**. (Two tasks are exposed despite post-pin
merge commits — measured on tree bytes, not merge metadata.)

| arm | exposed (n=51) | unexposed (n=49) |
|---|---|---|
| A | 9/51 = 17.6% [9.6, 30.2] | 11/49 = 22.4% [13.0, 35.9] |
| B | 8/51 = 15.7% [8.2, 28.0] | 14/49 = 28.6% [17.8, 42.4] |
| C | 11/51 = 21.6% [12.5, 34.6] | 14/49 = 28.6% [17.8, 42.4] |
| D | 24/51 = 47.1% [34.1, 60.5] | 18/49 = 36.7% [24.7, 50.7] |
| E | 15/51 = 29.4% [18.7, 43.0] | 15/49 = 30.6% [19.5, 44.5] |

McNemar by stratum, post-repair: D-vs-E exposed 14/5 p=0.064,
unexposed 11/8 p=0.648; D-vs-C exposed 18/5 p=0.0106, unexposed 10/6
p=0.454. The leak's direction favors C/E. **A correction to earlier
editions:** the snapshot-basis version of this table showed D's edge
over E strongest in the *unexposed* stratum (17/3, p=0.0026), and v2
concluded that D's advantage was "strongest exactly where there was
nothing to leak." That pattern does not survive the E repair — 24 of
E's 31 outage rows fell in the unexposed stratum, so the stratum
contrast was outage-driven. On the repaired rows D-over-E, like
D-over-C, is *larger in the exposed stratum* (RD +0.18 vs +0.06) and
is a null unexposed; neither stratum is significant alone. The
merge-date split reproduces the repaired pattern (before-pin 14/5
p=0.064; after-pin 11/8 p=0.648), and the repaired-instrument
sensitivity gives the same qualitative picture (D-vs-E exposed 15/8
p=0.21; unexposed 13/9 p=0.52). The judge-endpoint exposure split is
§S11.4.

## S5. Turn-budget sensitivity

The source is `tier1_reanalysis.md` §5, corrected to the post-repair
rows by `v3_gate_fixes.py` §4. E overran the advisory 30-turn budget on
**48/100** repaired rows — 16 of the 31 rerun rows plus 32 of the 69
originals; the earlier as-run count of 32 treated E's 31 outage rows
(turns=1) as within budget, which also inflated the old within-budget
count (E 68) and the old n=45 pair analysis. Corrected per-arm
within-budget run counts: A 100, B 100, C 50, D 62, E 52. Restricted
to pairs where both arms stayed within the advisory 30 turns
(raw instrument, post-repair rows): D-vs-E n=35, 51.4% vs 40.0%, 7/3
discordant, p=0.34; D-vs-C n=35, 54.3% vs 25.7%, 11/1, p=0.0063;
D-vs-A n=62, 46.8% vs 25.8%, 19/6, p=0.0146. Consistent in direction;
for D-vs-E, inconclusive within budget.

## S6. Tool-call census, cost/latency, and figure 5

**Tier-1 eval-split trace attribution** (`bench/trace_analysis.py`):
98–100% of traced tool-arm runs cite at least one
declaration that surfaced in a tool result; 35% of arm-D eval runs cite
a declaration from a `brain_bridge`/`brain_transfer` result. Among
*hallucinated* citations (raw oracle), the checked-and-cited-anyway
rate on eval rows is 35% (C), 30% (E), 13% (D); on the fresh split —
the paper's primary evidence base — the same statistic is C 44%,
D 34%, E 61%: verify-before-cite discipline transferred imperfectly to
the held-out set, and worst in E.

**v2 census** (figure: `figures/fig5_tooluse.pdf` in the typeset
build, regenerated on the repaired run rows; the per-tool counts quoted
here are the as-run census — the race repair raises only F's means,
since its 190 condemned rows had zero calls; basis stated per clause): W averages 3.5 calls/run
on QR-810 and 10.6 on MPR, ≈4.0 pooled (QR-810 counts: decl_exists
1,251 + brain_bridge 608 — verify-then-cite made mechanical; pooled
QR+MPR counts: decl_exists ~1,473 + brain_bridge ~727); WF 4.6 pooled
QR+MPR in a genuine dual-toolkit mix (decl_grep ~1.5k, decl_exists
~0.9k, brain_bridge ~0.7k, decl_read ~0.5k, loogle ~0.4k); repaired F
averages 2.8 (QR) and 8.2 (MPR) calls/run. Under the manual, `brain_cell` misuse
(63.3% failure over 482 W-arm calls) falls to a single call in WF.
Same-eval-set caveat as the manual (§S8).

**The cold-start-race repair (grid provenance).** The §3.4 race was
found by the U launch and repaired across seven commits. `458891dc`
built the U arm (bare W ∪ F union, no manual); its first launch showed
13/49 rows with both MCP servers still `pending` at the init event and
zero tool calls, and `834a130a` taught the runner to capture that init
signature, condemn-and-retry such rows, stagger the first wave, and
raise the attach timeout. `99a2075f` byte-archived the 194 affected
grid rows (F: 175 QR-810 + 15 MPR; W: 2 + 2; manifest of qids
included) to `bench/v2/runs/agent/race_condemned_archive/`;
`f041928a` rerun all 194 attach-verified in place under the fixed
harness (the 4 stubborn W rows against the original fast-attaching
local worker — the asymmetry that had spared W), leaving **zero race
rows in every arm × benchmark**; N/U/WF audit race-free before and
after (N has no MCP by design), and pre-existing format/timeout
failures (N 143+15, F 1, W 5, WF 1) are untouched. `c1dea98f` landed
the 879 attach-validated U rows (26 condemned first attempts rerun;
$187.20); `322e84f5` and `dd3eb689` are the union-ablation and
repaired-grid analyses (`union_ablation.{py,json,md}`,
`grid_repaired.{py,json,md}`). Repair cost $33.08. Scores moved only
for F (QR R@10 .8309→.8457, nDCG .7901→.8089; MPR gR@10 .4532→.5468);
N/U/WF rows are byte-identical and W's aggregates are unchanged at
four decimals.

**Per-query cost/latency (QR-810)** — repaired grid, recomputed from
the final run rows (as-run table: `retrieval_repair.md` §3; CLI cost
estimates under Max auth):

| system | mean $/query | mean wall s | median wall s | mean calls | mean turns |
|---|---|---|---|---|---|
| N | 0.085 | 15.1 | 8.9 | 0.0 | 1.0 |
| F | 0.143 | 13.2 | 10.6 | 2.8 | 3.8 |
| W | 0.197 | 23.7 | 11.2 | 3.5 | 4.3 |
| U | 0.197 | 12.4 | 9.8 | 3.1 | 4.1 |
| WF | 0.207 | 15.2 | 12.5 | 4.2 | 5.2 |
| retriever-mode wikibrain | ~0 | — | — | 1 | — |

**MPR (repaired grid):** N $0.185/50.7 s/0 calls · F $0.370/38.8 s/8.2
· W $0.459/62.6 s/10.6 · U $0.403/33.1 s/7.9 · WF $0.418/38.2 s/9.3.
U-arm totals: $159.37 (QR-810) + $27.84 (MPR) = $187.20.

**N format-repair detail** (`retrieval_repair.md`): symmetric tiered
extraction (relaxed JSON → backticked names → bare dotted identifiers →
compound tokens) over all assistant text, identically for every arm's
empty rows. QR: N 143 empty rows → 104 repaired ≥1 name but only 12
recover the gold; strict 0.6333 → lenient 0.6481; oracle ceiling
0.8099. F/W/WF have 1/5/1 empty rows on the repaired grid (2/5/1
as-run) and move ≤0.0012. The five W
empties are 420 s hard timeouts, not format failures. MPR: N 15 empty →
1 gold recovered (0.2025 → 0.2062). Per-tool provenance detail
(surfaced-by-tool, first-written-in, per-style tables):
`bench/analysis/retrieval_provenance.md` — an as-run trace pass; its
F row counts the 175 race rows' hits as memory (main §5 caveat).

## S7. Snapshot rot: the detailed narrative

The original SorryDB freeze (8 repositories, 164 tasks) was prepared
against sorrydb.org's SorryDB-2601 evaluation split with pinned
upstream commits. At verification time — roughly six months after the
pins were minted — 74 of 164 tasks (45%) were unreproducible: the
pinned commits of GlimpseOfLean, LeanCourse25, and most Foundation and
SemicircleLaw tasks no longer exist on GitHub, their branches
force-pushed or rebased away. The rot is invisible to any cheap check:
GitHub's archive endpoint answers a preflight HEAD request for a dead
commit with HTTP 200, and only an actual fetch fails. The refreeze
(`bench/v2/sorrydb_prep.py`, commit `db679e8d`) keeps
verified-fetchable pins only — every task's repository archive
downloaded and its splice point confirmed — yielding 171 tasks across
10 repositories. Lesson for benchmark builders: a pin is not a snapshot;
archive the bytes, not the reference, and verify by fetching, not by
HEAD.

## S8. The WF agent manual (verbatim) and its timeline

**Timeline (verified commit times).** N/F/W results committed 22:27 on
2026-07-24; the manual 00:30 on 2026-07-25; WF results 01:15 on
2026-07-25. The manual distills measurements from the same evaluation
queries WF was then scored on — its bracketed figures (per-style nDCG,
the 63% brain_cell failure rate, the 143/810 format failures, call
counts) are the N/F/W/system-mode eval-set aggregates, reproduced
exactly. WF is therefore development evidence of a maximally equipped,
briefed agent, not an untouched test result. (v1 also misattributed the
63% figure to "the Tier-1 traces"; it is the v2 W-arm figure — 482
calls, 63.3% failed.)

> `bench/v2/AGENT_MANUAL.md`, prepended verbatim to every WF-arm
> prompt; a **post-hoc, benchmark-informed** artifact.

# Tool manual: searching Mathlib with the Brain + formal search

You have two complementary toolkits. Every claim in this manual is measured
from ~5,500 logged tool calls across 3,500 benchmark runs (Tier-1 +
Bridge v2); numbers in brackets are those measurements.

## The mental model

- **The Brain (wikibrain, 8 tools)** is a *curated knowledge graph* joining
  informal mathematics (Wikipedia/Wikidata concepts, external databases) to
  Mathlib declarations. It knows what things *mean* and how concepts relate —
  but it only "atomizes" declarations that some concept, annotation, or tag
  claims (~12k of Mathlib's declarations). It is a map, not the territory.
- **Formal search (3 tools)** operates on *all* of Mathlib directly: type-
  pattern search (loogle), source grep, source reading. It knows everything
  that exists but nothing about what it means informally.

Rule of thumb: **route by what you're holding.** Holding a concept described
in words → start with the Brain. Holding a type shape or name fragment →
start with formal search. Always finish with `decl_exists` on every name you
intend to output.

## Formal search tools

### loogle
Type-pattern and constant search (the public loogle.lean-lang.org service).
- USE for: "a lemma whose statement looks like `_ * _ ≤ _ * _`", searches by
  involved constants (`Real.sqrt`, `tsum`), name substrings.
- Strengths: covers ALL of Mathlib; ~0% call errors [578 calls, 0 errors];
  the best premise-finding tool — the loogle+grep toolkit matched a
  specialist premise retriever [group-recall@10 0.453 vs 0.461 SOTA].
- Weaknesses: needs Lean syntax fluency; a wrong pattern silently returns
  unrelated hits; small result payloads mean you often need decl_read next.

### decl_grep
Regex search over the Mathlib source checkout (ripgrep).
- USE for: name fragments ("cantor"), notation, docstring phrases, finding
  the file a declaration lives in.
- Strengths: the workhorse [1,206 calls, 0% errors]; catches things loogle's
  type index can't (comments, docstrings, naming conventions).
- Weaknesses: lexical only — if the concept's name doesn't appear in source,
  grep cannot find it (descriptive queries like "special case of X" score
  worst for grep-based agents [nDCG 0.384 vs the Brain's 0.523]).

### decl_read
Read declaration source (with surrounding context) from the checkout.
- USE for: verifying a candidate actually says what you think BEFORE citing
  it; getting exact signatures and hypotheses.
- Strengths: ground truth for any decl in Mathlib [397 calls, 0% errors].
- Weaknesses: you need the file/name first — it is a confirmation tool, not
  a discovery tool.

## Brain tools

### brain_bridge — THE ENTRY POINT for informal→formal
Free text in, existence-verified declarations + owning atoms out, with
signatures, import lines, and one-hop dependency context.
- USE for: "the concept described as ⟨words⟩" — especially nicknames and
  special-case descriptions, where the Brain's curated bonds beat grep
  [special_case nDCG 0.523 vs 0.384; nickname 0.779 vs 0.757].
- CRITICAL LIMITATION (measured): its free-text matching is label/alias
  anchored, NOT semantic. A single call with a descriptive paraphrase
  usually misses [single-shot benchmark: 3.6% R@10 vs 82% for an agent that
  reformulates]. **Never accept one miss as the answer: reformulate 2–4
  times** — try the concept's canonical name, a synonym, the head noun alone.
  8.9% of calls return match:"none" [727 calls]; that response includes
  nearest-candidate suggestions — read them.
- The `hits` list is existence-verified; bond quality matters: `exact` means
  the decl IS the concept; `formalizes`/weaker bonds are nearby.

### brain_search
Fuzzy label + alias search returning atoms (cells).
- USE for: resolving a name you half-know into an atom id; browsing what the
  Brain calls something [400 calls, 0% errors — the reliable fallback when
  brain_bridge misses].
- Weaknesses: returns atoms, not ranked decls — follow with brain_cell on
  the winning atom.

### brain_cell
The full atom card: Lean code, Wikidata description, DB snippets, organs.
- USE for: everything known about ONE object after bridge/search resolved it.
- **THE #1 MISUSE [63% of 482 calls failed]: calling it with a bare
  `decl:Mathlib:Name` for an arbitrary declaration.** The Brain only has
  atoms for concept-joined decls; "unresolvable key" means *no atom owns
  this decl*, NOT that the decl doesn't exist. For arbitrary decls use
  decl_exists (works for all of Mathlib) and decl_read for source. Call
  brain_cell only with ids that came OUT of a Brain tool.

### decl_exists — THE DISCIPLINE TOOL
Batch existence check for declaration names (backed by the full oracle —
covers ALL of Mathlib, unlike brain_cell).
- **USE ALWAYS, on every name you are about to output.** This is the
  measured difference between grounded and hallucinated citations: agents
  that verified before citing cut hallucinated-but-cited-anyway to 13% vs
  33% for agents relying on fuzzy search results alone. It is cheap
  [1,473 calls, 0.1% errors] and batched — check 10 names in one call.
- Also returns verified-rename suggestions for stale names.

### brain_neighborhood
The synapse graph around an atom (depends/links/relates, cursored).
- USE for: "what connects to X" — finding the connecting lemma between two
  concepts, walking dependencies.
- Weaknesses: needs a valid atom id [24.8% of calls errored — same
  unresolvable-key trap as brain_cell; resolve the atom first].

### brain_transfer / brain_snippets / brain_filter
Narrower tools: label↔decl jumps for KNOWN labels (transfer), stored source
snippets (snippets), facet enumeration (filter). High misuse rates when fed
non-atom ids [40%/57% errors]. Prefer brain_bridge/brain_search entry; reach
for these only on ids the Brain already returned.

## Routing playbook

| You are holding | Do this |
|---|---|
| A concept in words | brain_bridge → (miss?) reformulate → brain_search → brain_cell |
| A special case / nickname | brain_bridge (the Brain's best terrain) |
| A type shape | loogle |
| A name fragment | decl_grep |
| Premises for a proof | loogle + decl_grep FIRST [0.453 vs 0.272], Brain for concept grounding |
| A candidate name to cite | decl_exists (batch, always) → decl_read if the statement matters |
| An atom id from any Brain tool | brain_cell / brain_neighborhood freely |
| A bare decl name needing info | decl_exists + decl_read — NOT brain_cell |

## Anti-patterns (each one measured in prior runs)

1. **Citing unverified names.** 33% of hallucinated citations came from
   agents that had search evidence in front of them and cited a wrong name
   anyway. decl_exists is binary and kills this.
2. **One query, then giving up.** The single-call miss rate on descriptive
   queries is ~96%; agents that reformulate reach ~82%. Iteration IS the
   algorithm.
3. **brain_cell as a generic decl inspector.** 63% failure rate. Brain
   atoms ≠ all of Mathlib.
4. **Answering outside the requested format.** 143/810 no-tool runs were
   scored 0 for exactly this. Re-read the output contract before replying.
5. **Inventing tool names** (e.g. `logue`, `lookie`). Use the names above.

*(End of verbatim manual.)* Its bracketed measurements are the as-run
pre-repair aggregates as WF saw them — e.g. the loogle+grep premise
figure it quotes as 0.453 is 0.547 on the repaired grid (§S6) — and
are preserved verbatim, not corrected.

## S9. SorryDB: full bookkeeping

The frozen split holds n=171 tasks/arm across 10 repositories, and all
203 candidate verdicts are definitive — 183 kernel pass/fail plus 19
unspliceable and 1 verify-timeout that never reached the kernel (commit `9682b3b1`; the 8
curve25519 stragglers verified as 0 proved). `verify.jsonl` carries 2
additional off-frozen N rows verdicted `env_broken` (205 lines total),
correctly excluded from the 203.

| | N no tools | F formal | WF union+manual (post-hoc) |
|---|---|---|---|
| run rows present | 168 | 169 | 171 |
| no output (timeout / no lean block) | 70 | 15 | 11 |
| honest give-up (literal `sorry`) | 40 | 83 | 86 |
| candidate proofs submitted | 58 | 71 | 74 |
| **proved (kernel)** | **2** | **9** | **10** |
| failed | 50 | 55 | 57 |
| unspliceable | 5 | 7 | 7 |
| verify timeout | 1 | 0 | 0 |
| proved / 171 | 1.2% [0.3, 4.2] | 5.3% [2.8, 9.7] | 5.8% [3.2, 10.4] |
| repo-clustered boot 95% CI | [0.0, 2.7] | [0.0, 10.7] | [0.0, 12.2] |
| proved / candidates | 3.4% | 12.7% | 13.5% |
| total cost (USD) | 58.97 | 102.76 | 108.87 |
| cost per proved | 29.48 | 11.42 | 10.89 |
| mean wall-clock / task | 120 s | 198 s | 183 s |

3 N and 2 F tasks lack run rows (runner losses). Distinct sorries
proved: 13; N's are a strict subset of F's and WF's; WF∩F 6, WF-only 4,
F-only 3. Per-repo: PersistentDecomp N1/F3/WF3, LeanAPAP 0/4/5, MiscYD
1/2/2, the other seven repos 0 everywhere. WF−F: +0.0058,
repo-clustered 95% CI [+0.0000, +0.0185] (the lower endpoint is exactly
0 because WF ≥ F in every repo; 34% of resamples show no difference or
worse); task-level exact McNemar 4/3, p=1.0. Cost-per-proved is
bookkeeping only (unstable at 2/9/10 successes). The published SorryDB
agentic best (~30%) is not comparable: different task subset, different
protocol (published agents iterate against the build; ours drafted
one-shot).

## S10. Per-arm cost tables

**Tier-1 campaign means per task** (all 471 runs/arm as run,
pre-repair; E's 31 error rows cost ≈0): A $0.034 · B $0.048 · C $0.140
· D $0.121 · E $0.128; mean wall-clock C 116 s, D 89 s, E 106 s.
tokens-to-solve was never computed, so P2 is untested; v1's "cheaper
and better" framing remains withdrawn. **Judge pass:** $81.86
(CLI-reported) for 500 gradings + the 50-item re-grade. **v2
retrieval:** per-query tables in §S6. **SorryDB:** totals in §S9.

## S11. Blind LLM-judge grading: full tables

Sources are `bench/analysis/judge_fresh_run.py` and
`judge_fresh_summary.{py,json,md}`, with the repaired-leg conjunction
from `bench/analysis/conjunction_repaired.{py,json}` (deterministic —
no sampling). Judge `claude-sonnet-5`; blind
(informal statement + gold with binders + candidate; no arm identity,
no tools, empty cwd); 500/500 rows, 0 errors; no-output rows auto-fail.
**Uncalibrated; exploratory.** The judge ran before the oracle repair,
so the conjunction is reported under both typecheck instruments; the
judge leg is identical in both.

### S11.1 Fresh-100 rates

Strict equivalence (same proposition, same hypotheses):

| arm | k/n | Wilson 95% CI |
|---|---|---|
| A | 11/100 | [6.2, 18.6] |
| B | 9/100 | [4.8, 16.2] |
| C | 43/100 | [33.7, 52.8] |
| D | 8/100 | [4.1, 15.0] |
| E | 46/100 | [36.6, 55.7] |

Evaluated equivalence (mathematical equivalence, high confidence):
A 17/100 [10.9, 25.6] · B 16/100 [10.1, 24.4] · C 51/100 [41.3, 60.6] ·
D 19/100 [12.5, 27.8] · E 53/100 [43.3, 62.5].

Conjunction (grounded-typecheck ∧ evaluated), both instruments:

| arm | raw leg | repaired leg [Wilson 95] |
|---|---|---|
| A | 8/100 | 8/100 [4.1, 15.0] |
| B | 9/100 | 9/100 [4.8, 16.2] |
| C | 16/100 | 20/100 [13.3, 28.9] |
| D | 15/100 | 16/100 [10.1, 24.4] |
| E | 18/100 | 21/100 [14.2, 30.0] |

Eight rows flip under the repair (C 4, E 3, D 1 — the repaired-clean
typecheck passes that the judge also evaluated equivalent); B/A are
essentially untouched, and between-tool parity is preserved.

### S11.2 McNemar, fresh-100

Evaluated equivalence: D-vs-E 1/35 p=1.1e-9 · D-vs-C 3/35 p=6.7e-8 ·
D-vs-A 7/5 p=0.774 · E-vs-A 37/1 p=2.8e-10 · E-vs-B 39/2 p=7.8e-10 ·
E-vs-C 13/11 p=0.839.

Conjunction, raw leg: D-vs-E 6/9 p=0.607 · D-vs-C 8/9 p=1.0 · D-vs-A
9/2 p=0.065 · E-vs-A 11/1 p=0.0064 · E-vs-B 12/3 p=0.035 · E-vs-C 6/4
p=0.754.

Conjunction, repaired leg (`conjunction_repaired.py`): D-vs-E 5/10
p=0.302 · D-vs-C 8/12 p=0.503 · D-vs-A 10/2 p=0.039 · E-vs-A 14/1
p=0.00098 · E-vs-B 14/2 p=0.0042 · E-vs-C 6/5 p=1.0. The only
classification change at α=.05 is D-vs-A ns → sig — a strengthening of
tools-versus-none; every between-tool conjunction contrast stays null
under both instruments.

Commit-clustered bootstrap versions (44 clusters, B=10,000;
`v3_gate_fixes.json`): evaluated equivalence D-vs-E −0.340
[−0.462, −0.220] p=0.0002, E-vs-A +0.360 [+0.233, +0.469] p=0.0002;
conjunction (repaired leg) D-vs-A +0.080 [+0.011, +0.152] p=0.037,
E-vs-A +0.130 [+0.055, +0.226] p=0.0002, D-vs-E −0.050
[−0.143, +0.021] p=0.247. No classification changes: everything
significant unclustered survives clustering, and the nulls stay null.

### S11.3 Completed-69 continuity

Evaluated: A 10/69 (.145) · B 9/69 (.130) · C 37/69 (.536) · D 11/69
(.159) · E 37/69 (.536); D-vs-E 0/26 p=3.0e-8; conjunction (raw leg):
C 8/69 (.116) · D 10/69 (.145) · E 9/69 (.130), D-vs-E 4/3 p=1.0,
D-vs-A 7/0 p=0.016; conjunction (repaired leg): C, D, and E at exactly
11/69 (.159) each, D-vs-E 4/4 p=1.0, D-vs-A 8/0 p=0.0078.

### S11.4 Exposure split (evaluated equivalence)

| arm | exposed (n=51) | unexposed (n=49) |
|---|---|---|
| A | 8/51 (.157) | 9/49 (.184) |
| B | 9/51 (.176) | 7/49 (.143) |
| C | 36/51 (.706) | 15/49 (.306) |
| D | 10/51 (.196) | 9/49 (.184) |
| E | 35/51 (.686) | 18/49 (.367) |

D-vs-E exposed: 1/26, p=4.2e-7. D-vs-E unexposed: 0/9, p=0.0039 — the
inversion narrows off-leak but does not vanish; E produces more
judge-equivalent statements than D even where the gold was not
retrievable. The conjunction (S11.2) is where parity appears. This is
reported as measured; the human queue below is how it gets adjudicated.

### S11.5 Self-consistency and the human queue

Fixed 50-item seed-stratified re-grade (seed 20260727, 10/arm): strict
agreement 98.0% (one flip), evaluated agreement 100.0%. Human grading
queue = the 36 D/E-discordant tasks on evaluated equivalence (they
drive the D-vs-E McNemar): fresh_002, 004, 006, 007, 011, 012, 013,
014, 019, 021, 023, 026, 027, 032, 044, 048, 049, 050, 051, 056, 057,
059, 062, 063, 064, 067, 069, 076, 077, 085, 086, 092, 094, 095, 096,
099.

## S12. Responses to the external reviews

Both reviews are preserved verbatim in `docs/research/review/`
(REVIEW-1.md, REVIEW-2.md), with our independent claim-by-claim
verification of review 1 in `verification-of-review-1.json`.

### S12.1 Review 1 (of v1, 2026-07-27) — actions taken in v2

All eight concerns confirmed in substance (29 claims confirmed, 1
partial, 1 refuted). Two factual claims did not survive: MathlibMPR
does not cluster by PR (69 tasks, 69 distinct PRs), and arm E's 31
declaration-less runs were contiguous 429 errors, not interface
overload — the second correction removed both the review's overload
reading and v1's error-inflated headline p=2.4e-5.

| # | Concern | Action |
|---|---|---|
| 1 | "Success" measures no semantic correctness | renamed grounded typecheck rate; judge pass; calibration owed |
| 2 | D vs E does not isolate the join | "join carries the effect" retired; bundled-condition language; the 2×2 factorial has since been run (v3.1, §4.4/§S15): both main effects null |
| 3 | Arm E failed its manipulation check | disclosed; yoked control on the roadmap |
| 4 | Fresh set not isolated from formal-search sources | renamed post-Brain-index; exposure strata; frozen snapshots on the roadmap |
| 5 | WF is test-set tuned | labeled post-hoc, benchmark-informed everywhere; provenance + timeline (§S8); bare-union arm launched |
| 6 | Independence and execution problems | declaration-clustered reanalysis; CIs throughout; disclosures |
| 7 | Preregistered experiment incomplete; deviations silent | execution inventory (§S1); register labels; zero-confirmatory up front |
| 8 | SorryDB evidence thin | all 203 verdicts completed; WF-vs-F indistinguishable; cost claim deleted |

### S12.2 Review 2 (of the v2 draft, 2026-08-01) — actions taken in v3

| # | Recommendation | Action in v3 |
|---|---|---|
| 1 | Lead with repaired full-100; one coherent paired inference | commit-clustered paired bootstrap is the only main-text framework; completed-69 and Wald/McNemar mismatch moved here (§S2) |
| 2 | Cluster the fresh set by commit/family | done (`fresh_clustered.py`): 44 commits, 57 files, 59 families; all levels + collapses (§S2.3); D−E widens exactly as predicted |
| 3 | Human semantic evaluation | full 500-row blind judge complete with self-consistency; 36-task discordant queue defined; **human grading and calibration still pending** — main §4.2 stays exploratory |
| 4 | Factorial ablation | DONE in v3.1: the preregistered 2×2 (prereg `3658bd58`, before any row) is run and analyzed (§4.4/§S15) — join +0.030 p=0.645, verifier +0.050 p=0.230, interaction −0.100 (redundancy); the bare-union U ablation had already shown the union inert (U−F null everywhere), the WF gain manual-driven on QR, and formal tools carrying MPR |
| 5 | Characterize the Brain; retrieval provenance decomposition | done (`brain_artifact.py`, main §3.1; `retrieval_provenance.py`, main §5) |
| 6 | Real related work (CRAMF, Aria, +) | done; every citation verified against arXiv on 2026-08-01 (`docs/research/review/related_work_notes.md`); comparison table in main §2 |
| 7 | Correct two overclaims (memorization; "clean verifier finding") | memorization → benchmark-familiarity language (main §4.1); verifier finding restated after blinded oracle validation — between-tool contrast dissolves under the repaired instrument (main §4.3), DDR cited as convergent evidence |
| 8 | Repair the retrieval evaluation | symmetric lenient extraction for every arm, cost/calls columns, retriever-vs-agent blocks, Brain-gold coverage, provenance decomposition (main §5, §S6); cold-start-race grid repair + U ablation complete; WF label kept |
| 9 | Reproducibility and conflict disclosures | data availability (§S14), AI-use + conflict statement (main §7) |
| cuts | move manuals, inventories, strata, response tables, file maps out | this supplement is that move |

## S13. Changelog

- **v1 (2026-07-25).** Initial report. Headline claims later retired:
  "the join carries the effect" (p<0.0001 driven by an unrecognized
  outage), "42% success" (no equivalence leg), "contamination-proof
  fresh set", WF as a SOTA row, cost-per-success claims.
- **v2 (2026-07-31).** Post-review-1 corrected edition: metric renamed
  grounded typecheck; arm-E 429 block repaired; register labels
  (preregistered-modified / exploratory / corrective) and
  zero-confirmatory statement up front; exposure strata;
  declaration-clustered retrieval statistics; WF relabeled post-hoc;
  SorryDB verdicts completed; judge and union slots left open.
- **v3 (2026-08-02, this edition).** Post-review-2 restructure into an
  8-page paper + this supplement. New evidence: blinded
  hallucination-oracle validation (13.3% precision) + 5-rule repaired
  instrument, with all Tier-1 headline numbers recomputed under it;
  commit-clustered bootstrap as the single inferential framework;
  completed blind judge pass (500/500) with the three-layer
  contamination×endpoint analysis; Brain artifact characterization;
  retrieval provenance decomposition (verification + routing, not
  content); N format-failure repair; the cold-start-race grid repair
  (194 rows rerun attach-verified; F QR R@10 .831→.846, MPR .453→.547)
  with the bare-union U ablation and final contrast set; the
  repaired-leg judge conjunction; verified related work; cost
  columns; conflict/AI-use disclosures. Still open: human grading of
  the 36-task queue, judge calibration, the 2×2 factorial.
- **v3 post-gate corrections (2026-08-03).** After an adversarial
  verification gate: held-out blinded revalidation of the repaired
  oracle (§S3; flags become upper bounds); §S4 exposure strata and §S5
  turn budgets recomputed on the post-repair rows (the
  "strongest-where-nothing-to-leak" claim retracted as outage-driven;
  E overruns 48/100); commit-clustered bootstraps extended to every
  significant §4.2/§4.3 contrast; Tier-1 attach audit; fresh-task
  provenance paragraph; five-arm judge Layer 1; census bases
  relabeled; assorted number and wording corrections (681, S6 fresh
  rates, SorryDB verdict categories, anchor provenance).
- **v3.1 (2026-08-18).** The preregistered 2×2 join × verifier
  factorial, run and analyzed exactly per `BRIDGE-FACTORIAL.md`
  (prereg `3658bd58` 2026-08-07; 400 rows `ba35fe7f`; new main-text
  §4.4 + §S15): both preregistered main effects null (join +0.030
  p=0.645, verifier +0.050 p=0.230), exploratory interaction −0.100
  (redundancy, not synergy); abstract, §1, §7, and §8 updated to the
  causal answer. One scoring-phase infrastructure incident (REPL
  server death mid-D′; the whole arm re-typechecked, §S15.1) documented with
  full cell provenance. Judge-graded secondaries completed same-day
  after an authentication delay (400/400 + 40-item re-grade,
  self-consistency 100%/97.5%): the §4.2 contamination-by-endpoint
  inversion reproduces (evaluated-equivalence JOIN −0.265 p=0.0002,
  exploratory; conjunction parity, all effects null; §S15.4). Still
  open: the 36-task human queue and judge calibration.

## S14. Data availability and file map

Everything needed to recompute the report lives in the private
preservation repository **`Deicyde/wikilean-bridge-experiment`** (report
at the root; supplement under `report/`). Provenance commits below
refer to the WikiLean working repository.

**Key commits.** `0d36f266` preregistration (2026-07-16) · `f97e67e4`
review-1 preservation + byte-preserved 429 archive · `19a90209` arm-E
repair driver (MCP-attach detection) · `64c48052` v2 corrective
statistics (tier1_reanalysis, fresh_exposure, retrieval_clustered) ·
`9682b3b1` SorryDB kernel verdicts complete (203/203) · `36f3a472`
report v2 + scored summary v2 (`bridge_summary_v2.json`) · `662db420`
review-2 analyses (commit-clustered inference, blinded oracle
validation, N format repair, Brain artifact table, provenance
decomposition) · `3cb8fa55` blind judge grading complete (500/500,
self-consistency, conjunction tables, human queue) · `9d10e44f`
repaired-oracle Tier-1 recompute (`success_repaired`) · `458891dc`
U-arm harness (bare W ∪ F union, no manual) · `834a130a`
cold-start-race condemnation + staggered launches · `c1dea98f` U run
rows (879, attach-validated) · `322e84f5` union-ablation analysis ·
`99a2075f` race-row archive (194 rows, byte-preserved) · `f041928a`
repaired F/W run rows (zero race rows remain) · `dd3eb689`
repaired-grid contrasts + refreshed union ablation · `2a9f6b91` REPL
routing fix ("honest labels").

| Path (preservation repo) | Contents |
|---|---|
| `docs/research/BRIDGE-EXPERIMENT.md` | preregistration incl. deviations 1–7 |
| `docs/research/review/` | reviews 1–2 verbatim; verification-of-review-1.json; related_work_notes.md (verified citations) |
| `bench/*.py`, `bench/arms/` | Tier-1 harness: runner, scorer, REPL rig, trace analysis, arm manifests |
| `bench/data/bridge_tasks.jsonl` | 371 ProofNet# tasks (pinned fork, MIT) |
| `bench/data/fresh_tasks.jsonl` | 100 post-Brain-index tasks + determinacy annotations + holdout check |
| `bench/data/runs/` | all 2,355 Tier-1 run rows incl. 31 repaired E rows |
| `bench/data/runs_E_fresh_429_archive/` | the 31 original 429 rows, byte-preserved |
| `bench/analysis/bridge_summary_v2.json` | scored fresh-500 summary + repair provenance |
| `bench/analysis/tier1_reanalysis.*`, `fresh_exposure.*`, `retrieval_clustered.*` | v2 corrective statistics |
| `bench/analysis/fresh_clustered.*`, `halluc_validation.*` (+`halluc_blind/`), `success_repaired.*` | v3: clustered inference; blinded oracle validation; repaired-instrument recompute |
| `bench/analysis/halluc_holdout.*` (+`holdout_blind/`) | held-out blinded revalidation of the repaired oracle (40 disjoint names, seed 20260802) |
| `bench/analysis/v3_gate_fixes.{py,json,md}` | post-gate analyses: post-repair S4 strata, clustered §4.2/§4.3 bootstraps, Tier-1 attach audit, corrected turn budgets, five-arm judge table, fresh-task provenance |
| `bench/analysis/judge_fresh_run.py`, `judge_fresh/`, `judge_fresh_summary.*` | blind judge pass (500 gradings + re-grade) |
| `bench/analysis/conjunction_repaired.{py,json}` | judge conjunction under both typecheck instruments (deterministic) |
| `bench/analysis/retrieval_repair.*`, `retrieval_provenance.*`, `brain_artifact.*` | v3: format repair; provenance decomposition (as-run trace pass); artifact characterization |
| `bench/analysis/union_ablation.*`, `grid_repaired.*` | U-arm bare-union ablation; race-repaired grid, before/after tables + final contrast set |
| `bench/v2/runs/agent/race_condemned_archive/` | the 194 original cold-start-race rows, byte-preserved with qid manifest |
| `bench/v2/` + `bench/v2/data/` + `bench/v2/runs/` | v2 harness, AGENT_MANUAL.md, MathlibQR/MPR (CC BY 4.0), SorryDB freeze, all v2 run rows (repaired grid) with gzipped stream transcripts, verify.jsonl (203 verdicts) |
| `docs/research/BRIDGE-FACTORIAL.md` | the 2×2 factorial preregistration (commit `3658bd58`, byte-unchanged; empty deviations log) |
| `bench/factorial/` | factorial runner (`run_factorial.py`) + prereg-time live-index census |
| `bench/arms/mcp-{Ep,X,J,Dp}.json` | the four factorial arm MCP configs |
| `bench/data/runs_factorial/` | all 400 factorial rows + gzipped stream transcripts + `conditions.json` (commit `ba35fe7f`) |
| `bench/analysis/factorial_scored.{py,json,md}` | Stage-1 mechanical scoring (repaired+raw oracle, fresh-pin typecheck, integrity gate, `retc_provenance`) |
| `bench/analysis/judge_factorial_run.py` + `judge_factorial/` | Stage-2 blinded-judge driver + all 400 verdicts + 40-item consistency re-grade (§S15.4) |
| `bench/analysis/factorial_analysis.{py,json,md}` | Stage-3 preregistered analysis (effects, pairwise grid, sensitivity cuts) |

**Recomputation entry points.** Factorial
`bench/analysis/factorial_scored.py` + `factorial_analysis.py`
(runner `bench/factorial/run_factorial.py`); Tier-1 grading `bench/score_bridge.py`;
repaired instrument `bench/analysis/halluc_validation.py` (`adjusted`)
+ `success_repaired.py` + held-out revalidation `halluc_holdout.py`;
post-gate analyses `v3_gate_fixes.py`; clustered inference
`fresh_clustered.py`;
exposure `fresh_exposure.py`; judge `judge_fresh_summary.py` +
`conjunction_repaired.py`; retrieval
`bench/v2/score_retrieval.py` + `retrieval_clustered.py` +
`retrieval_repair.py` + `retrieval_provenance.py`; repaired grid +
union ablation `grid_repaired.py` + `union_ablation.py`; artifact
`brain_artifact.py`; SorryDB `bench/v2/verify_sorrydb.py`.

**External sources** (fetched and verified during the v2/v3 design
sweeps): TheoremGraph arXiv:2606.25363 · LeanSearch arXiv:2403.13310 ·
LeanSearch-v2 arXiv:2605.13137 + github.com/frenzymath/LeanSearch-v2 ·
LeanExplore arXiv:2506.11085 · SorryDB arXiv:2603.02668 + sorrydb.org ·
FATE arXiv:2511.02872 · miniCTX arXiv:2408.03350 · LeanDojo
arXiv:2306.15626 · LeanAgent arXiv:2410.06209 · Numina-Lean-Agent
arXiv:2601.14027 · miniF2F-v2 arXiv:2511.03108 · ProofNet#/ProofNetVerif
arXiv:2406.07222 · benchmark-faults survey arXiv:2606.29493 · CRAMF
arXiv:2508.06931 · Aria arXiv:2510.04520 · DRIFT arXiv:2510.10815 ·
DDR arXiv:2511.11990 (the last four verified 2026-08-01,
`related_work_notes.md`).

## S15. The preregistered 2×2 factorial (join × verifier): full detail

Preregistration `docs/research/BRIDGE-FACTORIAL.md`, commit `3658bd58`
(2026-08-07) — committed before the harness was built and before any row
ran; the file is byte-unchanged since, and its §9 deviations log is
empty. Harness commit `cbdcf3b7`; the 400 run rows (+ gzipped stream
transcripts + `conditions.json`) commit `ba35fe7f`. Scoring, judging,
and analysis are a separate later phase (prereg §4.9), by a different
agent from the runner. Artifacts: `bench/analysis/factorial_scored.*`
(Stage 1), `bench/analysis/judge_factorial_run.py` + `judge_factorial/`
(Stage 2), `bench/analysis/factorial_analysis.*` (Stage 3).

### S15.1 Arms, execution, and integrity

2×2 over JOIN (Wikibrain joined surface replaces the unjoined
wiki+formal stack) × VERIFIER (`decl_exists` present): E′ = arm E's 6
tools; X = E′ + `decl_exists` (7); J = the 7 Wikibrain graph/content
tools; D′ = all 8 (arm D exactly). All arms: `--tools ""` (empty
built-in set), per-row allowed/disallowed manifests, model
claude-haiku-4-5-20251001, CLI 2.1.153, prompt byte-identical to
Tier-1, `--max-turns 30` (mechanical), formal-search reads pinned to
the 61a5e4f338 tree, one interleaved order (seed 20260803 shuffle of
all 400 pairs), concurrency 4 with staggered first wave, per-row
attach-signature + manifest validation with auto-condemn/retry.

Run 2026-08-08 00:51–03:45 UTC, $40.91. Integrity gate (re-verified at
scoring): 400/400 terminal rows, 0 errors, 0 attach-dirty, 0 manifest
mismatches, 0 zero-tool rows, 0 turn-cap violations; per-arm condition
hashes uniform and equal to `conditions.json`. 46 rows hit the
mechanical cap (E′ 18, X 22, J 0, D′ 6) — valid terminal rows scored on
their extracted output per prereg §4.2.

**Scoring-phase incident (defect-4 class, repaired).** The fresh-pin
REPL server died mid-pass while typechecking arm D′ — during
`fresh_052`'s check, whose first verdict recorded the REPL dying — and
the 44 subsequent produced rows silently fell back to single-shot
bare-environment checks (sub-second failures citing unknown
identifiers) — the exact silent-fallback mode the v3 report's §3.4
defect 4 documents. Caught by an elapsed-time audit (all legitimate
server verdicts take ≥3 s; the other three arms' minima are 3.0–9.8 s),
repaired by re-typechecking every produced D′ row against a restarted,
identity-gated server: all 48 healthy-window verdicts reproduced
exactly; 23 of the 45 affected verdicts flipped, all False→True
(`fresh_052` among them). Full cell-level provenance:
`factorial_scored.json` → `retc_provenance`.

### S15.2 Primary endpoint: grounded typecheck (repaired oracle)

| arm | cell | k/100 | rate | Wilson 95% | raw oracle | typecheck ok | produced |
|---|---|---|---|---|---|---|---|
| E′ | join−, ver− | 31 | .310 | [.228, .406] | 26 | 33 | 82 |
| X | join−, ver+ | 41 | .410 | [.319, .508] | 31 | 42 | 78 |
| J | join+, ver− | 39 | .390 | [.300, .488] | 36 | 39 | 100 |
| D′ | join+, ver+ | 39 | .390 | [.300, .488] | 39 | 40 | 93 |

Preregistered effects (commit-clustered paired bootstrap, 44 clusters,
B=10,000, seed 20260803, `cluster_boot_rd` verbatim; α=.05 two-sided):

| effect | register | RD | 95% CI | p | verdict |
|---|---|---|---|---|---|
| JOIN main | confirmatory | +0.030 | [−0.093, +0.139] | 0.645 | null |
| VERIFIER main | confirmatory | +0.050 | [−0.029, +0.121] | 0.230 | null |
| interaction | exploratory | −0.100 | [−0.245, +0.047] | 0.196 | no detectable interaction |

Six pairwise clustered RDs (supporting descriptives): D′−E′ +0.080
[−0.071, +0.207] p=0.310 · D′−X −0.020 [−0.172, +0.120] p=0.840 ·
D′−J +0.000 [−0.099, +0.080] p=1.000 · X−E′ +0.100 [−0.022, +0.215]
p=0.127 · J−E′ +0.080 [−0.053, +0.202] p=0.259 · J−X −0.020
[−0.155, +0.116] p=0.821.

Raw-oracle sensitivity: rates E′ 26 / X 31 / J 36 / D′ 39; join +0.090
[−0.006, +0.183] p=0.076; verifier +0.040 [−0.030, +0.103] p=0.298;
interaction −0.020 [−0.156, +0.134] p=0.832. The raw instrument
flatters the join arms (they copy fully-qualified names from tool
payloads; §4.3's citation-style artifact), and the repair removes
exactly that artifact — the preregistered primary is the repaired row.

### S15.3 Secondary: run-level repaired hallucination (lower better)

Rates: E′ 9/100 · X 5/100 · J 11/100 · D′ 10/100. Effects: join +0.035
[−0.026, +0.100] p=0.289; verifier −0.025 [−0.081, +0.025] p=0.381;
interaction +0.030 [−0.082, +0.142] p=0.666. With formal tools of any
kind present, flagged citations no longer separate arms.

### S15.4 Secondaries: judge evaluated-equivalence and the conjunction

Graded 2026-08-18, after a same-day authentication delay (the judging
CLI's Max OAuth had expired; interactive re-login, then the staged
pass ran unchanged): `bench/analysis/judge_factorial_run.py`, protocol
identical to the v3 fresh-set judge pass (verbatim
`judge_bridge.PROMPT`, gold with binders, arm-substring blinding scan
green over all 353 gradeable outputs, no-tools claude-sonnet-5 from an
empty scratch cwd, `--max-turns 1`, concurrency 3, 429 wait-retry);
the 47 no-output rows pre-decided not-equivalent by definition.
400/400 verdicts, 0 judge errors (three transient max-turns failures
re-graded cleanly); judge cost $52.32. Self-consistency on the fixed
40-item seed-stratified re-grade (seed 20260727, 10/arm): evaluated
agreement 100.0%, strict agreement 97.5% (one flip). Both endpoints
are preregistered-exploratory; the judge remains uncalibrated (§4.2
caveats apply unchanged).

Evaluated equivalence:

| arm | evaluated | Wilson 95% | strict | exposed (n=51) | unexposed (n=49) |
|---|---|---|---|---|---|
| E′ | 46/100 | [36.6, 55.7] | 39/100 | 30/51 (.588) | 16/49 (.327) |
| X | 42/100 | [32.8, 51.8] | 36/100 | 34/51 (.667) | 8/49 (.163) |
| J | 20/100 | [13.3, 28.9] | 13/100 | 9/51 (.176) | 11/49 (.224) |
| D′ | 15/100 | [9.3, 23.3] | 12/100 | 7/51 (.137) | 8/49 (.163) |

Factorial effects on evaluated (same clustered machinery): join
**−0.265** [−0.368, −0.154] p=0.0002; verifier −0.045
[−0.117, +0.029] p=0.264; interaction −0.010 [−0.152, +0.117]
p=0.915. This is §4.2's contamination-by-endpoint inversion
reproduced under the factorial's yoked interface: the unjoined arms
hold source grep over the checkout containing 51 of the 100 golds and
transcribe — their judged-equivalence advantage concentrates on the
exposed half and largely vanishes off it — while the join arms'
generate-and-verify outputs are grounded but less often judged
equivalent.

Conjunction (grounded typecheck ∧ evaluated, the closest
faithful@budget analogue): E′ 20 / X 25 / J 17 / D′ 14 per 100; join
−0.070 [−0.161, +0.021] p=0.141; verifier +0.010 [−0.053, +0.065]
p=0.822; interaction −0.080 [−0.176, +0.011] p=0.108 — parity: no
factor moves the conjunction detectably in either direction.

### S15.5 Manipulation checks, descriptives, sensitivity cuts

Verifier usage: X 213 `decl_exists` calls across 94/100 runs; D′ 536
across 92/100. Informal-tool touches: E′ 2/100 runs, X 4/100 — the
unjoined arms again barely open the informal corpus (Tier-1's E
manipulation failure, reproduced). Mean assistant turns: E′ 20.4 /
X 21.4 / J 17.1 / D′ 17.4; mean tool calls 30.4 / 30.3 / 26.8 / 26.7.
Production: E′ 82 / X 78 / J 100 / D′ 93 of 100 — the join arms finish
inside the 30-turn budget; the unjoined arms cap out (18 and 22 capped
rows). The factorial's rates are therefore not comparable to the
Tier-1 five-arm table (§4.1): the yoked interface (empty built-ins +
mechanical cap + interleaving) is stricter, and D′ lands at 39% where
Tier-1's D scored 48%.

Sensitivity cuts (primary outcome, exploratory; same machinery):

| cut | E′ | X | J | D′ | join RD [CI] p | verifier RD [CI] p |
|---|---|---|---|---|---|---|
| own-module exposed (n=51) | 17 | 25 | 20 | 18 | −0.039 [−0.205, +0.108] 0.653 | +0.059 [−0.060, +0.170] 0.391 |
| unexposed (n=49) | 14 | 16 | 19 | 21 | +0.102 [−0.076, +0.243] 0.271 | +0.041 [−0.054, +0.130] 0.438 |
| drop 3 live-index leaks (n=97) | 31 | 39 | 38 | 38 | +0.031 [−0.093, +0.142] 0.632 | +0.041 [−0.039, +0.114] 0.353 |
| both-annotator determinate (n=74) | 25 | 35 | 30 | 30 | +0.000 [−0.156, +0.139] 1.000 | +0.068 [−0.013, +0.151] 0.110 |

Every cut preserves both nulls. The exposed/unexposed join reversal
(−0.04 vs +0.10, both null) is the direction §4.2's contamination
analysis predicts — the unjoined arms benefit where golds are
retrievable — but neither stratum reaches significance.

### S15.6 Recomputation

`python3 bench/analysis/factorial_scored.py` (score → retc-arm Dp →
judge-summary) then `python3 bench/analysis/factorial_analysis.py`;
runner `bench/factorial/run_factorial.py`; prereg-time live-index
census `bench/factorial/live_declexists_census.json` (3/100 golds
resolvable in the live Brain index at prereg time — the drop-3
sensitivity row above).
