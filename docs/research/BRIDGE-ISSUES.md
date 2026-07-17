# Bridge Experiment + BRAIN v3 — issue queue for the next iteration

> Written 2026-07-17 at a context boundary. Each issue is self-contained: state,
> blocker, exact next command. Branch `brain-v3-cells` (~40 commits ahead of
> `main`; `main` untouched and still deployed). Companions:
> `docs/research/BRIDGE-EXPERIMENT.md` (the preregistration — its deviations
> section is part of the record), `docs/BRAIN-V3.md`, `brain/SCHEMA.md` (v3).

## P0 — unblock and run the campaign

### 1. Re-authenticate the terminal `claude` CLI (JACK, ~30s)
Every benchmark child process fails `401 OAuth access token has expired`. Two-layer
diagnosis is DONE: layer 1 (session env `ANTHROPIC_BASE_URL`/`USE_*_OAUTH` sending
the token to the wrong endpoint) is fixed — all four CLI-shelling surfaces scrub
those vars (commit 644fd89). Layer 2 remains: the CLI keychain's own OAuth token +
refresh token are expired. **Fix: run `claude` interactively in Terminal once.**
The desktop app's auth is a separate, auto-refreshing store — its usage meter says
nothing about the CLI keychain.

### 2. Run the Tier-1 campaign
Prereqs all green (471 census-gated tasks, arms pilot-verified, servers hot).
```
bench/run_campaign.sh dev     # 150-run smoke (5 arms x 30 dev tasks)
bench/run_campaign.sh eval    # Tier 1a: 5 x 341 = 1,705 runs (resumable)
bench/run_campaign.sh fresh   # Tier 1b: 5 x 100 = 500 runs (contamination-proof)
```
Watch: per-arm tool_calls_by_name (arm A must be 0; arm D should use brain_*),
the runner's "0 tool calls — suspect" warning, and `--resume` after any interrupt.
Then scoring: `python3 bench/score_bridge.py` (typecheck+hallucination),
`python3 bench/judge_bridge.py --arm A..E`, then `--calibration 50` → **Jack
hand-grades the 50** → report judge–human agreement BEFORE quoting judge numbers.
Analysis is preregistered: McNemar D-vs-E on faithful@budget + cost axes.

### 3. Restart the three local servers if the machine rebooted
All three die with the machine; the campaign preflights will catch it:
```
# arm D (real v3 Worker app, local):
cd wiki && npx tsx ../bench/arms/local_worker.mts 8790 &
# Tier-1a grading server (wikifunctions pin, ~3min Mathlib load):
python3 bench/typecheck.py --server &                       # /tmp/wikilean_tc.sock
# Tier-1b grading server (fresh pin):
python3 bench/typecheck.py --server --project /Users/jack/Desktop/LEAN/bench-lean-fresh \
  --socket /tmp/wikilean_tc_fresh.sock &
```

## P1 — before/while the campaign runs

### 4. Jack's review of `brain-v3-cells` → deploy → merge (Phase 5)
The whole v3 stack (atom layer, renderer, API/MCP, bench) awaits review. A
Worker version (pre-API-improvements) sits uploaded-but-unserved; re-upload after
review: `cd wiki && npx wrangler versions upload`, then `npx wrangler versions
deploy` (or `npm run deploy`). After deploy, arm D could target production and
`WIKIBRAIN_MCP_URL` becomes unnecessary. Reminder: nightly deploy stays gated
(`WIKILEAN_BRAIN_DEPLOY=1` + clean tree + main branch).

### 5. Codify the gold-census construction as ONE shared helper
The candidate side is unified (score_bridge renames the produced decl `__cand__` —
fresh env CONTAINS the golds, so name collisions were real). The GOLD census
construction (rename-to-`__gold__` + namespace-opens, which passed 100/100) lives
only in agent-written scratch scripts. Extract `assemble_gold(row)` +
`prepare_candidate(output_lean)` into bench/typecheck.py or a bench/construct.py,
and make any future census + the scorer both call them. Divergence here produced a
false alarm already (fresh_039).

### 6. bench/README.md documents only the old 180-task bench
Add the bridge harness: arms, run_campaign, the two grading servers/pins, census
files, judge calibration protocol. The old bench is demoted to an API diagnostic
(gold circularity — see the design doc's threats).

## P2 — the deferred design requirements (needed for Tiers 2–3)

### 7. `brain_premises(goal_state)` — the premise-selection mode
TheoremGraph proved concept retrieval ≠ premise retrieval (their concept-tuned
config LOWERED premise Recall@10 0.224→0.165). Tier 2 (FATE-H/MathlibMPR proving)
is expected to expose this; pre-registered as an API finding. Needs a premise-level
index (decl-to-decl usage stats exist in rollup_edges + synapse witnesses).

### 8. Ingest formal-conjectures + erdosproblems.com (Tier 3 prerequisite)
Already chipped as a spawn-task (task_b560333b) with a full brief: adapters →
`decl:FormalConjectures:*` atoms joined via the teorth/erdosproblems YAML →
source_registry licences → nightly cadence. Tier 3a (offline re-formalization of
150 accepted Erdős statements) reuses the bridge grading rig once this lands.

### 9. BEq+ equivalence grading
This campaign grades typecheck + judge + hallucinated-decl rate. The preregistered
PRIMARY metric (faithful@budget = typecheck AND BEq+ vs gold) needs the
ProofNetVerif BEq implementation (bidirectional `exact?`-style proving between
gold and candidate). Build against the two REPL servers.

### 10. Repair the 21 judge-only ProofNet golds (optional, raises n)
Families: `Lattice ℂ` abs-resolution (6 — needs `Complex.abs`/`‖·‖` rewrite,
semantic care), `Subgroup.relindex` rename (2), `QuotientMap`→`Topology.IsQuotientMap`
(3), Munkres binder drift (~5), misc. Each repair must be re-verified through
/tmp/wikilean_tc.sock and tagged in `gold_repairs`.

### 11. Fresh-set determinacy second annotator
The 86/100 determinate screen is single-annotator (design wants two independent
formalizers agreeing). Cheap agent pass; report the excluded fraction both ways.

## P3 — infrastructure debt surfaced this session

### 12. `_pin()` is mtime-based → content hash
A worktree churned 42,005 edge pins on byte-identical inputs. Replace mtime with a
content hash in brain/build_common.py `_pin()`; one nightly will re-pin everything
once, then diffs become meaningful forever. (Memory: brain_build_traps.)

### 13. Worker 503-bursts on article writes
apply_decl_sweep converged (411/411) only by outlasting multi-minute 503 windows
(Cloudflare error page, alphabetically-consecutive failures ⇒ time-bursts, not
payload size). Investigate Worker CPU limits on big `findLostHuman` validations /
free-plan burst limits; consider chunked writes. Until then: the patient-backoff
recipe (RETRY_BACKOFFS up to 300s) is proven.

### 14. `wrangler dev` is broken in this environment (`spawn EBADF`)
Wrangler 4.107 + this sandbox. The local_worker.mts workaround serves the app
without workerd. Retest after a wrangler upgrade; if fixed, arm D can use it.

### 15. engine.golden.test.ts fails at HEAD (pre-existing)
Stale gitignored fixtures (memory: wikilean_golden_fixtures). Regenerate site/out
via render.py per slug, or re-bless. It pollutes every `npm test` reading.

## Standing decisions on record (do not relitigate)
- Merge rule stays wide; ballooning cells = tagger signal (`cell_review.jsonl`,
  23 flagged; grounding_overrides is the fix channel).
- The 46-cell root bubble is CORRECT residue (11 hallucinations, 24 real-but-
  unfileable, 8 refuted, 3 unsure) — annotation debt, not a filing bug.
- No silent caps anywhere; round-robin-by-kind sampling; fit/measure robustly.
- Verify RENDERED pixels, not DOM attributes; drive UIs, don't read them.
- Typecheck ≠ success; judge numbers quarantined until human calibration.
