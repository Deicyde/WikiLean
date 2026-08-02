# Grounding Lean agents with a curated informal–formal join: a preregistered study with corrective reanalysis

**Report, version 3 (paper structure) — 2026-08-02.**
Jack McCarthy (WikiLean). Experiments and analyses were executed by Claude
Code agents under the author's direction (disclosure in §7).
Preregistration: `docs/research/BRIDGE-EXPERIMENT.md`, commit `0d36f266`
(2026-07-16). Versions 1 (2026-07-25) and 2 (2026-07-31) remain in git
history; the external reviews of both are preserved verbatim in
`docs/research/review/`. Everything moved out of the main text lives in
`docs/research/BRIDGE-SUPPLEMENT.md`, cited below as "Supplement §Sn".
All data and code are in the private preservation repository
`Deicyde/wikilean-bridge-experiment` (Supplement §S14); every number in
this paper is recomputable from a named file there.

## Abstract

We test whether a curated join between informal mathematical concepts and
formal Mathlib declarations — the WikiLean Brain, served to agents as
tools — improves language-model performance on Lean tasks, in a
preregistered five-arm study whose controls hold the same corpora
accessible but unjoined. After corrective reanalysis (a blinded audit
found our hallucination oracle's flagged class only 13.3% precise, biased
against the control arms; inference moved to a commit-clustered
bootstrap), the robust effect is tool access versus none: on 100
post-Brain-index tasks, the Brain arm's grounded typecheck rate is 48%
against a 21% no-tools floor (+0.27, 95% CI [+0.13, +0.40], p=0.0004),
and formal-tool arms collapse confirmed hallucinated citations to 6–11%
of runs against 30–37% (p=1.2e-7). Between tool packages nothing is
established: Brain versus unjoined tools is +0.11 (p=0.30). Trace
decomposition locates the join's measured value in verification and
routing, not retrieved content: 88% of the Brain arm's retrieval hits are
model-generated names its existence oracle verified, 10% surfaced from
the graph, and the graph holds only 38% of benchmark golds.
Kernel-verified downstream proving shows no package effect (p=1.0). We
also report transferable evaluation-design findings: instrument bias, a
contamination-by-endpoint interaction, four infrastructure failure modes,
and benchmark snapshot rot.

## 1. Introduction and contributions

A language-model agent working in Lean holds two corpora at once —
informal mathematics and the formal library — and it holds them loosely:
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
MCP tools: `brain_bridge` (free text in, existence-verified declarations
out), six graph and content tools, and `decl_exists`, a batch existence
check against an index of all of Mathlib, not just the joined subset.

This paper is the corrected report of that experiment, restructured
after two external reviews. Its contributions, stated honestly:

1. **The only causal-control design we could verify.** Among the
   2025–26 concept-grounding systems, this is the only evaluation whose
   control arms hold the same informal and formal corpora accessible but
   unjoined (§2, §3) — the design that isolates joining itself.
2. **A robust tools-versus-none effect on grounded output** (§4). The
   Brain arm exceeds the no-tools floor by 27 points of grounded
   typecheck rate under commit-clustered inference (p=0.0004), the
   unjoined-tools arm by 16 points (p=0.007), and formal-tool access
   collapses confirmed citation hallucination (p=1.2e-7). These survive
   instrument repair and clustering.
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

Equally plainly: **between-tool-package superiority is not established
at this sample size.** The Brain-versus-unjoined contrast is +0.11
(p=0.30) and Brain-versus-formal-search +0.15 (p=0.10); the
preregistered semantic endpoint was graded only by an uncalibrated LLM
judge; and the Brain arm bundles the join with the verifier, so
attribution between them awaits a factorial ablation (§8).

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
existence check — the closest published analog of `decl_exists`, and
independent convergence on existence verification of generated names as
the anti-hallucination mechanism (§5). Adjacent lines: TheoremGraph
(arXiv:2606.25363) builds statement-level graphs on both registers and
bridges them by embeddings — a dataset, not agent tooling;
LeanSearch-v2 (arXiv:2605.13137) is learned declaration-level premise
retrieval over a formal-only corpus with a fixed downstream prover loop;
ProofNetVerif/BEq+ (arXiv:2406.07222) anchors the semantic-grader axis
and is the ready-made instrument for the equivalence leg this study left
ungraded — on that axis Aria and DRIFT are ahead of us, and we say so.

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

The positioning fact this table encodes: every 2025–26 concept-grounding
system joins per-query with learned components and lacks an unjoined
control; the Brain is the persistent-curated-database quadrant, and this
study's D-versus-C/E contrast is, as far as we could verify, the only
causal test of the join itself.

## 3. The Brain artifact and experimental design

### 3.1 The system under test

The Brain snapshot the experiments ran against, characterized from the
repo artifacts (recompute: `bench/analysis/brain_artifact.py`):

| Quantity | Value (snapshot 2026-08-01; Mathlib pin 2026-07-04) |
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

Hypotheses: an agent holding the joined dictionary beats one given
informal and formal search separately (P1), at no higher cost (P2), with
better grounding (P3). Five arms, identical model, prompt, and budgets
within each phase; only the tool manifest differs:

| Arm | Tools | Isolates |
|---|---|---|
| A `no_tools` | none | floor / memorization |
| B `informal` | Wikipedia + nLab search/fetch | informal reasoning alone |
| C `formal` | loogle + ripgrep + source read (Mathlib checkout) | the LeanSearch-class status quo |
| D `wikibrain` | the Brain MCP — all eight tools | the join **+ the existence verifier** (not separable here) |
| E `B+C unjoined` | B's and C's tools, no join | the unjoined-corpora control |

Two design facts up front. D alone holds `decl_exists`, and
hallucination-free citation is a conjunct of the primary metric — D
versus E measures the *bundled Wikibrain package*, not the join (715
`decl_exists` attempts on the 100 fresh tasks, 682 successful). And E
failed its manipulation check: 4 informal-tool touches across its 100
fresh runs against B's 345 — it behaved as a formal-search agent with a
larger manifest.

Tier 1 ran `claude-haiku-4-5` on ProofNet# (371 tasks; 341-task eval
split; pinned fork of arXiv:2406.07222) and on a 100-task **fresh set**
drawn from theorems merged into Mathlib master 2026-07-03→07-16, graded
by REPL typecheck on pinned toolchains. The exploratory second phase
("v2", `claude-sonnet-5`) ran retrieval arms N (no tools), F (formal
search), W (the Brain), WF (union + a tool manual), and U (bare union)
on third-party-graded benchmarks (§5), and a one-shot proving
protocol on SorryDB (§6). One model per phase, one seed per (task, arm).

### 3.3 Deviations from preregistration

The complete 25-component execution inventory is Supplement §S1; six
deviations change interpretation:

| # | Deviation | Consequence |
|---|---|---|
| 1 | Primary endpoint faithful@budget (BEq+ equivalence) never graded in campaign; the grader was a stub | zero confirmatory results; a post-review blind LLM-judge pass over all 500 fresh outputs is the exploratory substitute (§4.2) |
| 2 | Second model class on the primary set not run | Tier-1 all-Haiku, v2 all-Sonnet; capability-band generality unknown |
| 3 | Three reseeds + pass@k not run | one seed everywhere; no run-to-run variance estimate |
| 4 | tokens-to-solve never computed | P2 (the cost half of the success criterion) untested |
| 5 | 30-turn budget advisory only (stated in prompt, no CLI cap) | overruns C 50 / D 38 / E 32 of 100, max 88 turns; sensitivity in Supplement §S5 |
| 6 | Arm-E fresh block: 31 contiguous infrastructure-failed rows | repaired by rerun (below); outage-basis tables in Supplement §S2 |

### 3.4 Execution fidelity

Four infrastructure defects were caught by cross-checking transcripts
against results; none was visible in topline files. (1) *The 429 tail*:
rows fresh_069–099 — 31 contiguous tasks at the tail of E's sequential
block — died with session-limit 429 errors and were rerun on 2026-07-27
with E's exact code path against a read-only archive of the same tree
(commit `19a90209`). (2) *The MCP non-blocking attach leak*: the agent
CLI attaches its MCP servers without blocking the first model turn, so a
run can "complete" having silently never had tools; the repair driver
detects this from the stream-json init event and retries. (3) *The
cold-start race* — found and repaired: under concurrent batch cold
starts the same non-blocking attach silently detooled whole waves —
13/49 rows of the first U launch and 175/810 zero-tool rows of the
original QR-810 F grid carried the pending-at-init signature (12/12
sampled confirmed). The v2 runner now condemns such rows and staggers
its first wave (commit `834a130a`), and all 194 affected rows (F: 175
QR + 15 MPR; W: 2 + 2 — W's Brain server is a fast-attaching local
worker, so the race fell almost entirely on F) were rerun
attach-verified under the fixed harness, the originals byte-preserved
in `bench/v2/runs/agent/race_condemned_archive/`; the final §5 grid
audits **zero race rows** in every arm × benchmark (commits `99a2075f`,
`f041928a`, `dd3eb689`). The bias had run *against* F — race rows
behaved as no-tools — i.e. against our own tooling conclusion.
(4) *The REPL silent fallback*:
the typechecker silently fell back from the persistent REPL server to a
~60 s single-shot Lean boot on the Tier-1a pin, tainting a scoring pass
with timeout failures until it was caught and rerouted with honest
labels (commit `2a9f6b91`).

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

## 4. Statement formalization results (claude-haiku-4-5, fresh-100)

### 4.1 Grounded typecheck under the repaired instrument

The primary metric is the **grounded typecheck rate**: the run produced
a declaration, cited zero names absent from a union oracle (doc-gen4
declaration data ∪ verified renames), and the declaration typechecked on
the pinned toolchain. It does not establish that the statement
formalizes the prompt — that is §4.2's question.

**The instrument had to be repaired first.** A blinded audit of the
hallucination oracle (60 distinct cited names, seeded and stratified,
graded against raw `git grep` evidence with verdicts sealed) found the
flagged class only **13.3% precise** [5.3, 29.7]: most flags were real
declarations cited by their conventional namespace-short names, plus
extractor noise. Five mechanical repair rules (drop comment, import, and
self-declaration tokens and single-letter dot-notation heads; resolve
single-segment namespace prefixes) agree with the blinded truth on
59/60. The bias was directional — false flags hit the control arms
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

E versus A is also robust: exact McNemar 24/8, p=0.007 (unclustered).
The picture: **tools-versus-none is solid; between tool packages the
matched comparisons are inconclusive at this sample size.** Clustering
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
(mathematical equivalence at high confidence), C scores 51.0%
[41.3, 60.6] and E 53.0% [43.3, 62.5] against D's 19.0% [12.5, 27.8]
(D-vs-E exact McNemar p=1.1e-9). Taken alone this says the unjoined
arms produce far more faithful statements.

**Layer 2 — the exposure explanation.** C and E hold source grep over a
checkout containing 51 of the 100 golds (§3.5), and on those tasks they
can simply transcribe: their evaluated-equivalence rates are .706 and
.686 on exposed tasks against .306 and .367 on unexposed ones, while D
is flat (.196/.184). The inversion concentrates exactly where the gold
was retrievable; it narrows sharply, but does not vanish, on the
unexposed half (Supplement §S11).

**Layer 3 — the conjunction.** On grounded-typecheck ∧ judge-evaluated
— the closest available analogue of the preregistered faithful@budget —
the arms return to parity. Under the repaired typecheck leg
(`conjunction_repaired.py`; the judge ran before the oracle repair, so
Supplement §S11 reports both instruments): C 20, D 16, E 21 per 100
(raw leg: 16/15/18). Every between-tool contrast is null (D-vs-E
p=0.30, D-vs-C p=0.50, E-vs-C p=1.0) and the tools-versus-none
contrasts strengthen — E-vs-A p=0.001, E-vs-B p=0.004, and D-vs-A
crosses to p=0.039. The instrument repair moves no conclusion here:
parity between tool packages, tools above the floor.

The transferable warning is the interaction itself: with contaminated
items in a task set, the apparent winner *flips with the endpoint* — D
leads on grounded typecheck, C/E lead on judged equivalence by
transcribing retrievable golds, and the conjunction returns parity. The
36 D/E-discordant tasks are queued for blinded human grading as the
first slice of the still-owed calibration.

### 4.3 Hallucinated citations

Run-level "≥1 confirmed hallucinated citation" under the repaired
instrument, n=100/arm:

| arm | runs with ≥1 hallucination | Wilson 95% CI |
|---|---|---|
| A no tools | 37/100 | [28.2, 46.8] |
| B informal | 30/100 | [21.9, 39.6] |
| C formal | 11/100 | [6.3, 18.6] |
| D wikibrain | **6/100** | [2.8, 12.5] |
| E B+C unjoined | 10/100 | [5.5, 17.4] |

Tools-versus-none survives everything: D-vs-A p=1.2e-7, D-vs-B
p=3.9e-5. The between-tool contrasts dissolve: D-vs-E p=0.45, D-vs-C
p=0.30 — direction preserved, but n=100 cannot distinguish them. The
raw oracle had shown D roughly 3× better at citation level; most of that
gap was **citation style, not grounding** — D copies fully-qualified
names out of tool payloads (10 short-name reclassifications under
repair) while C and E write idiomatic namespace-short names under
`open` (65 and 71 reclassifications), which an exact-string oracle
falsely flags. All four fabricated names confirmed in the blinded
sample came from the arms without formal tools (A/B); none from C, D,
or E.

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
are the numbers the respective papers report for their own single-call
systems — context, not controlled comparison.

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
loogle *exceeds* the specialist premise retriever (0.547 vs 0.461) —
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

The decomposition is clean: on concept retrieval the active ingredient
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
one of the 203 candidate proofs has a kernel verdict. Results: N 2/171
= 1.2% [0.3, 4.2] · F 9/171 = 5.3% [2.8, 9.7] · WF 10/171 = 5.8%
[3.2, 10.4] (Wilson); repo-clustered bootstrap CIs widen to [0.0, 2.7] /
[0.0, 10.7] / [0.0, 12.2]. **WF versus F is indistinguishable**: one
proof apart, exact McNemar 4/3 p=1.0, repo-clustered difference +0.58pp
[+0.0, +1.85] with 34% of resamples at or below zero — and WF routed
94% of its tool calls to formal search anyway, so SorryDB says
essentially nothing about the Brain. Tools-versus-none is descriptive
but consistent: the no-tools arm burns its budget (70 of 168 rows
produce nothing), while tool arms raise proving four-to-five-fold and
double honest abstention — with only 10 repo clusters we leave it
descriptive. One sentence on infrastructure: the original freeze lost
74/164 tasks (45%) in about six months to rewritten upstream history,
invisible until every pin is actually fetched, because GitHub's archive
endpoint answers preflight HEAD 200 for dead commits (full narrative
and bookkeeping: Supplement §S7, §S9).

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

**Attribution.** D bundles the join, the verifier, curated metadata, and
a distinct interface; the E repair ran later than D, so a time confound
remains on that contrast; E failed its manipulation check. Attribution
of D's tools-versus-none effect between join and verifier awaits the
2×2 factorial.

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
confirmed citation hallucination (6–11% of runs versus 30–37%); the
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
The decisive next experiment is the 2×2 factorial {join, no join} ×
{verifier, none} on a frozen post-cutoff set with human-calibrated
equivalence grading and frozen-snapshot search infrastructure.

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
