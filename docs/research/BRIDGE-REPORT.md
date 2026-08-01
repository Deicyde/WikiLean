# The Bridge Experiment: does an informal↔formal join move end-task Lean performance?

**Report, version 2 — 2026-07-31. Post-review corrected edition.**
Jack McCarthy (WikiLean), with experiments executed by Claude Code agents
under direction. Preregistration: `docs/research/BRIDGE-EXPERIMENT.md`,
commit `0d36f2664ab2ebb2c7b80b350d3c9bd820414335` (2026-07-16). Version 1
(2026-07-25) received an external review on 2026-07-27
(`docs/research/review/REVIEW-1.md`), whose claims we verified
independently (`verification-of-review-1.json`: 29 confirmed, 1 partial,
1 refuted); this edition incorporates the corrections, and v1 remains in
git history. All data, code, and run transcripts live in the private
repository `Deicyde/wikilean-bridge-experiment`; every number below is
recomputable from a file there (Appendix C).

**Registers.** **[PREREGISTERED-MODIFIED]** — a preregistered analysis run
with a named modification; **[EXPLORATORY]** — not preregistered (all of
Bridge v2, the uncalibrated judge); **[CORRECTIVE]** — a reanalysis
finding of this edition. **Zero results are cleanly confirmatory**: the
preregistered criterion — D>E on *faithful@budget* (semantic equivalence
to gold ∧ typecheck) and D≤E on tokens-to-solve — was never graded on
either half (§3); §5 is the exploratory substitute for the missing
equivalence leg.

---

## 1. Abstract

We test whether a curated dictionary between mathematical concepts and
Mathlib declarations (the WikiLean "Brain") improves language-model
performance on formalization tasks, against controls holding the same
corpora unjoined. Nothing is cleanly confirmatory — the preregistered
semantic-equivalence endpoint was never graded — so our primary metric is
the **grounded typecheck rate** (produced declaration, zero hallucinated
citations, passing typecheck), which does not measure semantic
correctness. On 100 post-Brain-index tasks (held out from the Brain's
indexes but not, we later verified, from the formal-search arms' sources),
the bundled Wikibrain arm reaches 40.6% against 23.2% for the
unjoined-tools control on the 69 completed pairs (McNemar 18/6, exact
p=0.023, risk difference +17.4pp [+4.1, +30.7]); after repair of the
control's 31 infrastructure-failed runs, the full-100 contrast is 42.0%
vs 30.0% (p=0.073, RD +12.0pp [+0.2, +23.8]). Join-specific attribution
awaits a factorial ablation; the clean independent finding is the
existence verifier, cutting hallucinated citations to 6.8% against
17.7–21.2% in every other arm. Exploratory: a post-hoc,
benchmark-informed union-plus-manual agent took the highest rows we
observed on MathlibQR-810 and MathlibMPR (declaration-clustered CIs
exclude the single-arm baselines); on SorryDB, kernel-verified proving
with and without the Brain is indistinguishable (10 vs 9 of 171, p=1.0).

## 2. The system under test: the WikiLean Brain

The Brain is a curated knowledge graph joining Wikipedia/Wikidata
concepts, Mathlib4 declarations, and external math databases (nLab, LMFDB,
Stacks, ProofWiki, and others). Its atom is a *cell*: a Lean declaration,
a Wikidata concept, an article, and a database page denoting the same
object are *organs* of one cell, not separate nodes (Mathlib's `Module`,
Wikidata's "module" *and* its "vector space" are one atom — Mathlib has no
`VectorSpace`). About 12,000 of Mathlib's declarations are concept-joined
this way; the Brain is a map of Mathlib, not the territory. Agents reach
it through an MCP server exposing eight tools: `brain_bridge` (free text
in, existence-verified declarations out — the informal→formal entry
point); six graph/content tools (`brain_search`, `brain_cell`,
`brain_transfer`, `brain_neighborhood`, `brain_snippets`, `brain_filter`);
and `decl_exists`, a batch existence check against the full declaration
index — all of Mathlib, not just the joined subset — meant to be called on
every name an agent is about to cite. The hypothesis: the informal↔formal
*join* is the scarce artifact — a model holds both corpora loosely and
hallucinates at their boundary — so curating it should move end-task
performance, not just lookup.

## 3. Preregistered design and what actually ran

**Hypothesis.** P1: an agent with the *joined* dictionary beats one given
informal and formal search *separately*. P2: the bridge solves at lower
cost. P3: it lifts grounding, not just compile rate. Success criterion:
D>E on faithful@budget (p<.05) AND D≤E on tokens-to-solve.

**The five arms.** Identical model, prompt, and budgets within each phase;
only the tool manifest differs (`bench/run_bridge.py`, `bench/arms/`):

| Arm | Tools | Isolates |
|---|---|---|
| A `no_tools` | none (`--tools ""`) | floor / memorization |
| B `informal` | Wikipedia + nLab search/fetch | informal reasoning alone |
| C `formal` | loogle + ripgrep + source read (Mathlib checkout) | the LeanSearch-class status quo |
| D `wikibrain` | the Brain MCP — all eight tools of §2 | the join **+ the existence verifier** (not separable here) |
| E `B+C unjoined` | B's tools AND C's tools, no join | the unjoined control |

Two design facts up front. D alone holds `decl_exists`, and
hallucination-free citation is a conjunct of the primary metric — D vs E
therefore measures the *bundled Wikibrain package*, not the join (715
`decl_exists` attempts on the 100 fresh tasks, 682 successful). And E
failed its manipulation check: 4 informal-tool touches across its 100
fresh runs against B's 345 — a formal-search agent with a larger manifest.

**Models.** Tier 1: `claude-haiku-4-5` (model id
`claude-haiku-4-5-20251001`), all five arms; Bridge v2: `claude-sonnet-5`
(a pre-execution decision, `docs/research/BRIDGE-V2-BENCHMARKS.md`). One
model per phase, one seed per (task, arm) — no run-to-run variance is
measured anywhere.

**Grading.** The primary metric is the **grounded typecheck rate**: the
run produced a declaration, cited zero names absent from a union oracle
(doc-gen4 declaration data ∪ verified renames; extractor in
`bench/score_bridge.py`), and the declaration typechecked on the pinned
toolchain.¹ It does **not** establish that the statement formalizes the
prompt — a passing declaration can be weaker, stronger, vacuous, or about
the wrong object; the preregistered faithful@budget (BEq+ equivalence to
gold) was never graded, its leg in `score_bridge.py` a stub returning
`None`.

¹ Version 1 called this metric "success (folded)"; renamed after the
review — a metric with no equivalence leg should not be called success.

**Task sets.** Tier 1a is ProofNet#: 371 problems (341 eval + 30 dev),
pinned to `PAug/ProofNetSharp`
@`a8da405fbd1e348a87445c2e562c747b7e26dc8f` (MIT; arXiv:2406.07222),
graded on Lean v4.32.0-rc1 / Mathlib `a33a5ccd`. Tier 1b is the
**post-Brain-index fresh set**²: 100 tasks from theorems merged into
Mathlib master 2026-07-03→07-16, verified absent from the Brain's
declaration universe and node set — the indexes arm D serves
(`bench/data/fresh_tasks.stats.json`) — and graded on Lean v4.33.0-rc1 /
Mathlib `9944fe29`. It was **not** verified absent from the formal-search
arms' sources: C/E read a mutable checkout (`61a5e4f338`, content through
~2026-07-10) and live unsnapshotted Loogle (exposure measured in §4.4).
The preregistered determinacy pre-screen became a post-hoc double
annotation (agreement 79%, κ≈0.20; a 74-task both-determinate subset, none
excluded).

² Version 1's "contamination-proof" and "newer than every arm's index" are
both retracted (§9, concern 4).

**Execution.** A 10-minute wall clock was enforced; the 30-turn budget was
**advisory only** (stated in the prompt, no CLI cap) — fresh-set overruns
C 50 runs, D 38, E 32, maxima 80/88/72. The fresh arms ran in strictly
sequential blocks on 2026-07-19 (A 01:50–02:08Z, B 02:09–03:56Z,
C 03:58–04:27Z, D 04:29–04:54Z, E 04:56–05:17Z), so arm is confounded with
time. Agents had no in-loop typechecker and were told to verify cited
names through their tools.

**The arm-E 429 incident and repair [CORRECTIVE].** Rows fresh_069–099 —
31 contiguous tasks at the tail of E's sequential block — errored with
session-limit 429s: infrastructure, not agent behavior. On 2026-07-27 they
were rerun (`bench/analysis/rerun_E_fresh.py`) with E's exact July-19 code
path on a read-only `git archive` of `61a5e4f338`, the same tree the
original C/E runs saw. The repair surfaced a second hazard: under
concurrent cold starts the CLI's stdio MCP servers can silently fail to
connect, yielding runs that *look* like completions but had zero tools;
the repair driver detects this from the stream-json init event and retries
(commit `19a90209`). Originals: `bench/data/runs_E_fresh_429_archive/`;
14 of the 31 repaired rows became grounded-typecheck passes
(`bridge_summary_v2.json`, `v2_provenance`).

**Execution summary.** The components whose status changes the
interpretation; the complete 25-component inventory is Appendix B.

| Preregistered component | Status → consequence |
|---|---|
| faithful@budget (BEq+ equivalence) | **not executed** (grader a stub) → primary endpoint missing; zero confirmatory results |
| LLM judge + 50-item human calibration | **not executed** in campaign → uncalibrated post-review pass only (§5) |
| tokens-to-solve (half the criterion) | **not executed** → P2 untested as specified |
| 3 reseeds + pass@k curves | **not executed** → single seed; no variance estimate anywhere |
| Second model class on the primary set | **not executed** → Haiku Tier-1 / Sonnet v2; generality unknown |
| Tier 2 as specified | replaced by exploratory v2 → §§6–7 exploratory |
| 30-turn budget | **modified** (advisory only) → uncontrolled; §4.3 |
| Fresh-set holdout | Brain indexes only → renamed post-Brain-index; §4.4 |
| Sequential blocks; arm-E 429 outage | **corrective** repair → snapshot E row conflates outage with behavior; §4.2 |

## 4. Tier 1: statement autoformalization (claude-haiku-4-5)

**[PREREGISTERED-MODIFIED]** — the preregistered paired design, graded on
grounded typecheck rate instead of faithful@budget; one model, one seed,
advisory budget. Sources: `bench/analysis/tier1_reanalysis.{json,md}`,
`part1_fresh100_v2.{json→bridge_summary_v2.json,md}`. All rates carry
Wilson 95% CIs; McNemar counts are discordant pairs (tasks exactly one arm
passed), written first-arm-only/second-arm-only.

### 4.1 Tier 1a — ProofNet# eval split (n=341/arm)

| arm | grounded typecheck | Wilson 95% CI |
|---|---|---|
| A none | 204/341 = 59.8% | [54.5, 64.9] |
| B wiki | 198/341 = 58.1% | [52.8, 63.2] |
| C formal | 218/341 = 63.9% | [58.7, 68.9] |
| D wikibrain | 219/341 = 64.2% | [59.0, 69.1] |
| E B+C unjoined | 208/341 = 61.0% | [55.7, 66.0] |

The arms sit within a few points of each other (v1's D-vs-E McNemar,
63 vs 51 discordant over all 371 pairs, p=0.30, is an underpowered null).
Arm A at ~60% with zero tools is substantial ProofNet memorization — the
motivation for Tier 1b. Typecheck alone *anti-correlates* with grounding
(E led typecheck at 83.3% while trailing on hallucinations), reproducing
TheoremGraph's typecheck-is-not-a-signal finding at 15× their n.
Contaminated by construction, Tier-1a serves only for paired deltas and
the memorization control.

### 4.2 Tier 1b — the post-Brain-index fresh set (n=100/arm)

Three analysis bases, one table: E's 31 infrastructure-dead rows counted
as failures; the 69 pairs arm E completed; the full 100 after the repair
(§3). Cells are grounded typecheck, n/N = % [Wilson 95% CI].

| arm | errors-as-failures (n=100) | completed pairs (n=69) | post-repair (n=100) |
|---|---|---|---|
| A | 20/100 = 20.0% [13.3, 28.9] | 10/69 = 14.5% [8.1, 24.7] | 20/100 = 20.0% [13.3, 28.9] |
| B | 22/100 = 22.0% [15.0, 31.1] | 12/69 = 17.4% [10.2, 28.0] | 22/100 = 22.0% [15.0, 31.1] |
| C | 25/100 = 25.0% [17.5, 34.3] | 12/69 = 17.4% [10.2, 28.0] | 25/100 = 25.0% [17.5, 34.3] |
| D | **42/100 = 42.0%** [32.8, 51.8] | **28/69 = 40.6%** [29.8, 52.4] | **42/100 = 42.0%** [32.8, 51.8] |
| E | 16/100 = 16.0% [10.1, 24.4] — **31 infra errors** | 16/69 = 23.2% [14.8, 34.4] | 30/100 = 30.0% [21.9, 39.6] |

D vs C (28/11 discordant, exact p=0.0095) and D vs A (29/7, p=0.0003) are
untouched by E's outage. D vs E: on completed pairs — the headline Tier-1
contrast — 18/6, p=0.023, RD +17.4pp [+4.1, +30.7] (paired Wald; D vs C
21/5 p=0.0025 and D vs A 22/4 p=0.0005 there); post-repair full-100,
25/13, p=0.073, RD +12.0pp [+0.2, +23.8]. The spread: significant on
completed pairs, marginal on the full set (the RD interval barely excludes
zero; the McNemar p misses 0.05); the errors-as-failures column conflates
an outage with behavior, and its inflated p survives only in §9. Repaired,
E is mid-pack (30%, indistinguishable from C, p=0.42); v1's "unjoined tool
volume can be actively harmful" is retired. Arm A's collapse from 59.8%
(1a) to 20% is the study's cleanest memorization quantification.

### 4.3 Turn-budget sensitivity [CORRECTIVE]

Restricted to pairs where both arms stayed within 30 turns
(`tier1_reanalysis.md` §5): D vs E n=45, 46.7% [32.9, 60.9] vs 24.4%
[14.2, 38.7], 16 discordant, p=0.021 — but that counts E's 429 rows
(turns=1) as within-budget failures, and the completed-only version is
underpowered (n=27, 44.4% [27.6, 62.7] vs 40.7% [24.5, 59.3], 7
discordant, p=1.0). D vs C within budget: n=35, 54.3% [38.2, 69.5] vs
25.7% [14.2, 42.1], 12 discordant, p=0.0063. Consistent in direction;
for D-vs-E, inconclusive once both restrictions apply at once.

### 4.4 Exposure strata: what the formal-search leak touched [CORRECTIVE]

We measured the §3 leak against the pinned tree `61a5e4f338`
(`bench/analysis/fresh_exposure.{json,md}`): of the 100 golds, the full
dotted name is in-tree for **37**, the basename is a declaration header
anywhere for **64** and in the task's own module file for **51** (the
stratum basis), and the gold's commit is an ancestor of the pin for 49.³
Cells: grounded typecheck, snapshot basis — E's 429 rows count as
failures, hitting its unexposed cell hardest; attempted-only excludes
them.

³ Measured on tree bytes, not merge metadata: fresh_037 and fresh_054 are
exposed despite post-pin commits (fresh_054's dotted name sits in its
module file at the pin) — hence 51 exposed ≠ 49 merged-before-pin.

| arm | exposed (n=51) | unexposed (n=49) |
|---|---|---|
| A | 9/51 = 17.6% [9.6, 30.2] | 11/49 = 22.4% [13.0, 35.9] |
| B | 8/51 = 15.7% [8.2, 28.0] | 14/49 = 28.6% [17.8, 42.4] |
| C | 11/51 = 21.6% [12.5, 34.6] | 14/49 = 28.6% [17.8, 42.4] |
| D | **24/51 = 47.1%** [34.1, 60.5] | **18/49 = 36.7%** [24.7, 50.7] |
| E | 12/51 = 23.5% [14.0, 36.8] | 4/49 = 8.2% [3.2, 19.2] |
| E attempted-only | 12/44 = 27.3% [16.4, 41.9] | 4/25 = 16.0% [6.4, 34.6] |

McNemar by stratum: D vs E exposed 15/3 p=0.0075, unexposed 17/3
p=0.0026; D vs C exposed 18/5 p=0.0106, unexposed 10/6 p=0.45. The leak's
direction favors C/E, and D's edge over E is strongest exactly where there
was nothing to leak (36.7% vs 8.2% unexposed; attempted-only, vs 16.0%) —
so the leak cannot explain that contrast. D over C, by contrast,
concentrates in the exposed stratum and is a null unexposed — part of D's
advantage over pure formal search may rest on targets C could have found
in-tree but didn't.

### 4.5 Hallucinated citations and trace attribution

Fresh set, all 100 rows per arm, post-repair (union oracle of
`score_bridge.py`):

| arm | halluc-decl rate (cited names) | runs w/ ≥1 hallucination | Wilson 95% CI (runs, n=100) |
|---|---|---|---|
| A | 94/443 = 21.2% | 54 | [44.3, 63.4] |
| B | 76/429 = 17.7% | 48 | [38.5, 57.7] |
| C | 108/517 = 20.9% | 49 | [39.4, 58.7] |
| D | **32/472 = 6.8%** | **23** | [15.8, 32.2] |
| E | 107/505 = 21.2% | 49 | [39.4, 58.7] |

Citation-level rates are descriptive (citations cluster within runs); the
run-level proportions carry the CIs. This is the study's most robust
effect: unchanged by the repair, and larger off-distribution than on
(Tier-1a: D 5.9% vs 10.1–11.3%). **Trace attribution [EXPLORATORY — eval
split only]** (`bench/trace_analysis.py`): 98–100% of traced tool-arm runs
cite at least one declaration that surfaced in a tool result; only 10–17%
of citations never touched the tools; 35% of arm-D eval runs (116/329)
cite a declaration from a brain_bridge/brain_transfer result — English in,
formal name out (an undercount: result heads truncate at 200 chars). The
sharpest signal is among *hallucinated* citations: the
checked-and-cited-anyway rate is 35% for C and 30% for E but **13% for D**
— binary verification disciplines the model where fuzzy search lets it
fool itself.

### 4.6 The existence verifier as an independent finding

What the data support: a **batch, binary, oracle-backed existence check**,
used on every name, collapses hallucinated citations roughly 3× across two
task distributions, concentrated on citations the agent had already
"checked" by search. What they do not yet support: how much of D's
grounded-typecheck edge is the verifier versus the join — D alone holds
it, and no-hallucination is a conjunct of the metric; the 2×2 factorial
(§10) separates them. One circularity: `decl_exists` and the scoring
oracle draw on the same doc-gen4 index, so "fewer hallucinations" partly
means "the arm could query the grader's oracle" — itself the product
claim, but it must be named.

**Cost (descriptive only).** Campaign means per task over all 471 runs/arm
as run (pre-repair; E's 31 error rows cost ≈0): A $0.034, B $0.048,
C $0.140, D $0.121, E $0.128; mean wall-clock C 116 s, D 89 s, E 106 s.
tokens-to-solve was never computed (§3), so P2 is untested; v1's "cheaper
and better" framing is withdrawn.

## 5. Semantic faithfulness (uncalibrated judge) [EXPLORATORY]

Following the review, a blind LLM-judge pass over all 500 fresh outputs
(5 arms × 100 tasks, the 31 repaired E rows included) was launched
2026-07-27 (`bench/analysis/judge_fresh_run.py`) as the exploratory
substitute for the ungraded equivalence endpoint (§3). Judge =
`claude-sonnet-5`, deliberately stronger than the Haiku subjects; no
tools, one turn; prompt = {informal statement, gold, produced
declaration}, scanned for arm-revealing substrings (abort on hit);
no-output rows count not-equivalent; a fixed 50-item seed-stratified
subset (seed 20260727, 10 per arm) is re-graded for self-consistency.
Pre-declared deviation: the judge sees gold_context + gold_formal (the
`variable`/`open` binders), arm-independent. **The judge is uncalibrated**
— the preregistered 50-item human calibration remains undone — so nothing
here rescues the missing confirmatory endpoint.

[[SLOT:JUDGE]] — *to be filled from `bench/analysis/judge_fresh_summary.json`
when the pass completes (arms A–D graded, E in progress). Tables to land,
per `judge_fresh_summary.py`: (1) per-arm strict and evaluated equivalence
rates on fresh-100, Wilson 95% CIs; (2) the conjunction grounded typecheck
∧ judge-evaluated-equivalent — the closest available analogue of
faithful@budget; (3) exact-binomial McNemar D-vs-E, D-vs-C, D-vs-A on
both, full-100 and completed-69; (4) self-consistency on the 50-item
re-grade, plus the judge cost/error census.*

## 6. Third-party retrieval: MathlibQR-810 and MathlibMPR (claude-sonnet-5) [EXPLORATORY]

Bridge v2 uses graders that predate the arms: third-party expert gold
labels here, the Lean kernel in §7. Data: `frenzymath/LeanSearch-v2`
@`94f4888cbaf9` (CC BY 4.0; arXiv:2605.13137). Arms: N (no tools), F
(loogle + decl_grep + decl_read), W (the Brain MCP), and WF (W ∪ F
**plus** the Appendix-A manual prepended to the prompt). Scoring:
`bench/v2/score_retrieval.py`, exact full-name match.

**WF is a post-hoc, benchmark-informed condition; every WF number carries
that label.** Verified commit times: N/F/W results 22:27 on 07-24; the
manual 00:30 on 07-25; WF results 01:15. The manual distills measurements
from the same evaluation queries WF was then scored on — its bracketed
figures (per-style nDCG, the 63% brain_cell failure rate, the 143/810
format failures, call counts) are the N/F/W/system-mode eval-set
aggregates, reproduced exactly — so WF is development evidence of a
maximally equipped, briefed agent, not an untouched test result or a
defensible SOTA comparison. (Version 1 also misattributed the 63% figure
to "the Tier-1 traces"; it is the v2 W-arm figure — 482 calls, 63.3%
failed.)

### 6.1 MathlibQR fair-810 — concept retrieval, declaration-clustered [CORRECTIVE statistics]

The 810 query rows are paraphrase styles of only **171 distinct gold
declarations** (2–6 rows each), so inference is at the declaration level
(`bench/analysis/retrieval_clustered.md`; cluster bootstrap B=10,000,
seed 20260727):

| system | R@10 (row) | R@10 95% CI (decl-cluster boot) | nDCG@10 | nDCG 95% CI |
|---|---|---|---|---|
| published: TheoremGraph | 0.775 | — | 0.548 | — |
| published: LSv2 retriever+reranker | 0.780 | — | 0.623 | — |
| system-mode wikibrain (one brain_bridge call, no LLM) | 0.036 | — | 0.031 | — |
| agent N | 0.633 | [0.581, 0.684] | 0.598 | [0.547, 0.647] |
| agent F | 0.831 | [0.792, 0.868] | 0.790 | [0.750, 0.829] |
| agent W | 0.816 | [0.767, 0.862] | 0.781 | [0.731, 0.827] |
| agent WF (post-hoc, benchmark-informed) | 0.885 | [0.849, 0.919] | 0.839 | [0.801, 0.874] |

Declaration-level paired contrasts: **F − W is a null** (R@10 +0.015
[−0.028, +0.059], Wilcoxon p=0.26; nDCG +0.010 [−0.029, +0.048]) — tied,
with different textures: W wins the special_case style (nDCG 0.523 vs
0.384), F the Lean-syntax styles (grep cannot find what source never
names). WF − F +0.054 [+0.027, +0.082], p=0.0017; WF − W +0.069
[+0.032, +0.108], p=0.0003 — the briefed union beats both single arms even
under clustering.⁴ The system-mode row is the headline API deficiency: one
brain_bridge call scores 0.036 — the free-text entry is a label/alias
resolver, not a semantic retriever — while an agent *iterating* over the
same API reaches 0.816; the content is in the graph, the single-shot entry
point is the gap. N's 0.633 includes 143/810 rows scored 0 for format
non-compliance.

⁴ Version 1's row-level McNemars (n=810 treated as independent: F−W 78/66
discordant p=0.36; WF−F 71/27 p=1.0×10⁻⁵; WF−W 83/27 p=8.3×10⁻⁸) are
uncorrected for the clustering — the 144 F/W discordant rows come from
only 76 declarations — and are superseded.

### 6.2 MathlibMPR — premise retrieval (69 post-cutoff PR theorems)

One task per PR, so no cluster correction is needed (§9); task-level
bootstrap CIs:

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

A generic Sonnet agent with grep and loogle **matches the specialist
premise retriever** (0.453 vs 0.461), untouched by this study's confounds.
W trails F by 18pp: the preregistered concept ≠ premise boundary
(TheoremGraph's negative transfer) measured on our own tools — a
quantified target for `brain_premises` (BRIDGE-ISSUES #7). WF − F is
marginal: +0.104 [+0.007, +0.201], sign test p=0.029, Wilcoxon p=0.049.

### 6.3 Tool use under the manual

Census, pooled QR+MPR: F averages 2.2 calls/run; W 3.5 (decl_exists 1,251
+ brain_bridge 608 — verify-then-cite made mechanical); WF 4.6 in a
genuine dual-toolkit mix (decl_grep ~1.5k, decl_exists ~0.9k, brain_bridge
~0.7k, decl_read ~0.5k, loogle ~0.4k). Under the manual, brain_cell misuse
(63% failure in W) falls to a single call in WF. Same-eval-set caveat as
above.

[[SLOT:UNION]] — *the bare-union U arm (W ∪ F tools, no manual;
`MANUAL_ARMS` blanked in `bench/v2/run_agent.py`) separates the union
effect from the briefing effect: N/F/W/U/WF on both benchmarks,
declaration-clustered (QR) and task-bootstrap (MPR) CIs, U-vs-F and
WF-vs-U paired contrasts. Until it lands, "union tools" and
"benchmark-informed manual" remain unseparated in WF.*

## 7. SorryDB: kernel-graded proving in live repositories (claude-sonnet-5) [EXPLORATORY]

SorryDB (arXiv:2603.02668, sorrydb.org) serves unsolved `sorry`s from real
Lean repositories; success is the repo *building* with your proof — no
judge, nothing to memorize. From the SorryDB-2601 1,000-task evaluation
split we sampled repositories (disk allowed one repo build at a time).
**Snapshot rot:** the original freeze (8 repositories, 164 tasks) lost 74
of 164 tasks (45%) in ~six months — pinned commits of GlimpseOfLean,
LeanCourse25, and most Foundation and SemicircleLaw tasks no longer exist
on GitHub (history rewritten) — and the rot is invisible without actually
fetching every pin: GitHub's archive endpoint answers preflight HEAD for
dead commits with HTTP 200. The refreeze (`bench/v2/sorrydb_prep.py`,
commit `db679e8d`) keeps verified-fetchable pins only: **171 tasks across
10 repositories**. Protocol: build-free, one-shot — goal state plus a
±60-line window at the pin; the agent (600 s; arms N/F/WF as in §6)
outputs one replacement for the `sorry` under an explicit
honest-abstention rule; `bench/v2/verify_sorrydb.py` clones, splices,
rebuilds; success = exit 0 with no sorry/axiom warning.

**Results — verdict-complete** (all 203 frozen-task candidates have kernel
verdicts, commit `9682b3b1`; v1's 8 curve25519 stragglers verified as 0
proved); n=171 tasks/arm:

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

(3 N and 2 F tasks lack run rows — runner losses. Distinct sorries proved:
13; N's are a strict subset of F's and WF's; WF∩F 6, WF-only 4, F-only 3;
proofs concentrate in PersistentDecomp, LeanAPAP, MiscYD — 3 of the 10
repos.)

**WF vs F is statistically indistinguishable**: one proof apart (10 vs 9
of 171), task-level exact McNemar 4/3, p=1.0; repo-clustered bootstrap
+0.58pp, 95% CI [+0.0, +1.85pp], 34% of resamples no difference or worse
(`retrieval_clustered.md` §4). SorryDB says essentially nothing about the
Brain — WF routed 94% of its tool calls to formal search, and no W-only or
bare-union arm ran. It does show the tools-vs-no-tools gap: N burns its
budget (70 of 168 rows produce nothing, mostly 600 s timeouts) while tool
arms triple-to-quintuple the prove rate and double honest abstention —
though 10 repo clusters warrant caution even there. Cost-per-proved is
bookkeeping only (unstable at 2/9/10 successes); v1's "adding the join
never made the task more expensive per success" is deleted (§9). The
published SorryDB agentic best (~30%) is not comparable: different task
subset, different protocol (published agents iterate against the build;
ours drafted one-shot).

## 8. What survives and what does not

Each retired v1 claim, with its v2 replacement:

| v1 claim (retired) | v2 replacement (supported) |
|---|---|
| "The join carries the effect" (fresh McNemar p<0.0001) | The **bundled Wikibrain condition** outperforms the tested controls (§4.2); join attribution awaits the 2×2 factorial |
| "42% success" | "42% **grounded typecheck rate**" — no equivalence leg graded; §5 is the exploratory substitute |
| "Contamination-proof / newer than every index" | "**Post-Brain-index**": Brain indexes only; 51/100 golds exposed to C/E, outcomes stratified (§4.4) |
| "Arm E worst at 16%; unjoined tool volume actively harmful" | The 31 no-output rows were 429s; repaired, E is mid-pack at 30% (§4.2) |
| "WF sets the best rows we know of" | A **post-hoc, benchmark-informed** condition took the highest observed rows (QR 0.885, MPR 0.557); manual tuned on the same eval queries (§6) |
| "Adding the join never made the task more expensive per success" | Deleted — cost is descriptive, tokens-to-solve never computed, SorryDB 10-vs-9 p=1.0 |
| brain_cell 63% failure "in the Tier-1 traces" | The figure is from the v2 W-arm eval runs (§6) |

What survives, with register: the **hallucination collapse** /
existence-verifier finding (§4.5–4.6) [preregistered-modified +
exploratory traces]; the **memorization quantification** (59.8% → 20.0%)
[preregistered-modified]; the **system-mode gap** (0.036 vs 0.816, §6.1) —
an API deficiency, not a content deficiency [exploratory]; the
**F-matches-SOTA baseline** and **concept≠premise boundary** (§6.2)
[exploratory]; **snapshot rot** (§7) [exploratory]; and the
**tools-vs-no-tools proving gap** (§7) [exploratory, 10 clusters].

## 9. Response to external review 1

The review raised eight concerns; all eight were confirmed in substance
after claim-by-claim verification, and the table gives the action on each
(full record: `docs/research/review/`). Two of its factual claims did not
survive (an error rate of two across 31 checked): MathlibMPR does not
cluster by PR (69 tasks, 69 distinct PRs — no correction needed; the
refuted claim), and arm E's 31 declaration-less runs were contiguous 429
errors, not interface overload. The second correction cuts both ways: it
removes the review's overload reading, and it removes v1's error-inflated
headline p=2.4×10⁻⁵ with the "unjoined volume is harmful" claim (repaired
contrasts: §4.2).

| # | Concern | Action in v2 |
|---|---|---|
| 1 | "Success" measures no semantic correctness | Renamed **grounded typecheck rate**; judge pass §5; calibration roadmap #4 |
| 2 | D vs E does not isolate the join | "Join carries the effect" retired; bundled-condition language (§4.6); 2×2 factorial roadmap #1 |
| 3 | Arm E failed its manipulation check | Disclosed (§3); yoked control roadmap #2 |
| 4 | Fresh set not isolated from formal-search sources | Renamed **post-Brain-index**; exposure strata (§4.4); frozen snapshots roadmap #7 |
| 5 | WF is test-set tuned | Labeled **post-hoc, benchmark-informed** everywhere; provenance in §6; 63% misattribution fixed; bare-union slotted; manual freeze roadmap #3 |
| 6 | Independence and execution (clustered rows, sequential blocks, advisory budget, single seed, no CIs) | Declaration-clustered reanalysis (§6.1); Wilson/bootstrap CIs and RD+CI throughout; §3 disclosures; row-level tests demoted to footnotes |
| 7 | Preregistered experiment incomplete; deviations list silent | Execution summary + Appendix B inventory; register labels; zero-confirmatory up front |
| 8 | SorryDB evidence about the join is thin | All 203 verdicts completed; WF-vs-F indistinguishable; repo-clustered CIs; cost claim deleted (§7) |

## 10. Roadmap

Adopted from the review's priority list, in its order:

1. **2×2 factorial** — {join, no join} × {decl_exists, none} on a truly
   frozen post-cutoff set; turns §4.6's attribution question into a
   measurement.
2. **Yoked unjoined control** — one integrated interface, informal and
   formal panels without declared correspondences, plus a preregistered
   manipulation criterion.
3. **Manual freeze on development data** — split MathlibQR by its 171
   target declarations, evaluate on unseen targets; run bare-union,
   F+manual, W+manual, union+manual.
4. **Human judge calibration** — the preregistered 50-item hand-graded set
   before any faithfulness number leaves the exploratory register.
5. **In-loop compiler proving** — the SorryDB follow-up with identical
   build access per arm.
6. **Multi-seed, multi-model** — ≥3 seeds per task and a second model
   family.
7. **Frozen search snapshots** — pinned checkout predating every test
   theorem, snapshotted Loogle index, per-run hashes of tree, index, Brain
   snapshot, prompts, and manifests.

## 11. Data availability

Everything needed to recompute this report lives in the private
preservation repository **`Deicyde/wikilean-bridge-experiment`** (report
at the root as `README.md` and under `report/`); provenance commits refer
to the WikiLean working repository. The complete file map, recomputation
entry points, and external sources are Appendix C.

## Appendix A: the agent manual

> `bench/v2/AGENT_MANUAL.md`, prepended verbatim to every WF-arm prompt; a
> **post-hoc, benchmark-informed** artifact (§6) — its bracketed figures
> were measured on the same MathlibQR/MPR evaluation queries the WF arm
> was then scored on.

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

## Appendix B: complete execution inventory

Every preregistered component, its status, and the inferential
consequence. (Sources: the preregistration; the claim-by-claim inventory
in `verification-of-review-1.json`, prereg section.)

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

## Appendix C: data file map

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
| `bench/v2/runs/` | every v2 run row with full gzipped stream-json transcripts (QR 810×4, MPR 69×4, SorryDB 508 rows) + `verify.jsonl` — all 203 frozen-candidate kernel verdicts, complete | `3664aa3d`, `daac6107`, `382e51bf`, `1fe21cca`, `9682b3b1` |

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
