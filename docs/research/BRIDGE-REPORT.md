# The Bridge Experiment: does an informal↔formal join move end-task Lean performance?

**Report, version 2 — 2026-07-31. Post-review corrected edition.**
Jack McCarthy (WikiLean), with experiments executed by Claude Code agents under
direction. Preregistration: `docs/research/BRIDGE-EXPERIMENT.md`, commit
`0d36f2664ab2ebb2c7b80b350d3c9bd820414335` (2026-07-16). Version 1 of this report
(2026-07-25) received an external methodological review on 2026-07-27
(`docs/research/review/REVIEW-1.md`); we verified its factual claims
independently (`docs/research/review/verification-of-review-1.json`: 29
confirmed, 1 partial, 1 refuted) and this edition incorporates the corrections.
Version 1 remains in git history. All data, code, and run transcripts are
preserved in the private repository `Deicyde/wikilean-bridge-experiment`
(file map in §11). Every number below is recomputable from a file in that
repository; the file is named where each table appears.

**How to read this report.** Every result carries one of three registers,
marked at the section or table where it appears:

- **[PREREGISTERED-MODIFIED]** — a preregistered analysis executed with a named
  modification (the modification is stated where the tag appears).
- **[EXPLORATORY]** — not preregistered: all of Bridge v2 (retrieval, SorryDB,
  tool-use analysis) and the uncalibrated-judge grading.
- **[CORRECTIVE]** — a reanalysis finding of this edition (error repair,
  exposure audit, cluster-aware statistics).

**Zero results in this report are cleanly confirmatory** under the
preregistered success criterion: that criterion was D>E on *faithful@budget*
(semantic equivalence to gold ∧ typecheck) and D≤E on tokens-to-solve, and
neither half was ever graded as specified (§3). The uncalibrated-judge section
(§5) is the exploratory substitute for the missing equivalence leg.

---

## 1. Abstract

We test whether a curated dictionary between mathematical concepts and Mathlib
declarations (the WikiLean "Brain") improves language-model performance on
formalization tasks, against controls holding the underlying corpora without
the join. Results fall in three registers: one preregistered-but-modified
comparison, a set of exploratory follow-ups, and the corrective reanalyses
prompted by an external review; nothing here is cleanly confirmatory, because
the preregistered semantic-equivalence endpoint was never graded as specified.
Our primary metric is therefore the **grounded typecheck rate** — produced
declaration, zero hallucinated citations, passing typecheck — which does not
measure semantic correctness. On 100 post-Brain-index tasks (held out from the
Brain's indexes, but not — we later verified — from the formal-search arms'
sources), the bundled Wikibrain arm reaches 40.6% against 23.2% for the
unjoined-tools control on the 69 completed pairs (McNemar 18/6, exact p=0.023,
risk difference +17.4pp [+4.1, +30.7]); after repairing the control's 31
infrastructure-failed runs, the full-100 contrast is 42.0% vs 30.0% (p=0.073,
RD +12.0pp [+0.2, +23.8]). Join-specific attribution awaits a factorial
ablation; the clean independent finding is the existence verifier, which cuts
hallucinated citations to 6.8% against 17.7–21.2% in every other arm.
Exploratory results: a post-hoc, benchmark-informed union-plus-manual agent
obtained the highest rows we observed on MathlibQR-810 and MathlibMPR
(declaration-clustered CIs exclude the single-arm baselines); on SorryDB,
kernel-verified proving with and without the Brain is statistically
indistinguishable (10 vs 9 of 171, p=1.0).

## 2. The system under test: the WikiLean Brain

The artifact everything below measures is the WikiLean "Brain": a curated
knowledge graph over mathematics that joins Wikipedia/Wikidata concepts,
Mathlib4 declarations, and the external mathematical databases (nLab,
LMFDB, the Stacks project, ProofWiki, and others) into one map. Its atom is
a *cell*: when a Lean declaration, a Wikidata concept, an encyclopedia
article, and a database page all denote the same mathematical object, they
are *organs* of one cell rather than separate nodes — Mathlib's `Module`,
Wikidata's "module" *and* its "vector space" are a single atom, because
Mathlib has no `VectorSpace`; `Module` generalizes it. Mathlib's folder
hierarchy supplies the containment altitude, and everything known about a
cell (Lean source, descriptions, licensed database snippets) is embedded on
the cell itself. About 12,000 of Mathlib's declarations are concept-joined
this way; the Brain is a map of Mathlib, not the territory.

Agents reach it through an MCP server (`POST /mcp` on the live site)
exposing eight tools. `brain_bridge` is the informal→formal entry point:
free text in, existence-verified declarations out, each with its signature,
import line, and bond quality. `brain_search` resolves labels and aliases
to atoms; `brain_cell` returns the full atom card; `brain_transfer` makes
the one-atom informal↔formal jump; `brain_neighborhood` walks the synapse
graph; `brain_snippets` and `brain_filter` serve stored content and facet
queries. The eighth, `decl_exists`, is different in kind: a batch existence
check against the full declaration index — all of Mathlib, not just the
joined subset — built to be called on every name an agent is about to cite.
It matters below that `decl_exists` queries (effectively) the same doc-gen4
oracle our scorer uses to grade hallucinated citations: the bundle contains
a tool aimed at a component of its own score, which is one reason this
report no longer attributes effects to "the join" (§3, §9).

Why build such a thing? The working hypothesis behind WikiLean is that
informal mathematics (what things are called and mean) and formal
mathematics (what typechecks) are two corpora whose *join* is the scarce
artifact: a language model already holds both corpora loosely, and the
informal↔formal boundary is precisely where its hallucinations live. If
that is right, then curating the join — names verified, concepts merged,
bonds carrying provenance — should move performance on end tasks, not just
on lookup. Whether it actually does is the question this report tries, and
in this edition only partly manages, to answer.

## 3. Preregistered design and what actually ran

**Hypothesis (operationalized).** Since "necessary" is unprovable, the
preregistration commits to three predictions. P1: an agent with the *joined*
dictionary beats an agent given informal and formal search *separately*. P2:
the bridge solves at lower cost. P3: the bridge lifts grounding (fewer
hallucinated declaration citations), not just compile rate. The preregistered
success criterion: D>E on faithful@budget (p<.05) AND D≤E on tokens-to-solve.

**The five arms.** All five arms share the same model, prompt, and budgets
within each phase; the only thing that differs between them is the tool
manifest (`bench/run_bridge.py`, manifests in `bench/arms/`):

| Arm | Tools | Intended to isolate |
|---|---|---|
| A `no_tools` | none (`--tools ""`) | floor / memorization |
| B `informal` | Wikipedia + nLab search/fetch, no Lean mapping | informal reasoning alone |
| C `formal` | loogle + ripgrep + source read over a Mathlib checkout | the LeanSearch-class status quo |
| D `wikibrain` | the Brain MCP (brain_bridge/search/cell/transfer/neighborhood/snippets/filter + batch decl_exists) | the join **+ the existence verifier** (not separable in this design) |
| E `B+C unjoined` | B's tools AND C's tools, no join | the unjoined control |

Two design facts the review surfaced, now stated up front. First, D is the
only arm holding `decl_exists`, and hallucination-free citation is a conjunct
of the primary metric, so D vs E measures the *bundled Wikibrain package*,
not the join per se (on the 100 fresh tasks D made 715 `decl_exists` call
attempts, 682 successful). Second, arm E failed its manipulation check as an
"actively consulting both corpora" control: across its 100 fresh runs it
touched the informal tools 4 times, against arm B's 345 — E behaved as a
formal-search agent with a larger manifest
(`verification-of-review-1.json`, arm-design section).

**Models per phase.** Tier 1 uses `claude-haiku-4-5` (model id
`claude-haiku-4-5-20251001`) in all five arms. Bridge v2 (retrieval and
proving) uses `claude-sonnet-5` in all agent arms, per a pre-execution
decision (`docs/research/BRIDGE-V2-BENCHMARKS.md`). One model per phase, one
seed per (task, arm): a bridge that only helps some capability band would not
be distinguished, and no run-to-run variance is measured anywhere.

**Grading.** The primary metric of this report is the **grounded typecheck
rate**: the fraction of tasks where the run produced a declaration, cited
zero names absent from a union oracle (doc-gen4 declaration data ∪ verified
renames; extractor documented in `bench/score_bridge.py`), and the
declaration typechecked on the pinned toolchain.¹ It is a syntactic-validity
+ grounding measure. It does **not** establish that the produced statement
formalizes the informal prompt: a declaration can pass all three legs while
being weaker, stronger, vacuous, or about the wrong object. The
preregistered faithful@budget (BEq+ equivalence to gold) was never graded —
the equivalence leg in `score_bridge.py` is a stub returning `None` — and §5
holds the exploratory substitute.

¹ Version 1 called this metric "success (folded)". The rename follows the
external review: a metric with no semantic-equivalence leg should not be
called success.

**Task sets.** Tier 1a is ProofNet#: 371 problems (341 eval + 30
prompt-freezing dev), pinned to `PAug/ProofNetSharp`
@`a8da405fbd1e348a87445c2e562c747b7e26dc8f` (MIT; arXiv:2406.07222), graded
on Lean v4.32.0-rc1 / Mathlib `a33a5ccd`. Tier 1b is the **post-Brain-index
fresh set**²: 100 tasks built from theorems merged into Mathlib master
between 2026-07-03 and 07-16, verified absent from the Brain's declaration
universe and node set — the indexes arm D serves
(`bench/data/fresh_tasks.stats.json`) — and graded on Lean v4.33.0-rc1 /
Mathlib `9944fe29`. The set was **not** verified absent from the
formal-search arms' sources: arms C/E read a mutable local checkout (which
stood at `61a5e4f338`, content through ~2026-07-10, during the runs) and
queried the live unsnapshotted Loogle service. §4.4 measures the resulting
exposure directly. A determinacy pre-screen was preregistered (exclude tasks
two independent formalizers cannot pin down); what ran instead was a
post-hoc double annotation (agreement 79%, κ≈0.20) defining a 74-task
both-determinate subset — no tasks were excluded.

² Version 1 called this set "contamination-proof" and "newer than every
arm's index". Both phrasings are retracted (§9, concern 4).

**Budgets and execution.** Every arm ran under a 10-minute wall clock
(enforced) and a 30-turn budget that was **advisory only** — the prompt
stated it, but the CLI passed no turn cap. Fresh-set overruns: C 50 runs,
D 38, E 32 exceeded 30 turns, with maxima 80, 88, and 72. The fresh arms
also ran in strictly sequential blocks on 2026-07-19 (A 01:50–02:08Z,
B 02:09–03:56Z, C 03:58–04:27Z, D 04:29–04:54Z, E 04:56–05:17Z) — no
interleaving, so arm is confounded with time of execution. Agents had no
in-loop typechecker and were instructed to verify cited names through their
tools.

**The arm-E 429 incident and repair [CORRECTIVE].** Arm E's fresh block died
at its tail: rows fresh_069–099 (31 contiguous tasks) all errored with
session-limit 429s — an infrastructure failure at the end of the sequential
E block, not agent behavior. Version 1 scored these as "produced no
declaration" and read them as evidence that unjoined tool volume is harmful;
that reading is retired. On 2026-07-27 the 31 rows were rerun
(`bench/analysis/rerun_E_fresh.py`) with E's exact July-19 code path, on a
read-only `git archive` extraction of `61a5e4f338` — the same tree content
the original C/E runs saw. The repair surfaced a second condition-fidelity
hazard worth recording: under concurrent cold starts, the CLI's stdio MCP
servers can silently fail to connect, yielding runs that *look* like
completions but had zero tools. The repair driver detects this from the
stream-json init event and forces such rows to error-and-retry rather than
recording fake tool-less completions (commit `19a90209`). Originals are
archived in `bench/data/runs_E_fresh_429_archive/`; 14 of the 31 repaired
rows became grounded-typecheck passes
(`bench/analysis/bridge_summary_v2.json`, `v2_provenance`).

**The complete execution table.** Every preregistered component, its status,
and the inferential consequence. (Sources: the preregistration; the
claim-by-claim inventory in `verification-of-review-1.json`, prereg section.)

| Preregistered component | Status | What actually happened | Inferential consequence |
|---|---|---|---|
| Five arms A–E, identical model/prompt/budget, tools-only difference | executed | 471 rows/arm (`bench/data/runs/`) | design intact |
| Per-tool call logging | executed | `tool_calls_by_name` every row; full traces eval C/D/E + fresh (deviation 7) | enables §4.5 attribution |
| Tier 1a ProofNet# (371) | executed | 371/arm, scored | contaminated by construction (arm A 59.8% on eval-341); paired deltas only |
| Tier 1b fresh set (~100) | executed | 100 tasks | holdout guarantee narrower than claimed (Brain indexes only) — renamed post-Brain-index |
| Hallucinated-decl rate | executed | all tables | the cleanest surviving mechanism signal |
| McNemar D-vs-E / D-vs-C + discordant tables | executed | §4 | run on grounded typecheck, not the preregistered endpoint |
| 10-min wall clock | executed | 600 s timeouts | — |
| Pre-campaign API requirements 1–8 | executed | implemented before campaign | — |
| 30-turn budget | **modified** | advisory only; overruns C 50/D 38/E 32, max 88 | budget is uncontrolled; §4.3 sensitivity analysis |
| Determinacy pre-screen (exclude) | **modified** | post-hoc dual annotation (79%, κ≈0.20); 74-task subset, none excluded | subset analysis, not screened population |
| Arm-D production server | **modified** | production Worker code served locally over shipped shards (deviation 2) | none material |
| Single-shot elaboration grading | **modified** | persistent REPL, same pins (deviation 3) | none material |
| ≤4 typecheck calls in-loop | **modified** | no in-loop typechecking at all (deviation 4) | halluc rate is a pure grounding measure |
| Skill/ToolSearch leak | **modified** | sealed before eval; 120 dev runs quarantined (deviation 6) | dev split discarded, eval clean |
| Per-arm cost distributions | **modified** | means only (USD + wall-clock) | descriptive |
| faithful@budget (BEq+ equivalence) | **not executed** | grader is a stub; zero judge files in campaign | primary endpoint missing; no confirmatory claim possible |
| LLM judge + 50-item human calibration | **not executed** (campaign) | harness existed unused; an *uncalibrated* blind judge pass was launched post-review (§5) | §5 is exploratory only |
| tokens-to-solve (half the success criterion) | **not executed** | never computed (tokens recorded per row) | P2 untested as specified |
| 3 reseeds + pass@k curves | **not executed** | one run per (task, arm) everywhere | no variance estimate; single-seed caveat on every number |
| Second model class on primary set | **not executed** | Tier-1 all-Haiku; v2 all-Sonnet on different benchmarks | capability-band generality unknown |
| Preregistered success criterion | **not executed** | neither half graded as specified | zero confirmatory results |
| Tier 2 as specified (FATE-H + MPR-Prop proving, reflection loop, arms A/C/D/E) | **not executed** | replaced by exploratory v2: QR/MPR retrieval + one-shot SorryDB, arms N/F/W/WF | §§6–7 are exploratory, not the preregistered Tier 2 |
| PutnamBench | **not executed** | — | — |
| Tier 3a offline Erdős set | **not executed** | — | — |
| Tier 3b live Erdős queue | **not executed** | — | — |

## 4. Tier 1: statement autoformalization (claude-haiku-4-5)

**[PREREGISTERED-MODIFIED]** — the preregistered paired design, graded on
grounded typecheck rate instead of faithful@budget, one model, one seed,
advisory turn budget. Corrected tables below are from
`bench/analysis/tier1_reanalysis.{json,md}` and
`bench/analysis/part1_fresh100_v2.{json→bridge_summary_v2.json,md}`; all
rates carry Wilson 95% CIs.

### 4.1 Tier 1a — ProofNet# eval split (n=341/arm)

| arm | grounded typecheck | Wilson 95% CI |
|---|---|---|
| A none | 204/341 = 59.8% | [54.5, 64.9] |
| B wiki | 198/341 = 58.1% | [52.8, 63.2] |
| C formal | 218/341 = 63.9% | [58.7, 68.9] |
| D wikibrain | 219/341 = 64.2% | [59.0, 69.1] |
| E B+C unjoined | 208/341 = 61.0% | [55.7, 66.0] |

The arms sit within a few points of each other; the v1 D-vs-E McNemar
(63 vs 51 discordant over the 371 folded pairs, exact p=0.30) is an
underpowered null. Arm A at ~60% with zero tools is substantial ProofNet
memorization and was the motivation for Tier 1b. The typecheck-only row
still *anti-correlates* with grounding (E led typecheck at 83.3% while
trailing on hallucinations in the v1 tables) — reproducing TheoremGraph's
typecheck-is-not-a-signal finding at 15× their n. Tier-1a is contaminated by
construction; we report it for the paired deltas and as the memorization
control, never alone.

### 4.2 Tier 1b — the post-Brain-index fresh set (n=100/arm)

Three analyses, in order of increasing completeness. First, the campaign
as it actually ran, with E's 31 infrastructure-dead rows counted as failures
(intention-to-treat over the snapshot; `tier1_reanalysis.md` §2):

| arm | grounded typecheck | Wilson 95% CI | infra errors |
|---|---|---|---|
| A | 20/100 = 20.0% | [13.3, 28.9] | 0 |
| B | 22/100 = 22.0% | [15.0, 31.1] | 0 |
| C | 25/100 = 25.0% | [17.5, 34.3] | 0 |
| D | 42/100 = 42.0% | [32.8, 51.8] | 0 |
| E | 16/100 = 16.0% | [10.1, 24.4] | **31** |

E's row conflates agent behavior with a rate-limit outage; the D-vs-E
McNemar on this table is the error-inflated figure v1 headlined, and it now
appears only in §9. The two contrasts *unaffected* by the outage (their arms
had zero errors): **D vs C 28/11 discordant, exact p = 0.0095; D vs A 29/7,
p = 0.0003** (100 pairs each).

Second, the completed-pairs analysis — the 69 tasks arm E actually ran —
which is this report's headline Tier-1 contrast
(`tier1_reanalysis.md` §3, §6):

| arm | grounded typecheck (n=69) | Wilson 95% CI |
|---|---|---|
| A | 10/69 = 14.5% | [8.1, 24.7] |
| B | 12/69 = 17.4% | [10.2, 28.0] |
| C | 12/69 = 17.4% | [10.2, 28.0] |
| D | **28/69 = 40.6%** | [29.8, 52.4] |
| E | 16/69 = 23.2% | [14.8, 34.4] |

**D vs E: McNemar 18/6 discordant, exact p = 0.023; risk difference +17.4pp,
95% CI [+4.1, +30.7]** (paired Wald). Also D vs C 21/5, p=0.0025; D vs A
22/4, p=0.0005 on these pairs.

Third, the post-repair full 100 — all 500 fresh rows completed, the 31
repaired E rows scored with the identical pipeline
(`bench/analysis/part1_fresh100_v2.md`, from `bridge_summary_v2.json`):

| arm | grounded typecheck (n=100) | Wilson 95% CI |
|---|---|---|
| A | 20/100 = 20.0% | [13.3, 28.9] |
| B | 22/100 = 22.0% | [15.0, 31.1] |
| C | 25/100 = 25.0% | [17.5, 34.3] |
| D | **42/100 = 42.0%** | [32.8, 51.8] |
| E | 30/100 = 30.0% | [21.9, 39.6] |

**D vs E post-repair: McNemar 25/13, exact p = 0.073; RD +12.0pp, 95% CI
[+0.2, +23.8].** D vs C (p=0.0095) and D vs A (p=0.0003) are unchanged —
only E's cells moved. Stated plainly: with the infrastructure failures
repaired, the bundled-Wikibrain-vs-unjoined contrast on the full set is a
directionally consistent but marginal effect (the RD interval barely
excludes zero; the McNemar p does not reach 0.05); the completed-pairs
contrast is significant. And E, repaired, is an unremarkable mid-pack arm
(30%, statistically indistinguishable from C, p=0.42): version 1's "unjoined
tool volume can be actively harmful" was an infrastructure artifact and is
retired.

Arm A's collapse from 59.8% (1a) to 20% here remains the cleanest
memorization quantification in the study.

### 4.3 Turn-budget sensitivity [CORRECTIVE]

The 30-turn budget was advisory (§3), so we recompute restricted to pairs
where **both** arms stayed within 30 turns (snapshot outcomes;
`tier1_reanalysis.md` §5). D vs E: n=45 pairs, D 46.7% [32.9, 60.9] vs E
24.4% [14.2, 38.7], 16 discordant, p = 0.021 — but this error-inclusive
version counts E's 429 rows (turns=1) as within-budget failures. The honest
completed-only version is underpowered: n=27 pairs, D 44.4% [27.6, 62.7] vs
E 40.7% [24.5, 59.3], 7 discordant, p = 1.0. D vs C within budget: n=35,
D 54.3% [38.2, 69.5] vs C 25.7% [14.2, 42.1], 12 discordant, p = 0.0063.
The budget-restricted evidence is consistent in direction and, for D-vs-E,
inconclusive once both restrictions are applied at once.

### 4.4 Exposure strata: what the formal-search leak actually touched [CORRECTIVE]

The review found the fresh golds were not held out from arms C/E's sources.
We measured it (`bench/analysis/fresh_exposure.{json,md}`; read-only
`git archive` of the pinned tree `61a5e4f338`, content date 2026-07-10). Of
the 100 tasks: the gold's full dotted name appears verbatim somewhere in the
tree for **37**; the basename appears as a declaration header anywhere for
**64**; the basename appears as a declaration header **in the task's own
module file** for **51** — the exposure basis used below; and the gold's
commit is an ancestor of the pin for 49.³

³ Exposure is measured on tree bytes, not merge metadata: two tasks
(fresh_037, fresh_054) are exposed despite post-pin added-commits —
fresh_054's full dotted name already sits in its module file at the pin —
which is why 51 exposed ≠ 49 merged-before-pin.

Split outcomes (snapshot basis: E's 429 rows count as failures, so E's
unexposed cell is heavily infrastructure-depressed; the attempted-only rows
exclude them):

Exposed stratum (n=51):

| arm | grounded typecheck | Wilson 95% CI |
|---|---|---|
| A | 9/51 = 17.6% | [9.6, 30.2] |
| B | 8/51 = 15.7% | [8.2, 28.0] |
| C | 11/51 = 21.6% | [12.5, 34.6] |
| D | **24/51 = 47.1%** | [34.1, 60.5] |
| E | 12/51 = 23.5% | [14.0, 36.8] |
| E attempted-only | 12/44 = 27.3% | [16.4, 41.9] |

Unexposed stratum (n=49):

| arm | grounded typecheck | Wilson 95% CI |
|---|---|---|
| A | 11/49 = 22.4% | [13.0, 35.9] |
| B | 14/49 = 28.6% | [17.8, 42.4] |
| C | 14/49 = 28.6% | [17.8, 42.4] |
| D | **18/49 = 36.7%** | [24.7, 50.7] |
| E | 4/49 = 8.2% | [3.2, 19.2] |
| E attempted-only | 4/25 = 16.0% | [6.4, 34.6] |

McNemar by stratum: D vs E exposed 15/3, p=0.0075; **D vs E unexposed 17/3,
p=0.0026**. D vs C exposed 18/5, p=0.0106; D vs C unexposed 10/6, p=0.45.

The reading has two honest halves. Against E, the leak cannot explain D's
edge: the leak's direction favors C/E (direct source access to the golds;
D's Brain indexes genuinely predate every gold), and D's advantage over E is
*strongest exactly where there was nothing to leak* — 36.7% vs 8.2% on
unexposed tasks (with the caveat that E's unexposed denominator is the most
outage-damaged stratum; even attempted-only, 36.7% vs 16.0%). Against C the
picture is weaker: D's edge concentrates in the exposed stratum and is a
null (p=0.45) on unexposed tasks — consistent with D's advantage over pure
formal search depending partly on targets a formal-search agent could in
principle have found in-tree but didn't.

### 4.5 Hallucinated citations and trace attribution

Hallucination rates on the fresh set, all 100 rows per arm (post-repair;
recomputed for this edition from `bench/data/runs/{A..E}/fresh_*.json` with
`score_bridge.py`'s union oracle):

| arm | halluc-decl rate (cited names) | runs w/ ≥1 hallucination | Wilson 95% CI (runs, n=100) |
|---|---|---|---|
| A | 94/443 = 21.2% | 54 | [44.3, 63.4] |
| B | 76/429 = 17.7% | 48 | [38.5, 57.7] |
| C | 108/517 = 20.9% | 49 | [39.4, 58.7] |
| D | **32/472 = 6.8%** | **23** | [15.8, 32.2] |
| E | 107/505 = 21.2% | 49 | [39.4, 58.7] |

The citation-level rates are descriptive (citations cluster within runs);
the run-level proportions carry the CIs. This is the study's most robust
effect: it survives the repair unchanged (D 6.8% vs 17.7–21.2%), it is
larger off-distribution than on (Tier-1a: D 5.9% vs 10.1–11.3%), and the
traces explain its mechanism.

**Trace attribution [EXPLORATORY — eval split only]** (deviation-7 telemetry
covers eval arms C/D/E; `bench/trace_analysis.py`): 98–100% of traced
tool-arm runs cite at least one declaration that visibly surfaced in a tool
result, and only 10–17% of citations never touched the tools. 35% of arm-D
eval runs (116/329) cite a declaration that came out of a
brain_bridge/brain_transfer result — English in, formal name out (an
undercount; result heads truncate at 200 chars). The sharpest signal is
among *hallucinated* citations: the checked-and-cited-anyway rate is 35% for
C and 30% for E but **13% for D** — binary verification disciplines the
model where the fuzzy neighborhoods of loogle and grep let it fool itself.

### 4.6 The existence verifier as an independent finding

We promote this from mechanism-footnote to first-class result, following
the review's own suggestion. What the data support: giving an agent a
**batch, binary, oracle-backed existence check** (plus the instruction to
use it on every name) collapses hallucinated citations by roughly 3× across
two task distributions, and the effect concentrates precisely on the
citations the agent had already "checked" by search. What the data do not
yet support: how much of D's grounded-typecheck edge is this verifier versus
the concept join — D is the only arm holding it, and no-halluc is a conjunct
of the metric. The 2×2 factorial (join × decl_exists; roadmap §10.1) is the
experiment that separates them. Note the circularity honestly: `decl_exists`
and the scoring oracle draw on the same doc-gen4 index, so "fewer
hallucinations" partly means "the arm could query the grader's oracle" —
which is itself the product claim, but must be named.

**Cost (descriptive only).** Campaign means per task, folded over 471
runs/arm as run (pre-repair; E's 31 error rows cost ≈0): A $0.034, B $0.048,
C $0.140, D $0.121, E $0.128; mean wall-clock C 116 s, D 89 s, E 106 s. The
preregistered tokens-to-solve test was never computed, so P2 remains
untested; v1's "cheaper and better" framing is withdrawn to this
descriptive note.

## 5. Semantic faithfulness (uncalibrated judge) [EXPLORATORY]

The preregistered primary endpoint — equivalence to the gold statement —
was never graded during the campaign (§3). Following the review, a blind
LLM-judge pass over all 500 fresh outputs (5 arms × 100 tasks, including
the 31 repaired E rows) was launched on 2026-07-27
(`bench/analysis/judge_fresh_run.py`). Protocol: judge = `claude-sonnet-5`
(deliberately a different, stronger model than the Haiku subjects), no
tools, one turn, prompt containing only {informal statement, gold, produced
declaration}; every rendered prompt is scanned for arm-revealing substrings
and the driver aborts on any hit; no-output rows are judged not-equivalent
by definition; a fixed 50-item seed-stratified subset (seed 20260727, 10 per
arm) is re-graded for self-consistency. **The judge is uncalibrated**: the
preregistered 50-item human calibration remains undone, so every number in
this section is exploratory and none can rescue the missing confirmatory
endpoint.

[[SLOT:JUDGE]] — *to be filled from `bench/analysis/judge_fresh_summary.json`
when the running pass completes (as of this draft, arms A–D are graded and E
is in progress). The tables to land here, per `judge_fresh_summary.py`:*

1. *Per-arm strict and evaluated equivalence rates on fresh-100, Wilson 95%
   CIs.*
2. *The conjunction (grounded typecheck ∧ judge-evaluated-equivalent) per
   arm — the closest available analogue of faithful@budget.*
3. *Exact-binomial McNemar D-vs-E, D-vs-C, D-vs-A on judge-evaluated and on
   the conjunction, full-100 and completed-69.*
4. *Self-consistency: strict/evaluated agreement on the 50-item re-grade,
   plus judge cost/error census.*

One grading-fidelity deviation is pre-declared: for fresh tasks the judge
sees gold_context + gold_formal (the `variable`/`open` binders the statement
needs); this is arm-independent.

## 6. Third-party retrieval: MathlibQR-810 and MathlibMPR (claude-sonnet-5) [EXPLORATORY]

Bridge v2 replaces judge-dependent grading with graders that predate the
arms: third-party expert gold labels here, the Lean kernel in §7. Data:
`frenzymath/LeanSearch-v2` @`94f4888cbaf9` (CC BY 4.0; arXiv:2605.13137),
copied under `bench/v2/data/`. Four arms: N (no tools), F (loogle +
decl_grep + decl_read), W (the Brain MCP), and WF (W ∪ F **plus**
`bench/v2/AGENT_MANUAL.md` prepended to the prompt). Scoring is
`bench/v2/score_retrieval.py`, exact full-name match; run rows with full
gzipped stream-json transcripts in `bench/v2/runs/agent/`.

**WF is a post-hoc, benchmark-informed condition — every WF number below
carries that label.** The timeline (commit times, verified): N/F/W results
committed 22:27 on 07-24; the manual written 00:30 on 07-25; WF results
01:15. The manual distills measurements taken **on the same evaluation
queries WF was then scored on** — its bracketed figures (per-style nDCG
values, the 63% brain_cell failure rate, the 143/810 format failures, per-tool
call counts) are the N/F/W/system-mode eval-set aggregates, reproduced
exactly (`verification-of-review-1.json`, manual-timing section). WF is
development evidence about what a maximally equipped, briefed agent can do —
not an untouched test result, and not a defensible SOTA comparison. Version 1
additionally misattributed the 63% brain_cell figure to "the Tier-1 traces";
it is the v2 W-arm eval figure (482 calls, 63.3% failed). Corrected here and
in §9.

### 6.1 MathlibQR fair-810 — concept retrieval, declaration-clustered [CORRECTIVE statistics]

The 810 query rows are paraphrase styles of only **171 distinct gold
declarations** (2–6 rows each), so v1's row-level McNemars overstated the
effective n. All inference is now at the declaration level
(`bench/analysis/retrieval_clustered.md`; cluster bootstrap B=10,000, seed
20260727):

| system | R@10 (row) | R@10 95% CI (decl-cluster boot) | nDCG@10 | nDCG 95% CI |
|---|---|---|---|---|
| published: TheoremGraph | 0.775 | — | 0.548 | — |
| published: LSv2 retriever+reranker | 0.780 | — | 0.623 | — |
| system-mode wikibrain (one brain_bridge call, no LLM) | 0.036 | — | 0.031 | — |
| agent N | 0.633 | [0.581, 0.684] | 0.598 | [0.547, 0.647] |
| agent F | 0.831 | [0.792, 0.868] | 0.790 | [0.750, 0.829] |
| agent W | 0.816 | [0.767, 0.862] | 0.781 | [0.731, 0.827] |
| agent WF (post-hoc, benchmark-informed) | 0.885 | [0.849, 0.919] | 0.839 | [0.801, 0.874] |

Declaration-level paired contrasts: **F − W is a null** (R@10 +0.015,
95% CI [−0.028, +0.059], Wilcoxon p=0.26; nDCG +0.010 [−0.029, +0.048]) —
the two toolkits are statistically tied, with different textures (W wins the
special_case style, nDCG 0.523 vs 0.384; F wins the Lean-syntax styles; grep
cannot find what source text never names). WF − F: +0.054 [+0.027, +0.082],
Wilcoxon p=0.0017; WF − W: +0.069 [+0.032, +0.108], p=0.0003 — the briefed
union arm beats both single arms even under clustering, with the post-hoc
label attached.⁴

⁴ Version 1's row-level McNemars (n=810 treated as independent: F−W 78/66
discordant p=0.36; WF−F 71/27 p=1.0×10⁻⁵; WF−W 83/27 p=8.3×10⁻⁸) are
**uncorrected** for the declaration clustering — the 144 F/W discordant
rows come from only 76 declarations — and are superseded by the clustered
tests above.

The system-mode row is the headline API deficiency and survives every
correction: one brain_bridge call scores 0.036 because the free-text entry
is a label/alias resolver, not a semantic retriever — while an agent
*iterating* over the same API reaches 0.816. The content is in the graph;
the single-shot entry point is the gap. Agent N's 0.633 includes 143/810
rows scored 0 for format non-compliance.

### 6.2 MathlibMPR — premise retrieval (69 post-cutoff PR theorems)

One task per PR (the review's clustering concern does not apply here — see
§9); task-level bootstrap CIs:

| system | group-recall@10 | 95% CI (task boot) |
|---|---|---|
| published: LeanSearch-v2 reasoning | 0.461 | — |
| published: DIVER | 0.380 | — |
| published: TheoremGraph concept-tuned | 0.165 | — |
| system-mode wikibrain | 0.000 | — |
| agent N | 0.203 | [0.131, 0.282] |
| agent W | 0.272 | [0.196, 0.354] |
| agent F | 0.453 | [0.365, 0.540] |
| agent WF (post-hoc, benchmark-informed) | 0.557 | [0.472, 0.642] |

Two readings. A generic Sonnet agent with grep and loogle **matches the
specialist premise retriever** (0.453 vs 0.461) — a notable baseline on its
own, untouched by any of this study's confounds. And W trails F by 18pp:
the preregistered concept ≠ premise boundary (TheoremGraph's negative
transfer), now measured on our own tools, giving `brain_premises`
(BRIDGE-ISSUES #7) a quantified target. The WF − F contrast is **marginal**:
+0.104, 95% CI [+0.007, +0.201], sign test p=0.029, Wilcoxon p=0.049 — the
interval nearly touches zero, and the post-hoc label applies.

### 6.3 Tool use under the manual

The census (pooled QR+MPR): F averages 2.2 calls/run; W 3.5 (decl_exists
1,251 + brain_bridge 608 — verify-then-cite made mechanical); WF 4.6 in a
genuine dual-toolkit mix (decl_grep ~1.5k, decl_exists ~0.9k, brain_bridge
~0.7k, decl_read ~0.5k, loogle ~0.4k). Under the manual, brain_cell misuse
(63% failure rate in the v2 W-arm runs) falls to a single call in WF. These
are development observations about the same eval set the manual was tuned
on, and are labeled accordingly.

[[SLOT:UNION]] — *the bare-union U arm (W ∪ F tools, no manual;
`MANUAL_ARMS` blanked in `bench/v2/run_agent.py`) separates the union effect
from the briefing effect. Planned table on completion: N / F / W / U / WF on
both benchmarks, declaration-clustered (QR) and task-bootstrap (MPR) CIs,
with U-vs-F and WF-vs-U paired contrasts. Until it lands, "union tools" and
"benchmark-informed manual" remain unseparated in WF.*

## 7. SorryDB: kernel-graded proving in live repositories (claude-sonnet-5) [EXPLORATORY]

SorryDB (arXiv:2603.02668, sorrydb.org) serves unsolved `sorry`s from real
Lean repositories; success is the repo *building* with your proof — no judge
anywhere, nothing to memorize. Snapshot: the SorryDB-2601 1,000-task
evaluation split.

**Subset construction and the snapshot-rot finding.** Disk permitted one
repository build at a time, so we sampled repositories rather than tasks.
The original freeze — 8 repositories, 164 tasks — lost **74 of 164 tasks
(45%)** in the ~six months since the snapshot: pinned commits of
GlimpseOfLean, LeanCourse25, and most Foundation and SemicircleLaw tasks no
longer exist on GitHub (history rewritten; unreachable commits deleted). No
retrieval route worked, and detection is subtle: GitHub's archive endpoint
answers preflight HEAD for dead commits with HTTP 200, so a liveness check
that does not actually fetch reports the pins healthy. Methodological
finding: benchmarks pinned to live repositories rot at the commit level, and
the rot is invisible unless every pin is re-fetched before use. The refreeze
(`bench/v2/sorrydb_prep.py`, commit `db679e8d`) keeps only
verified-fetchable pins: **171 tasks across 10 repositories**.

**Protocol.** Build-free, one-shot: each row carries the goal state plus a
±60-line window at the pinned commit; the agent (600 s, arms N/F/WF as in
§6) outputs one replacement for the `sorry`, with an explicit
honest-abstention rule. Verification (`bench/v2/verify_sorrydb.py`) clones
at the pin, splices, rebuilds the module; success = exit 0 with no
sorry/axiom warning.

**Results — verdict-complete.** All 203 frozen-task candidate proofs now
have kernel verdicts (commit `9682b3b1`; the 8 curve25519 stragglers of v1
verified as 0 proved). Recomputed from `bench/v2/runs/sorrydb/verify.jsonl`
× the run rows, n=171 tasks/arm:

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
| **proved / 171** | 1.2% | 5.3% | 5.8% |
| Wilson 95% CI | [0.3, 4.2] | [2.8, 9.7] | [3.2, 10.4] |
| repo-clustered boot 95% CI | [0.0, 2.7] | [0.0, 10.7] | [0.0, 12.2] |
| proved / candidates | 3.4% | 12.7% | 13.5% |
| total cost (USD) | $58.97 | $102.76 | $108.87 |
| cost per proved | $29.48 | $11.42 | $10.89 |
| mean wall-clock / task | 120 s | 198 s | 183 s |

(3 N and 2 F tasks have no run row — runner losses. Distinct sorries proved
across arms: 13; N's are a strict subset of both F's and WF's; WF∩F 6,
WF-only 4, F-only 3. Proofs concentrate in 3 of the 10 repos: PersistentDecomp,
LeanAPAP, MiscYD.)

**WF vs F is statistically indistinguishable.** The point difference is one
proof (10 vs 9 of 171): task-level exact McNemar 4/3, **p = 1.0**;
repo-clustered bootstrap difference +0.58pp, 95% CI [+0.0, +1.85pp], with
**34% of resamples showing no difference or worse**
(`retrieval_clustered.md` §4). SorryDB provides essentially no evidence
about the Brain: WF routed 94% of its tool calls to formal search here, and
no W-only or bare-union arm was run. What the experiment does show is the
tools-vs-no-tools gap — N burns its budget (70 of 168 rows produce nothing,
mostly 600 s timeouts) while tool arms triple-to-quintuple the prove rate
and double the honest-abstention rate — though with only 10 repo clusters
even that deserves caution. The cost-per-proved row is retained as
bookkeeping; with 2/9/10 successes it is unstable, and v1's "adding the join
never made the task more expensive per success" is deleted (§9). The
published SorryDB agentic best (≈30%) is not comparable: different task
subset (research-frontier-weighted) and materially different protocol
(published agents iterate against the build; ours drafted one-shot).

## 8. What survives and what does not

Each retired v1 claim, paired with its v2 replacement.

| v1 claim (retired) | v2 replacement (supported) |
|---|---|
| "The join carries the effect" (fresh McNemar p<0.0001) | The **bundled Wikibrain condition** outperforms the tested controls: completed-69 D 40.6% vs E 23.2% (p=0.023, RD +17.4pp [+4.1,+30.7]); post-repair full-100 42.0% vs 30.0% (p=0.073, RD +12.0pp [+0.2,+23.8]); D vs C p=0.0095. Join-specific attribution awaits the 2×2 factorial. |
| "42% success" | "42% **grounded typecheck rate**" — no semantic-equivalence leg was graded; §5 is the exploratory substitute. |
| "Contamination-proof / newer than every index" | "**Post-Brain-index**": held out from the Brain's indexes only; 51/100 golds were exposed in arms C/E's checkout at run time. The leak's direction favored C/E, and D's edge over E is strongest on unexposed tasks (36.7% vs 8.2%, p=0.0026) — but D-vs-C is a null on that stratum. |
| "Arm E worst at 16%; unjoined tool volume actively harmful" | E's 31 no-output rows were session-limit 429s. Repaired, E is mid-pack at 30.0%, indistinguishable from C. |
| "WF sets the best rows we know of" | A **post-hoc, benchmark-informed** union+manual condition obtained the highest rows we observed (QR 0.885, MPR 0.557); clustered CIs exclude the single-arm baselines, but the manual was tuned on the same eval queries. |
| "Adding the join never made the task more expensive per success" | Deleted. Cost figures are descriptive; tokens-to-solve was never computed; SorryDB's 10-vs-9 is p=1.0. |
| brain_cell 63% failure "in the Tier-1 traces" | The figure is from the **v2 W-arm eval runs** (482 calls, 63.3% failed). |

What survives cleanly, with its register: the **hallucination collapse**
(D 6.8% vs 17.7–21.2% fresh, post-repair; checked-and-cited-anyway 13% vs
30–35%) and its promotion to an independent existence-verifier finding
[preregistered-modified + exploratory traces]; the **memorization
quantification** (arm A 59.8% → 20.0%) [preregistered-modified]; the
**system-mode gap** (0.036 single-shot vs 0.816 agentic on QR) — an API
deficiency, not a content deficiency [exploratory]; the **F-matches-SOTA
baseline** on MPR (0.453 vs 0.461) and the **concept≠premise boundary**
(W −18pp) [exploratory]; the **snapshot-rot finding** (45% of pinned tasks
unreachable in ~6 months, invisible to HEAD-based liveness checks)
[exploratory]; and the **tools-vs-no-tools proving gap** on SorryDB
[exploratory, 10 clusters].

## 9. Corrections and response to external review 1

The review (`docs/research/review/REVIEW-1.md`, received 2026-07-27) raised
eight concerns. We verified every factual claim against the repo before
acting (`verification-of-review-1.json`); the table gives concern →
verification outcome → action taken in this edition.

| # | Concern | Verification | Action in v2 |
|---|---|---|---|
| 1 | Tier-1 "success" does not measure semantic correctness (equivalence grader is a stub) | CONFIRMED (the "misleading" characterization graded PARTIAL — v1's body disclosed the gap; its abstract did not) | Metric renamed **grounded typecheck rate** everywhere including the abstract; blind uncalibrated-judge pass launched as the exploratory substitute (§5); human calibration on the roadmap |
| 2 | D vs E does not isolate the join (D alone holds decl_exists, which optimizes a component of the score; 715 calls on fresh) | CONFIRMED | "The join carries the effect" retired; claims rewritten to "the bundled condition"; decl_exists promoted to an independent finding (§4.6); 2×2 factorial is roadmap item 1 |
| 3 | Arm E failed its manipulation check (4 informal calls vs B's 345) | CONFIRMED | Disclosed in §3; yoked single-interface control on the roadmap |
| 4 | The fresh set was not isolated from formal-search sources (mutable checkout at `61a5e4f338`; live Loogle unsnapshotted) | CONFIRMED (64/100 basenames in-tree; 51 own-module) | Set renamed **post-Brain-index**; exposure measured per-task and outcomes stratified (§4.4); frozen-snapshot protocol on the roadmap |
| 5 | WF is test-set tuned (manual written after and from the same eval's N/F/W results) | CONFIRMED (commit times 22:27 → 00:30 → 01:15; every bracketed manual figure reproduces from the eval aggregates) | WF labeled **post-hoc, benchmark-informed** at every mention including the abstract; the manual's same-eval-set provenance stated plainly (§6); the v1 misattribution of the 63% brain_cell figure to Tier-1 corrected; bare-union arm slotted; dev-set manual freeze on the roadmap |
| 6 | Statistical independence and execution (810 QR rows are 171 declarations; sequential arm blocks; advisory turn budget; single seed; no CIs) | CONFIRMED | Declaration-clustered QR reanalysis (§6.1); Wilson/bootstrap CIs on every rate; RD + CI on paired effects; sequential blocks, overrun counts (C 50/D 38/E 32, max 88), and single-seed status disclosed in §3; row-level tests survive only as uncorrected footnotes |
| 7 | The preregistered primary experiment was not completed, and the deviations list did not say so | CONFIRMED | The complete execution table (§3) — every component, status, inferential consequence; the three-register labeling throughout; the zero-confirmatory statement in the abstract and §3 |
| 8 | SorryDB provides little evidence about the join (no W-only/bare-union arm; 94% formal routing; missing verdicts; unstable cost-per-proof) | CONFIRMED | All 203 candidate verdicts completed (`9682b3b1`); WF-vs-F stated as indistinguishable (p=1.0; 34% of repo-bootstrap resamples ≤0); repo-clustered CIs; the cost-per-success claim deleted |

Two of the review's factual claims did not survive verification, recorded
here with appreciation rather than triumph — the review's error rate across
31 checked claims was two. First, MathlibMPR does not cluster by PR: the
data file has 69 tasks and 69 distinct PRs, one task each, so the MPR
analysis needed no cluster correction (the refuted claim). Second, arm E's
31 declaration-less fresh runs were not a behavioral response to interface
overload — they were contiguous session-limit 429 errors at the tail of E's
sequential block. That correction cuts both ways: it removes the review's
"interface overload" reading, and it removes v1's "unjoined volume is
harmful" claim along with the error-inflated headline p=2.4×10⁻⁵ (which
appears in this edition only in this paragraph, as the corrected artifact
it is; the repaired contrasts are §4.2's).

## 10. Roadmap

Adopted from the review's priority list, in its order:

1. **2×2 factorial** — {join, no join} × {decl_exists, none} on a truly
   frozen post-cutoff set, same tool counts and schemas; the experiment
   that turns §4.6's attribution question into a measurement.
2. **Yoked unjoined control** — one integrated interface returning equally
   concise informal and formal panels without declaring correspondences,
   plus a preregistered manipulation criterion for actual dual-corpus use.
3. **Manual freeze on development data** — split MathlibQR by its 171
   target declarations, freeze the manual on a dev split, evaluate on
   unseen targets; run bare-union, F+manual, W+manual, union+manual.
4. **Human judge calibration** — the preregistered 50-item hand-graded set,
   judge–human agreement reported, before any faithfulness number leaves
   the exploratory register.
5. **In-loop compiler proving** — the SorryDB follow-up with identical
   build access per arm; external validity the one-shot protocol lacks.
6. **Multi-seed, multi-model** — ≥3 seeds per task and a second model
   family; no variance estimate exists anywhere in the current data.
7. **Frozen search snapshots** — read-only pinned checkout predating every
   test theorem, locally snapshotted Loogle index, per-run hashes of tree,
   index, Brain snapshot, prompts, and manifests.

## 11. Data availability

Everything needed to recompute this report lives in the private
preservation repository **`Deicyde/wikilean-bridge-experiment`** (report at
the root as `README.md` and under `report/`). Provenance commits refer to
the WikiLean working repository.

| Path (preservation repo) | Contents | Key provenance commits (WikiLean) |
|---|---|---|
| `docs/research/BRIDGE-EXPERIMENT.md` | preregistration incl. deviations 1–7 | `0d36f266` (2026-07-16) |
| `docs/research/BRIDGE-RESULTS.md` | Tier-1 + retrieval results log | `53f5cb31`, `daac6107` |
| `docs/research/review/` | external review 1 verbatim + independent claim-by-claim verification (29 confirmed / 1 partial / 1 refuted) | `f97e67e4` |
| `bench/*.py`, `bench/README.md`, `bench/arms/` | Tier-1 harness: runner, scorer, REPL typecheck rig, trace analysis, arm manifests | `068abe34`, `e554902e`, `6c2ca704`, `31b95caa`, `79ac3dfe` |
| `bench/data/bridge_tasks.jsonl` (+`.stats`) | 371 ProofNet# tasks; source pin + MIT licence | `e554902e`, `ec2a9e11` |
| `bench/data/fresh_tasks.jsonl` (+`.stats`) | 100 post-Brain-index tasks + determinacy annotations + the (Brain-scoped) held-out check | `df5dbf92`, `a0d45103` |
| `bench/data/bridge_summary.json` | v1 Tier-1 scored summary (pre-repair) | `53f5cb31` |
| `bench/data/runs/` | all 2,355 Tier-1 run rows, incl. the 31 repaired E rows | `19a90209` (repair driver) |
| `bench/data/runs_E_fresh_429_archive/` | the 31 original 429-errored E rows, byte-preserved | `f97e67e4` |
| `bench/data/runs_devleak_2026-07-18/` | the 120 quarantined skill-leak dev runs (deviation 6) | `f89e7a41` |
| `bench/analysis/` | v2 corrective statistics: `tier1_reanalysis.{py,json,md}`, `fresh_exposure.{py,json,md}`, `retrieval_clustered.{py,json,md}`, `rerun_E_fresh.py`, `score_e31_v2.py` → `bridge_summary_v2.json` + `part1_fresh100_v2.{json,md}`, `judge_fresh_run.py` / `judge_fresh_summary.py` | `64c48052`, `19a90209` |
| `bench/v2/` | v2 harness + `AGENT_MANUAL.md` | `c7629584`, `21032c06` |
| `bench/v2/data/` | MathlibQR/MPR (LeanSearch-v2 @`94f4888cbaf9`, CC BY 4.0), SorryDB-2601 split, `tasks_frozen.jsonl` | `c7629584`, `87638cfc`, `db679e8d` |
| `bench/v2/runs/` | every v2 run row with full gzipped stream-json transcripts (QR 810×4, MPR 69×4, SorryDB 508 rows) + `verify.jsonl` — **all 203 frozen-candidate kernel verdicts, complete** | `3664aa3d`, `daac6107`, `382e51bf`, `1fe21cca`, `9682b3b1` |

Recomputation entry points: `bench/score_bridge.py` (Tier-1 grading),
`bench/analysis/tier1_reanalysis.py` + `score_e31_v2.py` (§4),
`bench/analysis/fresh_exposure.py` (§4.4),
`bench/analysis/retrieval_clustered.py` (§6.1–6.2, §7 uncertainty),
`bench/trace_analysis.py` (§4.5), `bench/v2/score_retrieval.py` (§6
point estimates), `bench/v2/verify_sorrydb.py` (§7 verdicts),
`bench/analysis/judge_fresh_summary.py` (§5, pending).

**External sources cited** (fetched and verified during the v2 design
sweep): TheoremGraph arXiv:2606.25363 · LeanSearch arXiv:2403.13310 ·
LeanSearch-v2 arXiv:2605.13137 + github.com/frenzymath/LeanSearch-v2 ·
LeanExplore arXiv:2506.11085 · SorryDB arXiv:2603.02668 + sorrydb.org ·
FATE arXiv:2511.02872 · miniCTX arXiv:2408.03350 · LeanDojo
arXiv:2306.15626 · LeanAgent arXiv:2410.06209 · Numina-Lean-Agent
arXiv:2601.14027 · miniF2F-v2 arXiv:2511.03108 · ProofNet#/ProofNetVerif
arXiv:2406.07222 · benchmark-faults survey arXiv:2606.29493.

## Appendix A: the agent manual

> The following is `bench/v2/AGENT_MANUAL.md`, the briefing prepended
> verbatim to every WF-arm prompt. It is reproduced unchanged as a study
> artifact — and, per §6, it is a **post-hoc, benchmark-informed**
> artifact: its bracketed figures were measured on the same MathlibQR/MPR
> evaluation queries the WF arm was then scored on.

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
