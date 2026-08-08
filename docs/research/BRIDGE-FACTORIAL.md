# BRIDGE-FACTORIAL — Preregistration: the 2×2 join × existence-verifier factorial

**Status: PREREGISTERED. This file is committed BEFORE any experimental row is
run; the commit hash of the commit introducing this file is the preregistration
timestamp.** Any later edit to this file is a deviation and must be logged in
§9 and stamped by its own commit.

This is the causal experiment both external reviews demanded
(`docs/research/review/REVIEW-1.md` "Run a factorial ablation — unjoined
baseline; existence control; join only; full Brain";
`docs/research/review/REVIEW-2.md` §4 "The minimum publishable ablation is the
2×2: unjoined / unjoined+verifier / join-without-verifier / full Brain …
arms run concurrently and randomized by task"). It follows the v3 report
(`docs/research/BRIDGE-REPORT.md`), whose §8 names this exact design as the
decisive next experiment, and embodies the execution lessons of its §3.4.

---

## 1. Question and hypotheses

The v3 report established tools-versus-none (D−A +0.27, clustered p=0.0004)
but could not attribute D's effect between the **join** (the curated
informal↔formal dictionary) and the **existence verifier** (`decl_exists`),
because arm D bundled both. This experiment crosses them:

- **H-JOIN**: the join has a positive main effect on grounded typecheck
  beyond what explicit existence verification provides.
- **H-VERIFIER**: explicit batch existence verification of model-generated
  names has a positive main effect on grounded typecheck.
- Interaction: estimated and reported, exploratory (no directional
  hypothesis).

**Success/failure criteria, stated plainly.** H-JOIN is supported iff the
JOIN main effect (§5) is > 0 with two-sided clustered-bootstrap p < 0.05.
A null (p ≥ 0.05) means: *at this sample size, the join per se does not
detectably help beyond verification* — and the report must say so, with the
CI as the precision statement. Same rule for H-VERIFIER. α = 0.05,
two-sided, no multiplicity correction across the two preregistered main
effects (each is a separate preregistered hypothesis; the interaction and
all secondary endpoints are exploratory and will be labeled as such).

## 2. Arms

2×2 over factors JOIN (the Wikibrain joined tool surface replaces the
unjoined wiki+formal search stack) × VERIFIER (`decl_exists` present).
Tools are resolved from the existing `bench/arms` configs with per-server
allowlists; server names match the `mcpServers` keys.

| Arm | Factor cell | MCP servers (config) | Model-visible tool manifest (exact, verified per row) |
|---|---|---|---|
| **E′** | join−, ver− | `wiki` + `formal` stdio (`bench/arms/mcp-Ep.json`, copy of arm-E's `mcp-E.json`) | `mcp__wiki__wiki_search`, `mcp__wiki__wiki_get`, `mcp__wiki__nlab_search`, `mcp__formal__loogle`, `mcp__formal__decl_grep`, `mcp__formal__decl_read` (6) |
| **X** | join−, ver+ | `wiki` + `formal` stdio + `wikibrain` http (`bench/arms/mcp-X.json`) | E′'s 6 + `mcp__wikibrain__decl_exists` (7) |
| **J** | join+, ver− | `wikibrain` http only (`bench/arms/mcp-J.json`, copy of `mcp-D.json`) | `mcp__wikibrain__brain_bridge`, `__brain_search`, `__brain_cell`, `__brain_transfer`, `__brain_neighborhood`, `__brain_snippets`, `__brain_filter` (7) |
| **D′** | join+, ver+ | `wikibrain` http only (`bench/arms/mcp-Dp.json`, copy of `mcp-D.json`) | all 8 live Wikibrain tools = J's 7 + `mcp__wikibrain__decl_exists` |

E′ is arm E's toolset exactly; D′ is arm D's exactly (the live server lists
exactly these 8 tools; `brain_unit` is a dispatch alias, not listed, and is
deny-listed wherever `decl_exists` is excluded as belt-and-suspenders).

**Tool-manifest mechanics (verified 2026-08-07 before this prereg was
written).** The installed claude CLI (v2.1.153) does NOT hide
non-allowlisted MCP tools from the model — with `--allowedTools` alone the
stream-json init event lists every server tool (call-time denial only).
`--disallowedTools` on an MCP tool name DOES remove it from the
model-visible manifest. Model self-report of its tools is unreliable (a
probe returned invented names); the **stream-json `system/init` event's
`tools` array is the ground truth** and is what per-row validation checks.
Therefore every arm runs with:

- `--tools ""` (empty built-in set — all four arms identical),
- `--allowedTools` = exactly the arm's manifest above,
- `--disallowedTools` = the sealed built-in list
  (`run_benchmark.DISALLOWED_TOOLS`) **plus** every excluded MCP tool on an
  attached server: X denies the 7 join tools + `brain_unit`; J denies
  `decl_exists` + `brain_unit`; D′ denies nothing extra; E′ has no
  wikibrain server attached at all.

Known interface deviation from Tier-1: Tier-1 tooled arms did not pass
`--tools ""`, so some built-ins were model-visible (never callable). Here
all four arms get the same empty built-in surface — an intentional
yoked-interface improvement, identical across arms.

**Factor-purity caveat, stated honestly (both reviews get this
disclosure).** The join's outputs are *inherently existence-verified
content*: `brain_bridge`/`brain_cell` return only decls verified against
the Brain's decl index, so arm J still RECEIVES verified names — what J
removes is the ability to CHECK model-written names in a
generate-then-verify loop. The VERIFIER factor is therefore precisely
"**explicit batch verification of model-generated names**", not "any
exposure to verified names". A JOIN main effect estimated here is the
effect of the joined surface *including* its implicit verification of
retrieved content. This is the sharpest cut the deployed architecture
permits without building a deliberately-degraded (unverified-output)
bridge, which would not be the artifact under test. Additional bundle
residue in the JOIN factor: curated metadata, external-DB snippets, and a
one-tool-surface interface (REVIEW-2's "yoked interface" is approximated —
identical prompt, model, budget, empty built-ins, tool counts 6/7/7/8 —
but payload formats necessarily differ between the joined and unjoined
stacks).

## 3. Tasks, model, pins, and disclosed exposure

- **Tasks**: the 100 fresh tasks, `bench/data/fresh_tasks.jsonl`
  (sha256 prefix `c4c759af34a551fd`), all `split:"eval"`, 44 distinct
  source commits (`added_in.commit` — the clustering unit).
- **Model**: `claude-haiku-4-5-20251001` (Tier-1's model), claude CLI
  2.1.153 on Max auth, env scrubbed per §4.
- **Prompt**: byte-identical to Tier-1 — `run_bridge.build_prompt` with
  `max_turns=30` (arm-neutral; names no tools).
- **Formal-search pin**: all `formal` stdio reads (`decl_grep`,
  `decl_read`) run against a read-only `git archive` extraction of mathlib4
  commit `61a5e4f338bfdddf2f6296402a49fe80f3b1a147` (the tree the Tier-1
  fresh runs searched), recreated exactly as
  `bench/analysis/rerun_E_fresh.py` did, at
  `<scratchpad>/mathlib4-pin/Mathlib`, passed via `MATHLIB_ROOT`. The live
  checkout is never touched.
- **Loogle is live** (disclosed): `mcp__formal__loogle` queries
  loogle.lean-lang.org, which indexes current Mathlib master — that index
  now contains the fresh theorems (merged 2026-07-03→07-16). Same
  disclosure held for Tier-1. Affects E′ and X only (J/D′ carry no
  loogle).
- **Wikibrain is live** (disclosed): the remote MCP at
  `https://wikilean.jackmccarthy.org/mcp` (override: `WIKIBRAIN_MCP_URL`),
  Brain decl-index snapshot `generated_at 2026-08-01T06:19:38Z`, pin
  `bf3266149cda603f`. **Prereg-time census (2026-08-07, recorded before
  any run)**: batch `decl_exists` over all 100 gold names → **3/100 exist
  in the live index** (fresh_037 `Manifold.IsImmersionOfComplement.contMDiff`,
  fresh_054 `MeasureTheory.IsSetSemiring.exists_disjoint_finset_sdiff_eq`,
  fresh_095 `LocallyBoundedVariationOn.exists_monotoneOn_sub_monotoneOn'`),
  97/100 absent, 0 renamed. The holdout is thus 97/100 against the index
  the verifier arms (X, D′) and the join arms (J, D′) query. Preregistered
  sensitivity row: the primary analysis on all 100; a secondary row drops
  fresh_037/054/095.
- **Checkout exposure** (disclosed, inherited): 51/100 golds are exposed
  own-module in the 61a5e4f338 tree that E′/X read
  (`bench/analysis/fresh_exposure.md`); exposure-stratified rates are a
  preregistered secondary cut, same as v3 §4.2.
- **Fresh-set validity caveats inherited from v3 §3.2** (docstring-derived
  NL, same-vendor paraphrase authorship, determinacy subset det2 74/100)
  apply unchanged and will be carried into the report.

## 4. Execution protocol (the §3.4 lessons, made mechanical)

**Runner**: `bench/factorial/run_factorial.py` (built in Stage 2, after
this file is committed). Rows →
`bench/data/runs_factorial/{Ep,X,J,Dp}/fresh_XXX.json` + full raw
stream-json transcript gzipped alongside
(`fresh_XXX.stream.jsonl.gz`, the bench/v2 max-telemetry convention).
`bench/data/runs_factorial/` is not gitignored (`bench/data/.gitignore`
ignores only `runs/`); rows and streams are committed after the run, the
same convention as `bench/v2/runs/`.

1. **One interleaved order, no arm blocks** (kills the §3.4 time confound
   REVIEW-2 named): the 400 (task, arm) pairs are enumerated
   `for task_id in sorted ids: for arm in (Ep, X, J, Dp): append`, then
   shuffled ONCE by `random.Random(20260803).shuffle`. Execution consumes
   this fixed order with a single worker pool, concurrency 4; the first
   wave is staggered 5 s apart (cold-start-race mitigation, commit
   `834a130a`); `MCP_TIMEOUT=120000`. Resume preserves the same global
   order (completed pairs are skipped in place).
2. **Mechanical turn cap 30**: `--max-turns 30` on the CLI. Verified
   against the installed CLI 2026-08-07: the flag is accepted (absent from
   `--help` but functional) and enforces a hard stop with result subtype
   `error_max_turns`. A capped row is a VALID terminal row, not an
   infrastructure error: it scores on whatever declaration is extractable —
   `extract_lean` over the result text if present, else over the
   concatenated assistant text blocks of the transcript (last
   sorry-bearing fenced Lean block wins) — else it scores as a failure.
   `capped: true` is recorded. (Tier-1's budget was advisory and overrun
   on ~half the rows — v3 deviation 5; this closes it.)
3. **Per-row MCP attach-signature + manifest validation, auto-condemn +
   retry** (ports and strengthens the `bench/v2/run_agent.py` commit
   `834a130a` init-signature check): a row is attach-clean iff the
   stream-json `system/init` event is present, every server in the arm's
   config reports `status:"connected"` in it, AND the init `tools` array
   equals the arm's expected manifest exactly (sorted). Any other row is
   condemned — written with an `error`, never counted terminal — and
   automatically retried (up to 5 attempts per pair, 20 s backoff between
   attempts; a pair still failing after 5 attach-condemned attempts halts
   the runner loudly rather than being silently dropped). A
   manifest-validated row with zero MCP calls is the model's genuine
   choice and stays (v3 policy).
4. **Per-row condition hash**: sha256 over the canonical JSON of {arm,
   model, sorted allowed tools, sorted disallowed tools, resolved
   mcp-config content, expected manifest, mathlib pin sha, tasks-file
   sha256, prompt-template sha256, max_turns, CLI version}. Recorded on
   every row; every accepted row of an arm must carry the identical hash.
5. **429 / usage-limit is never a terminal row**: any attempt whose error
   matches (429 | usage/session limit | rate limit | overloaded) is not
   written as terminal; the runner enters a global hold (parses a reset
   time from the message when present, else exponential backoff 60 s
   doubling to a 3600 s cap) and retries the same pair. Auth failures
   (401/OAuth) likewise never terminal: loud halt for re-auth, resume
   preserves order.
6. **Hygiene (Tier-1 code path)**: child env drops `ANTHROPIC_API_KEY`,
   `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `USE_STAGING_OAUTH`,
   `USE_LOCAL_OAUTH`, `CLAUDE_CODE_OAUTH_SCOPES` (Max-auth gotcha + the
   644fd89 endpoint scrub, as `bench/analysis/rerun_E_fresh.py`'s driver
   scrubbed); children run from one fresh empty temp dir outside the repo;
   `--strict-mcp-config`; per-attempt wall timeout 600 s; wikibrain
   preflight (`run_benchmark.preflight_wikibrain`) before the run and on
   every resume, aborting loudly on failure.
7. **Dry-run gate**: before the real run, 2 tasks × 4 arms interleaved
   must produce 8/8 attach-clean rows with enforced caps, recorded
   condition hashes, per-arm manifests verified both from the rows and by
   an explicit init-event tool-listing probe, and the pinned tree honored
   (a `decl_read` probe must show 61a5e4f338 content). Dry-run rows are
   then deleted; the real run covers all 400 pairs including those tasks.
8. **Monitoring**: the run is polled in-turn by the executing agent (no
   unattended watchers are trusted — v3 lesson); the runner is restarted
   on death; resume is idempotent from disk state.
9. **Separation of roles**: the agent that executes the runs does NOT
   score, judge, or analyze them. Scoring/judging/analysis is a separate
   subsequent phase.

## 5. Endpoints and analysis plan

**Primary endpoint — grounded typecheck under the REPAIRED oracle.** Per
(task, arm): success = produced a declaration ∧ zero cited names classified
`hallucinated` by `bench/analysis/halluc_validation.py::classify_adjusted`
(the five mechanical repair rules R1–R5 over the union oracle, exactly as
v3 §4.1) ∧ the declaration typechecks on the **bench-lean-fresh rig**
(`/Users/jack/Desktop/LEAN/bench-lean-fresh` via
`bench/score_bridge.py::typecheck_stub` routing, `sorry` a warning not an
error). Citation surface: `output_lean`, the run's final Lean block, as in
v3.

**Primary analysis — commit-clustered paired bootstrap.** Clusters = the
44 `added_in.commit` values; B = 10,000; seed = 20260803; machinery =
`bench/analysis/fresh_clustered.py::cluster_boot_rd` (percentile 95% CI and
two-sided percentile-inversion p from the same resampling distribution).
With Y_a(t) ∈ {0,1} the primary outcome of arm a on task t, all paired per
task:

- **JOIN main effect** = mean over tasks of ((Y_D′ + Y_J) − (Y_X + Y_E′))/2
- **VERIFIER main effect** = mean over tasks of ((Y_D′ + Y_X) − (Y_J + Y_E′))/2
- **Interaction** = mean over tasks of (Y_D′ − Y_J) − (Y_X − Y_E′)

Each bootstrap replicate resamples the 44 commits with replacement and
recomputes the per-task-mean statistic as (sum of resampled cluster sums) /
(sum of resampled cluster sizes). α = 0.05 two-sided (§1 criteria).
Supporting descriptives: the four per-arm rates with Wilson 95% CIs and
the six pairwise clustered RDs.

**Secondary endpoints (exploratory, same clustered machinery):**

1. Run-level repaired hallucination: run cites ≥1 `hallucinated` name
   under `classify_adjusted` (lower is better).
2. Judge evaluated-equivalence: blinded LLM judge, protocol identical to
   `bench/analysis/judge_fresh_run.py` — prompt template imported verbatim
   from `judge_bridge.PROMPT` ({informal, gold, produced} only), gold shown
   with `gold_context`, arm-substring blinding scan aborts on leak,
   no-tools `claude-sonnet-5` judge from an empty scratch cwd,
   `--max-turns 1`; graded on the `evaluated` (mathematical-equivalence)
   verdict.
3. Conjunction: grounded typecheck ∧ judge evaluated-equivalent.
4. Turn/tool-use descriptives (turns, tool calls by name, `capped` rate,
   cost) per arm; `decl_exists` call counts in X vs D′ (the verifier-usage
   manipulation check); informal-tool touches in E′/X (the E manipulation
   check v3 flagged).
5. Exposure-stratified primary rates (own-module basis, 51/49) and the
   3-task live-index-leak sensitivity (§3).
6. det2-subset (74/100 determinate) sensitivity of the primary contrasts.

**Scoring notes preregistered:** rows with `error` (after the retry policy
exhausts infrastructure causes) score as failures on all endpoints, and
their count per arm is reported; capped rows score on their extracted
output per §4.2. No row is dropped from the primary analysis for any
model-behavior reason.

## 6. What this experiment cannot show (preregistered limits)

- It cannot separate the join's curated content from its interface (one
  integrated tool surface vs two servers) — REVIEW-1's
  "suppress-or-shuffle alignments" control is out of scope.
- J's outputs remain implicitly verified (§2 caveat); a JOIN null is
  evidence the *joined surface* adds nothing beyond explicit verification,
  not that alignment content is worthless.
- One model, one seed per (task, arm) pair — no run-to-run variance
  estimate (v3 deviation 3 persists; the clustered bootstrap covers task
  sampling, not decoding noise).
- The fresh set's NL paraphrases share a vendor with the subject model
  (v3 §3.2), and 51/100 golds are checkout-exposed to E′/X.

## 7. Artifacts this file precommits

- `bench/factorial/run_factorial.py` — the runner (Stage 2).
- `bench/arms/mcp-Ep.json`, `mcp-X.json`, `mcp-J.json`, `mcp-Dp.json`.
- `bench/data/runs_factorial/{Ep,X,J,Dp}/fresh_XXX.json` (+
  `.stream.jsonl.gz`) — 400 terminal rows, committed post-run.
- The prereg-time live-index census JSON (committed with the harness as
  `bench/factorial/live_declexists_census.json`).

## 8. Preregistration procedure

This file is committed alone, before the harness is built and before any
row (including dry-run rows) is executed, on branch `main` of
`Deicyde/WikiLean`. The commit hash of that commit is the prereg
timestamp. Stages: PREREGISTER (this commit) → BUILD (harness commit) →
DRY-RUN (8 rows, then deleted) → RUN (400 rows) → separate-phase
scoring/analysis.

## 9. Deviations log

(Empty at preregistration. Any deviation from §§2–5 discovered during
BUILD/DRY-RUN/RUN is appended here with its own commit before results are
interpreted.)
