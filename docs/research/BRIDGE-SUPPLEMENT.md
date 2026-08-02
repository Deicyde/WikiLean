# Bridge study — supplement (v3)

**Companion to `docs/research/BRIDGE-REPORT.md` (version 3, 2026-08-02).**
Everything the main paper's structure moved out lives here: the complete
preregistration execution inventory, raw-instrument tables and outage
bases, instrument-validation detail, exposure strata, sensitivity
analyses, the full tool-call census, the WF manual, review responses,
the changelog, and data availability. Section numbers §Sn are cited from
the main paper. Nothing here is new evidence; every table is
recomputable from the file named beside it.

## S1. Complete preregistration execution inventory

Every preregistered component, its status, and the inferential
consequence. Sources: the preregistration
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
| 30-turn budget | **modified** | advisory only; overruns C 50/D 38/E 32, max 88 | budget uncontrolled; §S5 sensitivity |
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
| Tier 2 as specified (FATE-H + MPR-Prop proving, reflection loop) | **not executed** | replaced by exploratory v2: QR/MPR retrieval + one-shot SorryDB, arms N/F/W/WF(+U) | main §§5–6 exploratory |
| PutnamBench | **not executed** | — | — |
| Tier 3a offline Erdős set | **not executed** | — | — |
| Tier 3b live Erdős queue | **not executed** | — | — |

## S2. Tier-1: raw-instrument tables and analysis bases

Source: `bench/analysis/tier1_reanalysis.{py,json,md}`,
`part1_fresh100_v2.{json,md}`, `fresh_clustered.{py,json,md}`,
`success_repaired.{py,json,md}`. Grading pins: eval-341 on Lean
v4.32.0-rc1 / Mathlib `a33a5ccd`; fresh-100 on Lean v4.33.0-rc1 /
Mathlib `9944fe29`; the checkout C/E's file tools read is `61a5e4f338`
(content ~2026-07-10); ProofNet# source pin
`PAug/ProofNetSharp @ a8da405f` (MIT).

### S2.1 The three arm-E bases (raw instrument)

E's 31 infrastructure-dead rows (fresh_069–099, contiguous session-limit
429s; A–D verified zero errors) can be counted three ways. Cells are
grounded typecheck, n/N = % [Wilson 95% CI], **raw oracle**:

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

100 tasks = **44 distinct source commits**, **57 files**, **59
name-stem families** (24 multi-member). Largest commit clusters:
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

Source: `bench/analysis/halluc_validation.{py,json,md}`; blinded
intermediates in `bench/analysis/halluc_blind/`.

**Blinded protocol.** Seeded (20260801) stratified sample of 60 distinct
cited names over arm × oracle-verdict strata (per arm: 6 flagged
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
rows excluded) precision 18.2% [7.3, 38.5]. The oracle's *exists*
verdicts were 30/30 correct.

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
D-vs-C p=0.302, D-vs-A p=1.23e-7, D-vs-B p=3.9e-5, E-vs-C p=1.0. The
R5 column is the citation-style artifact quantified: C/E write
namespace-short names (65/71 reclassifications), D copies
fully-qualified names out of tool payloads (10).

## S4. Fresh-set exposure strata (full)

Source: `bench/analysis/fresh_exposure.{py,json,md}`. Pinned tree
`61a5e4f338` (content ~2026-07-10), the rev C/E's file tools read.
Outcomes below are the **snapshot basis** (E's 429 rows count as
failures; `E attempted-only` excludes them) on the **raw** instrument —
this analysis predates the repair and is retained as measured.

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
| E | 12/51 = 23.5% [14.0, 36.8] | 4/49 = 8.2% [3.2, 19.2] |
| E attempted-only | 12/44 = 27.3% [16.4, 41.9] | 4/25 = 16.0% [6.4, 34.6] |

McNemar by stratum: D-vs-E exposed 15/3 p=0.0075, unexposed 17/3
p=0.0026; D-vs-C exposed 18/5 p=0.0106, unexposed 10/6 p=0.454. The
leak's direction favors C/E, and D's edge over E is strongest exactly
where there was nothing to leak; D-over-C, by contrast, concentrates in
the exposed stratum and is a null unexposed. The merge-date split
(before/after pin, n=49/51) reproduces the same pattern within a point
(`fresh_exposure.md` §"Split by merge date"). The judge-endpoint
exposure split is §S11.4.

## S5. Turn-budget sensitivity

Source: `tier1_reanalysis.md` §5. Restricted to pairs where both arms
stayed within the advisory 30 turns: D-vs-E n=45, 46.7% [32.9, 60.9] vs
24.4% [14.2, 38.7], 16 discordant, p=0.021 — but that counts E's 429
rows (turns=1) as within-budget failures; the completed-only version is
underpowered (n=27, 44.4% [27.6, 62.7] vs 40.7% [24.5, 59.3], 7
discordant, p=1.0). D-vs-C within budget: n=35, 54.3% [38.2, 69.5] vs
25.7% [14.2, 42.1], 12 discordant, p=0.0063. D-vs-A: n=62, 46.8%
[34.9, 59.0] vs 25.8% [16.6, 37.9], 25 discordant, p=0.0146. Consistent
in direction; for D-vs-E, inconclusive once both restrictions apply.
Per-arm within-budget run counts: A 100, B 100, C 50, D 62, E 68.

## S6. Tool-call census, cost/latency, and figure 5

**Tier-1 fresh trace attribution** (`bench/trace_analysis.py`,
eval-split traces): 98–100% of traced tool-arm runs cite at least one
declaration that surfaced in a tool result; 35% of arm-D eval runs cite
a declaration from a `brain_bridge`/`brain_transfer` result. Among
*hallucinated* citations (raw oracle), the checked-and-cited-anyway
rate is 35% (C), 30% (E), 13% (D).

**v2 census, pooled QR+MPR** (figure: `figures/fig5_tooluse.pdf` in the
typeset build, telemetry unchanged by the corrections): F averages 2.2
calls/run; W 3.5 (decl_exists 1,251 + brain_bridge 608 —
verify-then-cite made mechanical); WF 4.6 in a genuine dual-toolkit mix
(decl_grep ~1.5k, decl_exists ~0.9k, brain_bridge ~0.7k, decl_read
~0.5k, loogle ~0.4k). Under the manual, `brain_cell` misuse (63.3%
failure over 482 W-arm calls) falls to a single call in WF. Same-eval-set
caveat as the manual (§S8).

**Per-query cost/latency (QR-810)** (`retrieval_repair.md` §3; CLI cost
estimates under Max auth):

| system | mean $/query | mean wall s | median wall s | mean calls | mean turns |
|---|---|---|---|---|---|
| N | 0.085 | 15.1 | 8.9 | 0.0 | 1.0 |
| F | 0.133 | 13.9 | 10.8 | 2.2 | 3.2 |
| W | 0.198 | 23.7 | 11.2 | 3.5 | 4.3 |
| WF | 0.207 | 15.2 | 12.5 | 4.2 | 5.2 |
| retriever-mode wikibrain | ~0 | — | — | 1 | — |

**MPR:** N $0.185/50.7 s/0 calls · F $0.329/42.2 s/6.0 · W $0.438/59.9
s/9.9 · WF $0.418/38.2 s/9.3.

**N format-repair detail** (`retrieval_repair.md`): symmetric tiered
extraction (relaxed JSON → backticked names → bare dotted identifiers →
compound tokens) over all assistant text, identically for every arm's
empty rows. QR: N 143 empty rows → 104 repaired ≥1 name but only 12
recover the gold; strict 0.6333 → lenient 0.6481; oracle ceiling
0.8099. F/W/WF have 2/5/1 empty rows and move ≤0.0012. The five W
empties are 420 s hard timeouts, not format failures. MPR: N 15 empty →
1 gold recovered (0.2025 → 0.2062). Per-tool provenance detail
(surfaced-by-tool, first-written-in, per-style tables):
`bench/analysis/retrieval_provenance.md`.

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

*(End of verbatim manual.)*

## S9. SorryDB: full bookkeeping

n=171 frozen tasks/arm across 10 repositories; all 203 candidate
verdicts kernel-graded (commit `9682b3b1`; the 8 curve25519 stragglers
verified as 0 proved).

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

Source: `bench/analysis/judge_fresh_run.py` /
`judge_fresh_summary.{py,json,md}`. Judge `claude-sonnet-5`; blind
(informal statement + gold with binders + candidate; no arm identity,
no tools, empty cwd); 500/500 rows, 0 errors; no-output rows auto-fail.
**Uncalibrated; exploratory.** The conjunction below uses the
raw-instrument grounded-typecheck leg (the judge ran before the oracle
repair).

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

Conjunction (grounded-typecheck ∧ evaluated): A 8/100 [4.1, 15.0] ·
B 9/100 [4.8, 16.2] · C 16/100 [10.1, 24.4] · D 15/100 [9.3, 23.3] ·
E 18/100 [11.7, 26.7].

### S11.2 McNemar, fresh-100

Evaluated equivalence: D-vs-E 1/35 p=1.1e-9 · D-vs-C 3/35 p=6.7e-8 ·
D-vs-A 7/5 p=0.774 · E-vs-A 37/1 p=2.8e-10 · E-vs-B 39/2 p=7.8e-10 ·
E-vs-C 13/11 p=0.839.

Conjunction: D-vs-E 6/9 p=0.607 · D-vs-C 8/9 p=1.0 · D-vs-A 9/2
p=0.065 · E-vs-A 11/1 p=0.0064 · E-vs-B 12/3 p=0.035 · E-vs-C 6/4
p=0.754.

### S11.3 Completed-69 continuity

Evaluated: A 10/69 (.145) · B 9/69 (.130) · C 37/69 (.536) · D 11/69
(.159) · E 37/69 (.536); D-vs-E 0/26 p=3.0e-8; conjunction: C 8/69
(.116) · D 10/69 (.145) · E 9/69 (.130), D-vs-E 4/3 p=1.0, D-vs-A 7/0
p=0.016.

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
| 2 | D vs E does not isolate the join | "join carries the effect" retired; bundled-condition language; 2×2 factorial on the roadmap |
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
| 4 | Factorial ablation | not yet run; bare-union U in flight ([[SLOT:UNION]]); paper reframed around the bundled intervention and the tools-vs-none result per the review's conditional |
| 5 | Characterize the Brain; retrieval provenance decomposition | done (`brain_artifact.py`, main §3.1; `retrieval_provenance.py`, main §5) |
| 6 | Real related work (CRAMF, Aria, +) | done; every citation verified against arXiv on 2026-08-01 (`docs/research/review/related_work_notes.md`); comparison table in main §2 |
| 7 | Correct two overclaims (memorization; "clean verifier finding") | memorization → benchmark-familiarity language (main §4.1); verifier finding restated after blinded oracle validation — between-tool contrast dissolves under the repaired instrument (main §4.3), DDR cited as convergent evidence |
| 8 | Repair the retrieval evaluation | symmetric lenient extraction for every arm, cost/calls columns, retriever-vs-agent blocks, Brain-gold coverage, provenance decomposition (main §5, §S6); U pending; WF label kept |
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
  content); N format-failure repair; verified related work; cost
  columns; conflict/AI-use disclosures. Still open: the U arm
  ([[SLOT:UNION]]), human grading of the 36-task queue, judge
  calibration, the 2×2 factorial.

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
repaired-oracle Tier-1 recompute (`success_repaired`) · `834a130a`
cold-start-race condemnation + staggered launches · `2a9f6b91` REPL
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
| `bench/analysis/judge_fresh_run.py`, `judge_fresh/`, `judge_fresh_summary.*` | blind judge pass (500 gradings + re-grade) |
| `bench/analysis/retrieval_repair.*`, `retrieval_provenance.*`, `brain_artifact.*` | v3: format repair; provenance decomposition; artifact characterization |
| `bench/analysis/union_ablation.py` | U-arm analysis (pending [[SLOT:UNION]]) |
| `bench/v2/` + `bench/v2/data/` + `bench/v2/runs/` | v2 harness, AGENT_MANUAL.md, MathlibQR/MPR (CC BY 4.0), SorryDB freeze, all v2 run rows with gzipped stream transcripts, verify.jsonl (203 verdicts) |

**Recomputation entry points.** Tier-1 grading `bench/score_bridge.py`;
repaired instrument `bench/analysis/halluc_validation.py` (`adjusted`)
+ `success_repaired.py`; clustered inference `fresh_clustered.py`;
exposure `fresh_exposure.py`; judge `judge_fresh_summary.py`; retrieval
`bench/v2/score_retrieval.py` + `retrieval_clustered.py` +
`retrieval_repair.py` + `retrieval_provenance.py`; artifact
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
