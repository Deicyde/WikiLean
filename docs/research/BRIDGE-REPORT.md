# Grounding Lean agents with a curated informal–formal join: a preregistered study with corrective reanalysis

**Report, version 3.1 (v3 paper structure + the preregistered factorial,
§4.4) — 2026-08-18.**
Jack McCarthy (WikiLean). Experiments and analyses were executed by Claude
Code agents under the author's direction (disclosure in §7).
Preregistrations: `docs/research/BRIDGE-EXPERIMENT.md`, commit `0d36f266`
(2026-07-16), and the 2×2 factorial `docs/research/BRIDGE-FACTORIAL.md`,
commit `3658bd58` (2026-08-07, before any factorial row ran). Versions 1 (2026-07-25) and 2 (2026-07-31) remain in git
history, and the external reviews of both are preserved verbatim in
`docs/research/review/`. Everything moved out of the main text lives in
`docs/research/BRIDGE-SUPPLEMENT.md`, cited below as "Supplement §Sn".
All data and code are in the private preservation repository
`Deicyde/wikilean-bridge-experiment` (Supplement §S14); every number in
this paper is recomputable from a named file there.

## Abstract

We test whether a curated join of informal concepts and Mathlib
declarations — the WikiLean Brain, served to agents as tools — improves
language-model Lean performance, in a preregistered five-arm study
with controls holding the same corpora accessible but unjoined. No
Tier-1 preregistered confirmatory endpoint was graded (§3.3);
everything there is exploratory or corrective; the later 2×2 factorial
(§4.4) is the study's one preregistered-confirmatory analysis. A
blinded audit found the hallucination
oracle 13.3% precise on flags and biased against the controls; a
five-rule repair validated at 90% held-out; inference moved to a
commit-clustered bootstrap. Tools versus none is robust: on 100
post-Brain-index tasks Brain-arm grounded typecheck is 48% over a
21% floor (+0.27, 95% CI [+0.13, +0.40], p=0.0004), and formal tools
collapse oracle-flagged hallucination — an upper bound on truth —
to 6–11% of runs versus 30–37% (clustered p=0.0002). Between packages
nothing favors the join, and the preregistered factorial (join ×
verifier, 400 interleaved capped runs on the same tasks) now says so
causally: both main effects are null (join +0.03, 95% CI
[−0.09, +0.14], p=0.65; verifier +0.05, [−0.03, +0.12], p=0.23), with
an exploratory negative interaction — the verifier's +10-point pairwise
gain appears only without the join, whose outputs arrive already
verified. The one decisive contrast, premise retrieval, favors formal
search by 27 points (p=2.3e-6, exploratory). The join's measured value is
verification and routing, not content: 88% of Brain-arm hits are
generated names its oracle verified, 10% graph-surfaced, and the
graph holds 38% of golds. The proving phase was uninformative: the union arm sent
94% of calls to formal search; the Brain-only arm never ran. Transferable findings close the paper: instrument bias, a
contamination-by-endpoint interaction, four infrastructure failure
modes, and benchmark snapshot rot.

## 1. Introduction and contributions

A language-model agent working in Lean holds two corpora at once,
informal mathematics and the formal library, and it holds them loosely:
it hallucinates most at their boundary, citing declarations that do not
exist for concepts it understands. Four independent 2025–26 systems
(CRAMF, Aria, DRIFT, DDR; §2) converged on the same response, grounding
autoformalization in concept-level retrieval, and all four report gains.
But all four construct the informal→formal join per query, with learned
components, and evaluate against no-retrieval or weaker-retrieval
baselines. None asks the causal question: given the same corpora, does
*joining* them help?

We built the join as a persistent artifact and preregistered that test.
The WikiLean Brain is a curated knowledge graph whose atom — a *cell* —
fuses a Mathlib declaration, a Wikidata/Wikipedia concept, and external
database pages denoting the same mathematical object, with typed,
provenance-carrying bonds between atoms. Agents reach it through eight
MCP (Model Context Protocol) tools: `brain_bridge` (free text in,
existence-verified declarations out), six graph and content tools, and
`decl_exists`, a batch existence check against an index of all of
Mathlib, not just the joined subset.

This paper is the corrected report of that experiment, restructured
after two external reviews. Its contributions are five.

1. **The only causal-control design we could verify — carried to its
   factorial completion.** Among the 2025–26 concept-grounding systems,
   this is the only evaluation whose control arms hold the same informal
   and formal corpora accessible but unjoined (§2, §3) — the design that
   isolates joining itself — and §4.4 finishes it with the preregistered
   2×2 join × verifier factorial (both main effects null).
2. **A robust tools-versus-none effect on grounded output** (§4). The
   Brain arm exceeds the no-tools floor by 27 points of grounded
   typecheck rate under commit-clustered inference (p=0.0004), the
   unjoined-tools arm by 16 points (clustered p=0.012), and formal-tool
   access collapses repaired-oracle-flagged citation hallucination
   (clustered p=0.0002). These survive instrument repair — including a
   held-out blinded revalidation — and clustering.
3. **A mechanism finding** (§5): on retrieval benchmarks the join's
   measured value is *verification and routing, not content*. 88% of the
   Brain arm's hits are generate-then-verify, only 10% are surfaced by
   the graph, and the graph contains 38% of the golds — convergent with
   DDR's independent finding for existence-checked generation.
4. **Transferable evaluation-design findings** (§§3–7): a hallucination
   oracle whose flagged class was 13.3% precise and biased against the
   control arms; a contamination-by-endpoint interaction in which the
   apparent winner flips with the choice of endpoint; four
   infrastructure failure modes that silently manufacture arm
   differences; and benchmark snapshot rot (45% task loss in six
   months).
5. **The artifact itself** (§3): a characterized, provenance-carrying
   informal–formal join that other groups can evaluate against.

Equally plainly, **between-tool-package superiority is not established
at this sample size.** The Brain-versus-unjoined contrast is +0.11
(p=0.30) and Brain-versus-formal-search +0.15 (p=0.10); the
preregistered semantic endpoint was graded only by an uncalibrated LLM
judge; and the Brain arm bundles the join with the verifier — the
preregistered factorial (§4.4) now estimates both factors directly and
finds neither detectable on its own (both main effects null).

## 2. Related work

Concept grounding for autoformalization is no longer a novel idea; the
causal test of the join is. CRAMF (arXiv:2508.06931) builds a
concept-definition knowledge base from Mathlib4 — 26k formal
definitions, 1k+ concepts, assembled by an unsupervised LLM pipeline —
and injects retrieved definitions into a single translation call. Aria
(arXiv:2510.04520, ICLR 2026) decomposes each informal statement into a
concept dependency graph, grounds each node against Mathlib on the fly,
and grades outputs with a semantic checker (AriaScorer). DRIFT
(arXiv:2510.10815, ICLR 2026) decomposes statements into concept-level
sub-queries for a Mathlib-fine-tuned premise retriever and grades with
BEq+. DDR (arXiv:2511.11990) has a model *generate* candidate dependency
names and validates each against the library with a suffix-array
existence check. DDR is the closest published analog of `decl_exists`,
and independent convergence on existence verification of generated
names as the anti-hallucination mechanism (§5).

Three adjacent lines complete the picture. TheoremGraph
(arXiv:2606.25363) builds statement-level graphs on both registers and
bridges them by embeddings; it is a dataset, not agent tooling.
LeanSearch-v2 (arXiv:2605.13137) is learned declaration-level premise
retrieval over a formal-only corpus with a fixed downstream prover loop.
ProofNetVerif/BEq+ (arXiv:2406.07222) anchors the semantic-grader axis
and is the ready-made instrument for the equivalence leg this study left
ungraded; on that axis Aria and DRIFT are ahead of us.

| System | Unit of retrieval | Join | Coverage | Side | Semantic grader | Downstream eval | No-join control |
|---|---|---|---|---|---|---|---|
| CRAMF (2508.06931) | concept→definition | learned (LLM pipeline) | 26k Mathlib defs / 1k+ concepts | system (RAG) | translation metrics | statement autoformalization | no (no-RAG baseline) |
| Aria (2510.04520) | concept node, per-stmt dep graph | learned, ephemeral per query | Mathlib (on the fly) | agent | AriaScorer | statement autoformalization | no |
| DRIFT (2510.10815) | concept sub-query→premise | learned (fine-tuned DPR) | Mathlib | system (pipeline) | BEq+ | statement autoformalization | no (weaker retrieval) |
| DDR (2511.11990) | generated decl name | learned + existence check | full library (suffix array) | system | accuracy/stability | statement autoformalization | no |
| TheoremGraph (2606.25363) | statement | learned (embeddings + LLM judge) | 388k decls, 25 projects | neither (dataset) | LLM judge on matches | retrieval study only | no |
| LeanSearch-v2 (2605.13137) | declaration (premise) | learned (embed+rerank), formal-only | all Mathlib | system (search API) | none | fixed prover loop | no-tools, not unjoined |
| ProofNetVerif (2406.07222) | n/a (evaluation) | n/a | n/a | n/a | BEq+ + 3,752 labels | metric validation | n/a |
| **Wikibrain (this study)** | **cell (concept ≡ decl ≡ article ≡ DB page)** | **curated, human+AI-moderated, persistent** | 7,439 Mathlib decls joined; existence index = all of Mathlib | **agent (MCP tools)** | exploratory uncalibrated judge (gap) | grounded typecheck, retrieval, kernel-verified proving | **yes — unjoined-corpora arms** |

The table encodes one positioning fact. Every 2025–26 concept-grounding
system joins per-query with learned components and lacks an unjoined
control; the Brain occupies the persistent-curated-database quadrant,
and this study's D-versus-C/E contrast is, as far as we could verify,
the only causal test of the join itself.

## 3. The Brain artifact and experimental design

This section characterizes the artifact under test, then the
preregistered design, its deviations, and what we know about execution
fidelity and holdout status.

### 3.1 The system under test

The table below characterizes the Brain under test, **post hoc, from
the 2026-08-01 build** (recompute: `bench/analysis/brain_artifact.py`).
No experiment phase queried this exact build, and the per-phase build
commits were not recorded — a bookkeeping gap. What we can bound is
this: the fresh-set holdout was checked against the 2026-07-03 snapshot
universe, and the one documented mid-campaign change to the graph is
the 2026-07-18 verified discovery fold of §3.5, which absorbed a single
fresh gold (fresh_025). The table therefore characterizes the artifact
family the arms queried, not any single run's view of it.

| Quantity | Value (2026-08-01 build; Mathlib pins 2026-07-04 harvest + 2026-07-18 discovery folds) |
|---|---|
| Cells (atoms) | 20,880 (4,095 multi-organ; largest cell 28 organs) |
| Organs | 29,051 — decl 19,611 · concept 2,969 · article 728 · external-DB page 3,467 · arXiv statement 2,276 |
| Mathlib declarations joined | 7,439 distinct = **2.34%** of 317,257 doc-gen4 Mathlib names; 1,197 share a cell with ≥1 concept |
| Concepts | 2,969 distinct QIDs in cells (+99 field concepts held at folder altitude) |
| Synapses | 100,797 aggregated edges carrying 132,708 bonds — depends 85,652 · mentions 12,361 · links 11,597 · invocation 9,204 · co-page 7,304 · cites 3,532 · other 3,058 |
| Provenance, organ attachments | human 5.5% · ai-moderated 30.9% · ai 0.1% · automated 63.6% |
| Provenance, synapse traces | human 0.3% · ai-moderated 1.8% · automated 98.0% |
| External DBs (cell-anchored pages) | MathWorld 1,360 · nLab 564 · ProofWiki 426 · EoM 409 · Stacks 268 · PlanetMath 250 · 5 others |
| Benchmark-target coverage | MathlibQR golds 65/171 (38.0%) · MathlibMPR premises 47/341 (13.8%) · fresh-100 golds 1/100 |

Construction is a deterministic, no-LLM build (external-DB ingest +
Wikidata/Mathlib harvests → organ layer → cell merge → synapse
aggregation); AI-generated joins enter only through verified proposal
folding, and acceptance suites gate publication. The coverage row
matters twice below: the Brain is a *map* of Mathlib (2.34% of names
joined, existence index over all of them), and it contains the gold
answer for only 38% of the concept-retrieval benchmark (§5).

### 3.2 Preregistered design

The preregistered hypotheses were that an agent holding the joined
dictionary beats one given informal and formal search separately (P1),
at no higher cost (P2), with better grounding (P3). Five arms ran with
identical model, prompt, and budgets within each phase; only the tool
manifest differs:

| Arm | Tools | Isolates |
|---|---|---|
| A `no_tools` | none | floor / memorization |
| B `informal` | Wikipedia + nLab search/fetch | informal reasoning alone |
| C `formal` | loogle + ripgrep + source read (Mathlib checkout) | the LeanSearch-class status quo |
| D `wikibrain` | the Brain MCP — all eight tools | the join **+ the existence verifier** (not separable here) |
| E `B+C unjoined` | B's and C's tools, no join | the unjoined-corpora control |

Two design facts matter up front. First, D alone holds `decl_exists`,
and hallucination-free citation is a conjunct of the primary metric —
so D versus E measures the *bundled Wikibrain package*, not the join
(715 `decl_exists` attempts on the 100 fresh tasks; 682 reached the
tool, 681 succeeded — the other 33 called a nonexistent bare tool
name). Second, E failed its manipulation check: 4 informal-tool touches
across its 100 fresh runs against B's 345 — it behaved as a
formal-search agent with a larger manifest.

Tier 1 ran `claude-haiku-4-5` on ProofNet# (371 tasks; 341-task eval
split; pinned fork of arXiv:2406.07222) and on a 100-task **fresh set**
drawn from theorems merged into Mathlib master 2026-07-03→07-16, graded
by REPL typecheck on pinned toolchains.

**What a fresh task is.** Candidates were every theorem/lemma line added
to Mathlib master between the Brain snapshot (2026-07-03) and the
2026-07-16 head `9944fe29` — 1,326 added lines across 44 commits,
pre-filtered to the 187 carrying an author-written docstring, of which
100 were kept (bespoke helpers and statements with unrecoverable
hypotheses dropped). Each task's gold is the exact Mathlib master
statement (`:= sorry`), elaboration-checked on the fresh pin; the agent
sees only the informal statement and must produce a Lean statement of
the same theorem. The informal statements are identifier-stripped
natural-language paraphrases of the golds' docstrings, written by a
Claude Opus 4.8 agent session. That session shares a vendor with the
subject models, and docstring-derived NL for formula lemmas is
inherently close to the Lean: both are validity caveats we cannot
remove. Held-out guarantee: every kept declaration is absent from both
the pinned 388k-declaration TheoremGraph universe and the Brain's node
set. One honest gap remains — the construction script was never
committed, so the docstring→NL step is documented by the commit record
and `fresh_tasks.stats.json`, not by re-runnable code. Determinacy was
screened post hoc by two independent AI annotators (86 and 83 of 100
judged determinate; 74 by both — the primary subset, §S1).

The exploratory second phase
("v2", `claude-sonnet-5`) ran retrieval arms N (no tools), F (formal
search), W (the Brain), WF (union + a tool manual), and U (bare union)
on third-party-graded benchmarks (§5), and a one-shot proving
protocol on SorryDB (§6). Each phase used one model and one seed per
(task, arm) pair.

### 3.3 Deviations from preregistration

The complete 25-component execution inventory is Supplement §S1; six
deviations change interpretation:

| # | Deviation | Consequence |
|---|---|---|
| 1 | Primary endpoint faithful@budget (BEq+ equivalence) never graded in campaign; the grader was a stub | zero confirmatory results; a post-review blind LLM-judge pass over all 500 fresh outputs is the exploratory substitute (§4.2) |
| 2 | Second model class on the primary set not run | Tier-1 all-Haiku, v2 all-Sonnet; capability-band generality unknown |
| 3 | Three reseeds + pass@k not run | one seed everywhere; no run-to-run variance estimate |
| 4 | tokens-to-solve never computed | P2 (the cost half of the success criterion) untested |
| 5 | 30-turn budget advisory only (stated in prompt, no CLI cap) | overruns on the repaired rows C 50 / D 38 / E 48 of 100, max 88 turns (the as-run count for E was 32 — its 31 outage rows died at turns=1 and masked overruns); sensitivity in Supplement §S5 |
| 6 | Arm-E fresh block: 31 contiguous infrastructure-failed rows | repaired by rerun (below); outage-basis tables in Supplement §S2 |

### 3.4 Execution fidelity

Four infrastructure defects were caught by cross-checking transcripts
against results; none was visible in topline files.

(1) *The 429 tail.* Rows fresh_069–099 — 31 contiguous tasks at the
tail of E's sequential block — died with session-limit 429 errors and
were rerun on 2026-07-27 with E's exact code path against a read-only
archive of the same tree (commit `19a90209`).

(2) *The MCP non-blocking attach leak.* The agent CLI attaches its MCP
servers without blocking the first model turn, so a run can "complete"
having silently never had tools; the repair driver detects this from
the stream-json init event and retries.

(3) *The cold-start race* — found and repaired. Under concurrent batch
cold starts the same non-blocking attach silently detooled whole waves:
13/49 rows of the first U launch and 175/810 zero-tool rows of the
original QR-810 F grid carried the pending-at-init signature (12/12
sampled confirmed). The v2 runner now condemns such rows and staggers
its first wave (commit `834a130a`), and all 194 affected rows (F: 175
QR + 15 MPR; W: 2 + 2 — W's Brain server is a fast-attaching local
worker, so the race fell almost entirely on F) were rerun
attach-verified under the fixed harness, the originals byte-preserved
in `bench/v2/runs/agent/race_condemned_archive/`. The final §5 grid
audits **zero race rows** in every arm × benchmark (commits `99a2075f`,
`f041928a`, `dd3eb689`). The bias had run *against* F — race rows
behaved as no-tools — that is, against our own tooling conclusion.

(4) *The REPL silent fallback.* The typechecker silently fell back from
the persistent REPL server to a ~60 s single-shot Lean boot on the
Tier-1a pin, tainting a scoring pass with timeout failures until it was
caught and rerouted with honest labels (commit `2a9f6b91`).

**Could the Tier-1 headline grids have run toolless?** The Tier-1 rows
predate the init-signature capture and record no attach event, so the
audit is indirect: nonzero tool calls prove attachment, and a zero-tool
row is the worst case. On the fresh set the tooled Lean arms show
C 0 / D 1 / E 1 zero-tool rows (each a single-turn, ~85 s row — the
plausible silent-detooling suspects, ≤1% per arm), and on eval-341
C 0 / D 0 / E 4. Arm B's 23 fresh zero-tool rows are single-turn
answers consistent with declining a rarely useful wiki toolkit rather
than detooling; arm D additionally preflighted its MCP server and
aborted on failure. The Tier-1 grids are thus not attach-verified
row-by-row as the v2 grid is, but worst-case contamination is one row
per tooled Lean arm on the fresh set (`v3_gate_fixes.json`).

### 3.5 Fresh-set isolation status

The fresh set is **post-Brain-index**: held out from the Brain's
declaration universe and node set — verified per-gold, with one leak.
`fresh_025`'s gold entered the Brain through a later (2026-07-18)
verified discovery fold, a second Mathlib-entry channel the holdout
check did not cover; the strict claim is **99/100**. The set was *not*
held out from the formal-search arms' sources: 51/100 golds are exposed
in the checkout C and E read (own-module basis; full strata Supplement
§S4). Exposure interacts with the choice of endpoint, and §4.2 measures
that interaction directly.

*Terminology used below.* A **verified rename** is a dead→current name
pair (e.g. `Basis`→`Module.Basis`) from our rename ledger, each pair
checked against the current declaration index; the union oracle accepts
either side. A `decl_exists` attempt is **successful** when the call
returns a verdict, not when the name exists. The judge's **strict**
grade requires the same proposition under the same hypotheses;
**evaluated** requires mathematical equivalence at high confidence.
**fair-810** is the LSv2-released MathlibQR subset whose 171 gold
declarations lie in the universe shared by all compared systems, times
up to six paraphrase styles per gold = 810 queries.
**R@10** (recall at ten) scores whether a gold declaration appears in a
ranked list's top ten; **nDCG@10** (normalized discounted cumulative
gain) additionally rewards ranking it early.
**group-recall@10** (MPR): each task's gold is a set of premise groups
of interchangeable names; the score is the fraction of groups with ≥1
member in the top 10. An **RD** is a risk difference between two arms'
rates; a **Wilson 95% CI** is the binomial confidence interval we quote
for single proportions; an exact **McNemar** test compares paired arms
on their discordant tasks. A **sign count** such as 34/35 counts
gold-declaration clusters where one arm's mean beats the other's (ties
dropped). **Own-module** exposure counts a gold as exposed when its
declaration name appears as a header in the task's own module file in
the pinned checkout.

## 4. Statement formalization results (claude-haiku-4-5, fresh-100)

### 4.1 Grounded typecheck under the repaired instrument

The register label comes first: the preregistered primary endpoint
(faithful@budget) was never graded (§3.3, deviation 1), so grounded
typecheck is a post-hoc component of it under a post-hoc analysis plan —
corrective-exploratory, not confirmatory.

The primary metric is the **grounded typecheck rate**: the run produced
a declaration, cited zero names absent from a union oracle (doc-gen4
declaration data ∪ verified renames), and the declaration typechecked on
the pinned toolchain. It does not establish that the statement
formalizes the prompt — that is §4.2's question. The citation surface
is the run's final Lean block (`output_lean`, the last fenced Lean
block of the final answer — nothing else in the transcript is scanned),
swept by a capitalized-identifier heuristic with no Lean parser, so it
also picks up names in comments and docstrings inside that block; the
oracle then matches exact fully-qualified strings. That is why a
typechecking run can still carry a flagged citation: a name in a
comment is never elaborated, and an idiomatic namespace-short name
under `open` elaborates while failing exact-string lookup — the
repair's main business, below. §4.3's run-level rates inherit the same
surface.

**The instrument had to be repaired first.** A blinded audit of the
hallucination oracle (60 distinct cited names, seeded and stratified,
graded against raw `git grep` evidence with verdicts sealed) found the
flagged class only **13.3% precise** [5.3, 29.7]: most flags were real
declarations cited by their conventional namespace-short names, plus
extractor noise. Five mechanical repair rules (drop comment, import, and
self-declaration tokens and single-letter dot-notation heads; resolve
single-segment namespace prefixes) agree with the blinded truth on
59/60. The rules were formulated after that audit unsealed, so 59/60 is
an in-sample fit; a second, disjoint 40-name blinded sample (seed
20260802) measured the repair out of sample: binary agreement 36/40 =
90% [77, 96], recall on true fabrications 3/3, but flagged-class
precision only 43–57%. The raw oracle's invalidity replicates almost
exactly (15% precision held-out), the repair transfers most of its
value, and a repaired flag remains closer to a coin toss than a
confirmation — so repaired hallucination rates are **upper bounds** on
true rates. The residual false-flag modes are namespace-style-correlated
and land on the free-text tool arms, so they cannot manufacture D's
advantage (protocol and error census: Supplement §S3). The original
bias was directional — false flags hit the control arms
hardest — and repairing it moves C by +8 successes, E +7, D +6, A +1,
B 0 (per-row detail Supplement §S3). All headline numbers below use the
repaired instrument; raw-instrument tables, the outage bases
(errors-as-failures and completed-69), and the earlier Wald/McNemar
mismatch are Supplement §S2.

| arm | grounded typecheck (repaired) | Wilson 95% CI |
|---|---|---|
| A no tools | 21/100 = 21.0% | [14.2, 30.0] |
| B informal | 22/100 = 22.0% | [15.0, 31.1] |
| C formal | 33/100 = 33.0% | [24.6, 42.7] |
| D wikibrain | **48/100 = 48.0%** | [38.5, 57.7] |
| E B+C unjoined | 37/100 = 37.0% | [28.2, 46.8] |

The 100 tasks are not independent draws: they come from **44 source
commits** and 57 files, with conspicuous sibling families (a 9-task
sum–integral family, an 8-task bounded-variation file). Our single
main-text inferential framework is therefore the **commit-clustered
paired bootstrap** (44 clusters, B=10,000; interval and p-value read off
the same resampling distribution):

| pair | RD | 95% CI | p |
|---|---|---|---|
| D − A | +0.27 | [+0.131, +0.395] | **0.0004** |
| D − C | +0.15 | [−0.023, +0.302] | 0.100 |
| D − E | +0.11 | [−0.089, +0.279] | 0.304 |

E versus A is also robust under the same framework: +0.16
[+0.04, +0.29], clustered p=0.012 (exact McNemar 24/8, p=0.007). The
same clustered framework was run on every A-floor and D/E/C contrast
reported as significant in §§4.2–4.3 below, and all survive it
(`v3_gate_fixes.json`); the remaining p-values there are unclustered
McNemars, labeled as such — anti-conservative for the two significant
B-comparisons, immaterial for the nulls.
The picture is plain: **tools-versus-none is solid, while between tool
packages the matched comparisons are inconclusive at this sample
size.** Clustering
is not pedantry here — on the raw instrument, 83% of D's net paired
advantage over E sits in 2 of the 44 commits, exactly the
pseudoreplication the clustered bootstrap absorbs. On the contaminated
ProofNet# eval split the arms sit within a few points of each other at
58–64% with the no-tools arm at 59.8% — consistent with benchmark
familiarity, though the fall to 21% on fresh tasks changes benchmark,
difficulty, domain, and recency at once and is not a clean memorization
quantification. The eval split was not re-graded under the repaired
oracle (280 affected rows; §7).

### 4.2 Semantic faithfulness (exploratory; uncalibrated judge)

The preregistered equivalence endpoint went ungraded in the campaign, so
after review 1 we ran a blind LLM-judge pass over all 500 fresh outputs
(judge `claude-sonnet-5`; sees the informal statement, the gold with its
binders, and the candidate; no arm identity, no tools; 0 errors;
self-consistency on a fixed 50-item re-grade: strict 98%, evaluated
100%). **The judge is uncalibrated — the preregistered 50-item human
calibration remains undone — so nothing in this subsection is
confirmatory.** The story has three layers.

**Layer 1 — the inversion.** On judge-evaluated equivalence
(mathematical equivalence at high confidence), the five arms score:
A 17.0% [10.9, 25.6] · B 16.0% [10.1, 24.4] · C 51.0% [41.3, 60.6] ·
D 19.0% [12.5, 27.8] · E 53.0% [43.3, 62.5]. D sits at the no-tools
floor (D-vs-A McNemar 7/5, p=0.77) while C and E sit far above it
(D-vs-E clustered −0.34 [−0.46, −0.22], p=0.0002). Taken alone this
says the unjoined arms produce far more faithful statements — and that
the Brain arm does not beat no tools at all on this endpoint.

**Layer 2 — the exposure explanation.** C and E hold source grep over a
checkout containing 51 of the 100 golds (§3.5), and on those tasks they
can simply transcribe: their evaluated-equivalence rates are .706 and
.686 on exposed tasks against .306 and .367 on unexposed ones, while D
is flat (.196/.184). The inversion concentrates exactly where the gold
was retrievable; it narrows sharply, but does not vanish, on the
unexposed half (Supplement §S11). The unexposed residual (C/E at
.31/.37 against D's ~.18) is not explained by exposure, and we cannot
exclude **verifier steering**: the generate-then-verify loop may pull D
toward statements that are existence-verified and typecheckable but
semantically weaker — groundedness traded against faithfulness. The
human grading queue below is where that hypothesis gets adjudicated.

**Layer 3 — the conjunction.** On grounded-typecheck ∧ judge-evaluated
— the closest available analogue of the preregistered faithful@budget —
the arms return to parity. Under the repaired typecheck leg
(`conjunction_repaired.py`; the judge ran before the oracle repair, so
Supplement §S11 reports both instruments): C 20, D 16, E 21 per 100
(raw leg: 16/15/18). Every between-tool contrast is null (D-vs-E
clustered p=0.25; D-vs-C p=0.50 and E-vs-C p=1.0, unclustered) and the
tools-versus-none contrasts strengthen — E-vs-A +0.13, clustered
p=0.0002; E-vs-B p=0.004 (unclustered); and D-vs-A crosses to +0.08,
clustered p=0.037. The instrument repair moves no conclusion here:
parity between tool packages, tools above the floor.

The transferable warning is the interaction itself: with contaminated
items in a task set, the apparent winner *flips with the endpoint* — D
leads on grounded typecheck, C/E lead on judged equivalence by
transcribing retrievable golds, and the conjunction returns parity. The
36 D/E-discordant tasks are queued for blinded human grading as the
first slice of the still-owed calibration.

### 4.3 Hallucinated citations

The table below counts runs with ≥1 repaired-oracle-flagged citation —
an upper bound on true hallucination, per the held-out precision in
§4.1 — at n=100 per arm:

| arm | runs with ≥1 hallucination | Wilson 95% CI |
|---|---|---|
| A no tools | 37/100 | [28.2, 46.8] |
| B informal | 30/100 | [21.9, 39.6] |
| C formal | 11/100 | [6.3, 18.6] |
| D wikibrain | **6/100** | [2.8, 12.5] |
| E B+C unjoined | 10/100 | [5.5, 17.4] |

Tools-versus-none survives everything: D-vs-A −0.31 [−0.45, −0.19],
clustered p=0.0002 (E-vs-A −0.27, clustered p=0.0002; D-vs-B p=3.9e-5,
unclustered). The between-tool contrasts dissolve: D-vs-E clustered
p=0.44, D-vs-C clustered p=0.31 — direction preserved, but n=100
cannot distinguish them. The
raw oracle had shown D roughly 3× better at citation level; most of that
gap was **citation style, not grounding**. D copies fully-qualified
names out of tool payloads (10 short-name reclassifications under
repair) while C and E write idiomatic namespace-short names under
`open` (65 and 71 reclassifications), which an exact-string oracle
falsely flags. All four fabricated names confirmed in the blinded
sample came from the arms without formal tools (A/B); none from C, D,
or E — and the held-out sample replicates this: all three of its
confirmed fabrications sit in A/B as well (§S3).

### 4.4 The preregistered factorial (join × verifier)

This subsection is the paper's one preregistered-confirmatory analysis —
the experiment both reviews demanded. We crossed the **join** (the
Wikibrain tool surface) with the explicit **existence verifier**
(`decl_exists`) over the same 100 fresh tasks and model, preregistering
design, endpoints, and analysis before any row ran
(`docs/research/BRIDGE-FACTORIAL.md`, commit `3658bd58`, 2026-08-07;
runs 2026-08-08, committed `ba35fe7f`). The four arms are E′ (neither;
arm E's exact toolset), X (verifier only), J (join only), and D′ (both;
arm D's exact toolset), with yoked interfaces — identical prompt, model,
and budget, empty built-in toolset, per-row tool-manifest validation
from the stream-json init event — one interleaved execution order, and
a mechanical 30-turn cap. Primary endpoint: grounded typecheck under
the repaired oracle (§4.1). Primary analysis: the same commit-clustered
paired bootstrap (44 clusters, B=10,000, preregistered seed), α=0.05
two-sided; the two main effects are the only confirmatory tests, the
interaction and all secondaries are preregistered-exploratory.

| arm | cell | grounded typecheck (repaired) | Wilson 95% CI |
|---|---|---|---|
| E′ | join−, verifier− | 31/100 = 31.0% | [22.8, 40.6] |
| X | join−, verifier+ | 41/100 = 41.0% | [31.9, 50.8] |
| J | join+, verifier− | 39/100 = 39.0% | [30.0, 48.8] |
| D′ | join+, verifier+ | 39/100 = 39.0% | [30.0, 48.8] |

**Both preregistered hypotheses are unsupported — the main effects are
null.** The JOIN main effect is **+0.030** (95% CI [−0.093, +0.139],
p=0.65): at this sample size the joined surface does not detectably
improve grounded typecheck beyond what explicit verification provides.
The VERIFIER main effect is **+0.050** ([−0.029, +0.121], p=0.23):
explicit generate-then-verify existence checking likewise shows no
detectable main effect. The interaction (exploratory) is **−0.100**
([−0.245, +0.047], p=0.20): the factors look redundant, not synergistic
— the verifier's pairwise gain appears only without the join (X−E′
+0.100, p=0.13) and vanishes on top of it (D′−J +0.000, p=1.0), exactly
the pattern the preregistered factor-purity caveat anticipated (the
join's outputs arrive already existence-verified, so J never lacked
verified names — what it lacked, checking model-written names, appears
to add nothing once the join is present). All six pairwise contrasts
are null (largest X−E′ +0.100, p=0.13; full grid Supplement §S15).

Secondaries agree. Run-level repaired hallucination is low and
indistinguishable everywhere (E′ 9, X 5, J 11, D′ 10 per 100; both
factor effects null) — with formal tools of any kind, flagged citations
are no longer where arms differ. Where the arms do differ sharply is
*production*: the join arms finished within budget (J 100/100 produced,
0 capped; D′ 93/100, 6 capped) while the unjoined arms burned their 30
turns (E′ 82/100 produced, 18 capped; X 78/100, 22 capped) — an
efficiency signature, not an accuracy one, and the reason the
factorial's rates are not directly comparable to §4.1's softer-budget
Tier-1 table (D′ 39% here vs D's 48% there; E′ 31% vs E's 37%).
Manipulation checks pass: X and D′ actually used the verifier (213
calls in 94/100 runs; 536 in 92/100), and the informal tools went
essentially untouched in E′/X (2 and 4 runs) — the same informal-arm
manipulation failure as Tier-1. Preregistered sensitivity cuts (raw
oracle, own-module exposure strata, dropping the 3 live-index-leaked
tasks, the 74-task both-annotator determinacy subset) all preserve the
nulls (§S15). The two judge-dependent secondaries (evaluated
equivalence and the conjunction) are staged — blinding scan green over
all 353 gradeable outputs — but not yet graded in this revision (the
judging CLI's authentication expired; §S15.4); nothing confirmatory
rests on them.

Execution fidelity: 400/400 rows terminal and attach-clean — 0 errors,
0 zero-tool rows, 0 turn-cap violations (46 capped rows are valid
per-prereg terminal rows scored on their extracted output), per-arm
condition hashes uniform, one interleaved order, $40.91, 2.9 h.
The preregistration file is byte-unchanged since commit `3658bd58` and
its deviations log is empty. Scoring was a separate later phase (prereg
§4.9); its one infrastructure incident — the typecheck REPL server died
mid-pass and 44 D′ rows silently fell back to bare-environment checks
(§3.4's defect-4 class, caught by an elapsed-time audit) — was repaired
by re-typechecking the whole arm against a restarted identity-gated
server, with all 48 healthy-window verdicts reproducing exactly
(`factorial_scored.json` `retc_provenance`).

What the factorial settles: arm D's Tier-1 margin cannot be attributed
to either bundled ingredient — under a yoked interface neither the
curated join nor the explicit verifier moves grounded typecheck
detectably on its own, and D′ does not separate from the strongest
single-factor arms. The tools-versus-none result (§4.1) stands;
between tool packages, the causal decomposition now confirms what the
observational contrasts suggested: nothing here favors the join.

## 5. Retrieval scope and ablations (claude-sonnet-5, exploratory)

The v2 phase uses graders that predate the arms: third-party expert gold
labels from the LeanSearch-v2 release (CC BY 4.0). MathlibQR fair-810
poses 810 paraphrase-style queries over **171 distinct gold
declarations**, so all inference is declaration-clustered; MathlibMPR
poses 69 post-cutoff premise-retrieval tasks, one per PR. All agent
rows below are the **race-repaired grid** (§3.4): the 194 condemned
F/W rows were rerun attach-verified, and the final grid audits
race-free (`bench/analysis/grid_repaired.py`). **WF is a
post-hoc, benchmark-informed condition**: its prepended manual distills
measurements from the same evaluation queries it was then scored on
(manual verbatim and commit timeline: Supplement §S8). Published rows
are the numbers the respective papers report for their own systems —
single-call retrievers on the QR anchors; on MPR, LSv2's reasoning
mode is itself an iterative sketch-retrieve-reflect pipeline, not a
single call — context, not controlled comparison. (Anchor provenance:
0.775, 0.780, 0.623, and 0.461 are verified in
`related_work_notes.md`; TheoremGraph nDCG 0.548, TheoremGraph MPR
0.165, and DIVER 0.380 are recorded from the papers' tables in the
pre-run design notes, `docs/research/BRIDGE-V2-BENCHMARKS.md`.)

**MathlibQR fair-810 — concept retrieval:**

| system | R@10 | decl-clustered 95% CI | nDCG@10 | 95% CI | $/query | tool calls/query |
|---|---|---|---|---|---|---|
| published: TheoremGraph | 0.775 | — | 0.548 | — | — | 1 |
| published: LSv2 retriever+reranker | 0.780 | — | 0.623 | — | — | 1 |
| system-mode `brain_bridge` (one call, no LLM) | 0.036 | — | 0.031 | — | ~0 | 1 |
| agent N (no tools) | 0.633 | [0.581, 0.684] | 0.598 | [0.547, 0.647] | 0.08 | 0 |
| agent F (formal) | 0.846 | [0.808, 0.880] | 0.809 | [0.771, 0.845] | 0.14 | 2.8 |
| agent W (Brain) | 0.816 | [0.767, 0.862] | 0.781 | [0.731, 0.827] | 0.20 | 3.5 |
| agent U (bare union, no manual) | 0.830 | [0.789, 0.868] | 0.799 | [0.758, 0.838] | 0.20 | 3.1 |
| agent WF (union + manual; post-hoc) | 0.885 | [0.849, 0.919] | 0.839 | [0.801, 0.874] | 0.21 | 4.2 |

Declaration-clustered paired contrasts on the repaired grid: **F − W
is a null** (R@10 +0.030 [−0.015, +0.076], Wilcoxon p=0.086, sign
34/35) with different textures — W wins the special-case paraphrase
style (nDCG 0.522 vs 0.356), F the Lean-syntax styles. WF − F is
+0.040 [+0.015, +0.064] (p=0.0097) and WF − W +0.069 [+0.031, +0.108]
(p=0.0003), but WF carries the post-hoc label — and the U ablation now
decomposes it: U, holding the identical W ∪ F toolset with no manual,
is indistinguishable from F (U − F −0.016 [−0.039, +0.005], p=0.13),
while WF − U is +0.056 [+0.032, +0.080] (p=1.3e-4). Note also that
repaired F alone already exceeds both published QR anchors (R@10 0.846
vs 0.780/0.775) — single-call systems against a multi-call agent, so
context, not a controlled comparison. The
system-mode row is the API deficiency in one number: a single
`brain_bridge` call scores 0.036 where an agent iterating over the same
API reaches 0.816 — the free-text entry point is a label/alias
resolver, not a semantic retriever.

**N's format failures explain little.** 143/810 N rows returned no
ranked list. A maximally lenient extraction pass applied identically to
every arm closes only 5.9–8.1% of N's deficit (0.633 → 0.648), and even
the oracle ceiling — every empty N row scored as a rank-1 hit — leaves
N below every tooled arm. N's empty rows are dominated by hallucinated
tool calls, not wrapper noncompliance (Supplement §S6).

**Where the answers actually came from.** For every QR hit we traced the
chronologically first entry of the gold name into the transcript
(full-transcript resolved pass):

| arm | hits | surfaced by a tool | guessed, then verified | pure memory |
|---|---|---|---|---|
| W | 661 | 69 (10.4%) | 582 (88.0%) | 2 (0.3%) |
| WF | 717 | 273 (38.1%) | 424 (59.1%) | 12 (1.7%) |
| F | 673 | 151 (22.4%) | 170 (25.3%) | 344 (51.1%) |

(The remaining ~1% appear in the query itself. This trace pass
predates the race repair: F's row includes its 175 zero-tool race
rows, whose hits count as "pure memory", so F's memory share is an
as-run upper bound; W and WF are materially unaffected — 2 and 0 race
rows.) W's score is not the
graph surfacing answers: it surfaced 10.4% of hits, and it could not
have surfaced most — the Brain holds only 65/171 (38%) of the QR golds.
Instead the model generates candidates and `decl_exists` confirms them,
and the loop genuinely filters: in 81% of W's verified hits the oracle
also *rejected* at least one candidate along the way. **On these
benchmarks the join's measured value is verification and routing, not
content** — convergent with DDR's finding that existence-checked
generation beats selection-RAG. WF's higher surfaced share (38.1%) is
the routing half: it greps and loogles when holding syntax, bridges when
holding words.

**MathlibMPR — premise retrieval** (group-recall@10, task-bootstrap
CIs): N 0.203 [0.131, 0.282] · W 0.272 [0.196, 0.354] · F 0.547
[0.463, 0.632] · U 0.549 [0.464, 0.633] · WF 0.557 [0.472, 0.642];
published anchors LSv2 reasoning-mode 0.461, DIVER 0.380, TheoremGraph
0.165; system-mode Brain 0.000. A generic Sonnet agent with grep and
loogle *exceeds* the specialist system's reasoning mode (0.547 vs
0.461) —
the as-run 0.453 had been deflated by F's 15 race rows. W trails F by
27 points (F − W +0.275 [+0.185, +0.369], p=2.3e-6) — the
preregistered concept ≠ premise boundary measured on our own tools,
and the one decisive between-package retrieval contrast, in formal
search's favor. The as-run grid's marginal WF − F advantage (+0.104,
Wilcoxon p=0.049) was a race artifact: repaired, WF − F is +0.010
[−0.068, +0.088] (p=0.73) and WF − U +0.009 (p=0.82) — neither the
manual nor the union adds anything on MPR.

**The bare-union ablation and the final contrast set.** U holds the
identical W ∪ F union toolset as WF with no manual, run entirely under
the condemn-and-retry protocol with a staggered first wave (879
attach-verified rows, 0 errors; its first launch is what surfaced the
cold-start race, §3.4). The repaired-grid contrasts
(declaration-clustered on QR, task-paired on MPR;
`grid_repaired.py`, seed 20260727, B=10,000):

| contrast | metric | diff | 95% CI | excl. 0 | Wilcoxon p |
|---|---|---|---|---|---|
| WF − F | QR R@10 | +0.0395 | [+0.0148, +0.0636] | **yes** | 9.7e-3 |
| WF − F | QR nDCG@10 | +0.0305 | [+0.0099, +0.0515] | **yes** | 6.4e-3 |
| WF − F | MPR gR@10 | +0.0101 | [−0.0679, +0.0882] | no | 0.73 |
| WF − U | QR R@10 | +0.0556 | [+0.0320, +0.0802] | **yes** | 1.3e-4 |
| WF − U | QR nDCG@10 | +0.0402 | [+0.0187, +0.0628] | **yes** | 1.2e-3 |
| WF − U | MPR gR@10 | +0.0085 | [−0.0664, +0.0833] | no | 0.82 |
| U − F | QR R@10 | −0.0161 | [−0.0385, +0.0050] | no | 0.13 |
| U − F | QR nDCG@10 | −0.0097 | [−0.0294, +0.0095] | no | 0.50 |
| U − F | MPR gR@10 | +0.0017 | [−0.0725, +0.0713] | no | 0.83 |
| F − W | QR R@10 | +0.0297 | [−0.0150, +0.0756] | no | 0.086 |
| F − W | QR nDCG@10 | +0.0283 | [−0.0110, +0.0684] | no | 0.28 |
| F − W | MPR gR@10 | +0.2747 | [+0.1851, +0.3688] | **yes** | 2.3e-6 |

The decomposition is clean. On concept retrieval the active ingredient
is the manual — a test-set-tuned upper bound (§S8), not a portable
prescription — while on premise retrieval formal tools are the entire
story and the bare union is inert everywhere (U − F null on all three
metrics). Repaired F alone already exceeds the published anchors on
both benchmarks (QR R@10 0.846 vs 0.780; MPR 0.547 vs 0.461).

## 6. Downstream proving: SorryDB (claude-sonnet-5, exploratory)

SorryDB (arXiv:2603.02668) serves unsolved `sorry`s from live Lean
repositories; success is the repository *building* with your proof —
kernel-graded, nothing to memorize. Our refrozen split holds 171 tasks
across 10 repositories; the protocol is one-shot and build-free (goal
state + a ±60-line window; explicit honest-abstention rule), and every
one of the 203 candidate proofs has a definitive verdict — 183 kernel
pass/fail, plus 19 unspliceable rows and 1 verify-timeout that never
reached the kernel. (Denominators reconciled: 171 frozen tasks; run
rows N 168 / F 169 / WF 171 — 3 N and 2 F runner losses; candidates
N 58 + F 71 + WF 74 = 203.) Results: N 2/171
= 1.2% [0.3, 4.2] · F 9/171 = 5.3% [2.8, 9.7] · WF 10/171 = 5.8%
[3.2, 10.4] (Wilson); repo-clustered bootstrap CIs are [0.0, 2.7] /
[0.0, 10.7] / [0.0, 12.2] — F's and WF's widen, while N's upper bound
tightens because both N successes sit in 2 of the 10 repo clusters.

**WF versus F is indistinguishable**: one
proof apart, exact McNemar 4/3 p=1.0, repo-clustered difference +0.58pp
[+0.0, +1.85] with 34% of resamples at or below zero — and WF routed
94% of its tool calls to formal search anyway, so SorryDB says
essentially nothing about the Brain. The Brain-only arm W, present in
every other phase, was never run here — a design gap, and precisely
the arm that would have made the phase informative about the package
contrast. The proving phase is therefore uninformative about the
Brain, not evidence of no effect. Tools-versus-none is descriptive
but consistent: the no-tools arm burns its budget (70 of its 168 rows
produce nothing), while tool arms raise proving four-to-five-fold and
double honest abstention — with only 10 repo clusters we leave it
descriptive.

The benchmark itself decayed: the original freeze lost 74
of 164 tasks (45%) in about six months to rewritten upstream history —
pinned commits force-pushed or rebased out of existence. The rot was
invisible to cheap checks, because GitHub's archive endpoint answers a
preflight HEAD request with 200 for dead commits; only actually
fetching every pin reveals it (full narrative and bookkeeping:
Supplement §S7, §S9).

## 7. Limitations and discussion

**Statistical scope.** One model per phase, one seed per (task, arm), no
run-to-run variance estimate anywhere. n=100 tasks in 44 commit clusters
is underpowered for between-package contrasts — the D−E interval spans
−0.09 to +0.28 — and nothing in this paper ranks tool packages in the
join's favor; the one decisive between-package contrast (F over W on
premise retrieval, §5) runs the other way and is exploratory. The
ProofNet# eval split was not re-typechecked under the repaired oracle:
280 raw-flagged, repaired-clean rows (A 39, B 45, C 68, D 47, E 81) are
hard per-arm upper bounds on the gains there, and the instrument bias
direction (against C/E) applies to that table too.

**Endpoint validity.** The preregistered semantic endpoint has still
only been graded by an uncalibrated LLM judge that shares a vendor with
the subject models; the human calibration and the 36-task discordant
queue are pending. Grounded typecheck is a grounding measure, not a
faithfulness measure, and §4.2 shows the two can rank arms oppositely.
The repaired hallucination oracle's flagged class is only 43–57%
precise out of sample (recall 3/3; §4.1, §S3), so all per-arm
hallucination rates are upper bounds on true rates.

**Attribution.** D bundles the join, the verifier, curated metadata, and
a distinct interface; the E repair ran later than D, so a time confound
remains on that contrast; E failed its manipulation check. The 2×2
factorial (§4.4) has since decomposed the bundle under a yoked
interface: neither the join nor the verifier shows a detectable main
effect, D′ does not reproduce D's 48% (39% under the stricter
interface), and the join's remaining measurable signature is
within-budget production efficiency, not grounded accuracy. What the
factorial cannot separate is content from interface (its own
preregistered limit), and its judge-graded secondaries remain
pending (§S15.4).

**A warning for the field.** The contamination-by-endpoint interaction
(§4.2) generalizes: a contaminated task set does not merely inflate
scores, it can *invert rankings* when one endpoint rewards transcription
and another rewards grounded generation. Post-cutoff sets decay, and a
holdout claim must name every index it is held out from — ours missed
two (the mutable checkout; our own later discovery-fold channel).

**Conflict and AI-use disclosure.** The author built and operates the
evaluated system. The study — runs, graders, and the corrective
analyses in this revision, including the blinded protocols — was
executed by AI agents (Claude Code) under the author's direction, with
human review of design and conclusions. The judge model and subject
models are from the same vendor as the executing agents.

## 8. Conclusion

What this study establishes: giving a Lean agent formal-tool access
lifts grounded typecheck output well above the no-tools floor (+16 to
+27 points; clustered p=0.0004 for the Brain arm) and collapses
flagged citation hallucination (6–11% of runs versus 30–37%, upper
bounds under the repaired oracle); the
sharpest mechanism signal is the existence verifier's
generate-then-verify loop, whose value survives every correction and
converges with independent work (DDR). What it does not establish: that
the curated join beats the same corpora unjoined — on grounded
typecheck, judged equivalence, retrieval, or kernel-verified proving,
every between-package contrast in the join's favor is inconclusive
once instruments are repaired and clustering is respected; the one
decisive retrieval contrast (premise retrieval, F − W +0.27, p=2.3e-6)
favors plain formal search; the bare-union ablation shows the union
itself inert and the WF gain manual-driven; and the join's retrieval
value measured here is verification and routing rather than curated
content.
The decisive experiment both reviews demanded — the preregistered 2×2
factorial {join, no join} × {verifier, none} on the same frozen
post-cutoff set — has now been run (§4.4), and it closes the
attribution question in the negative: at n=100 tasks per cell, neither
the curated join (+0.03, p=0.65) nor explicit existence verification
(+0.05, p=0.23) detectably raises grounded typecheck on its own, the
two are redundant rather than synergistic, and no arm combination
separates from the strongest single factor. What remains open is what
this study could never grade: human-calibrated semantic equivalence —
the judge calibration and the 36-task human queue are the outstanding
work.

## References

- CRAMF — Lu et al., *Automated Formalization via Conceptual
  Retrieval-Augmented LLMs*, arXiv:2508.06931 (preprint).
- Aria — Wang et al., *Aria: An Agent for Retrieval and Iterative
  Auto-Formalization via Dependency Graph*, arXiv:2510.04520 (ICLR 2026
  poster; numbers quoted from v2, 2026-07-02).
- DRIFT — Zhang et al., *DRIFT: Decompose, Retrieve, Illustrate, then
  Formalize Theorems*, arXiv:2510.10815 (ICLR 2026).
- DDR — Wang et al., *Improving Autoformalization Using Direct
  Dependency Retrieval*, arXiv:2511.11990 (preprint).
- TheoremGraph — Kurgan et al., *TheoremGraph: Bridging Formal and
  Informal Mathematics*, arXiv:2606.25363 (preprint).
- LeanSearch-v2 — Gao et al., *LeanSearch v2: Global Premise Retrieval
  for Lean 4 Theorem Proving*, arXiv:2605.13137 (preprint; also the
  source of the MathlibQR/MPR data and the published anchor rows).
- ProofNetVerif / BEq+ / ProofNet# — Poiroux et al., *Improving
  Autoformalization using Type Checking*, arXiv:2406.07222.
- SorryDB — arXiv:2603.02668, sorrydb.org.
