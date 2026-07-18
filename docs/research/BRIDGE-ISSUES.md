# Bridge Experiment + BRAIN v3 — issue queue for the next iteration

> Written 2026-07-17; REFRESHED 2026-07-17 (second boundary, pre-/compact). Each issue is self-contained: state,
> blocker, exact next command. Branch `brain-v3-cells` (~40 commits ahead of
> `main`; `main` untouched and still deployed). Companions:
> `docs/research/BRIDGE-EXPERIMENT.md` (the preregistration — its deviations
> section is part of the record), `docs/BRAIN-V3.md`, `brain/SCHEMA.md` (v3).

## P0 — unblock and run the campaign

### 1. Re-authenticate the terminal `claude` CLI (JACK, ~30s)
STATE NOW: probe says `Not logged in · Please run /login` — the stale token is
CLEARED (progress vs the earlier 401s). **Fix: run `claude` in Terminal, type
`/login`, complete the browser flow.** Env-var layer already fixed (644fd89: all
CLI-shelling surfaces scrub ANTHROPIC_BASE_URL/USE_*_OAUTH). Probe to confirm:
`env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL \
 -u USE_STAGING_OAUTH -u USE_LOCAL_OAUTH -u CLAUDE_CODE_OAUTH_SCOPES \
 claude -p "reply with exactly: ok" --model claude-haiku-4-5-20251001 < /dev/null`

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
(Restarted again this session: main READY on /tmp/wikilean_tc.sock; fresh was
mid-load at handoff — check `grep READY .../scratchpad/tc_fresh.log`; arm-D
worker status UNKNOWN after the last session teardown — re-check pgrep tsx.)
EVERY-ROW-120s-TIMEOUT = a dead socket silently falling back to single-shot,
not hard statements. Servers die with each session teardown.
All three die with the machine; the campaign preflights will catch it:
```
# arm D (real v3 Worker app, local):
cd wiki && npx tsx ../bench/arms/local_worker.mts 8790 &
# Tier-1a grading server (wikifunctions pin, ~3min Mathlib load):
python3 bench/typecheck.py --server &                       # /tmp/wikilean_tc.sock
# Tier-1b grading server (fresh pin):
python3 bench/typecheck.py --server --project /Users/jack/Desktop/LEAN/bench-lean-fresh \
  --socket /tmp/wikilean_tc_fresh.sock \
  --repl-bin /Users/jack/Desktop/LEAN/lean-repl-fresh/.lake/build/bin/repl &
# (the --repl-bin is MANDATORY: the fresh pin is v4.33 and the default repl is
#  v4.32 — a mismatched repl "loads" a bare prelude; the sanity gate now refuses
#  READY in that state instead of serving wrong answers)
```

## P1 — before/while the campaign runs

### 4. ~~Deploy + merge (Phase 5)~~ DONE (2026-07-18, on Jack's direct order)
Jack waived the detailed pre-review ("I don't want to wait any more"):
`main` fast-forwarded to the branch tip, `npm run deploy` shipped Worker
version c7c36e9d, pushed to origin. Live-verified: /brain renders v3 at real
pixels (34 supercell bubbles → 468 cells on dive), cell API + MCP 3.0.0 + 301s
+ v2 node oracle all answering. TRAP LEARNED: the 440MB asset upload
fetch-fails under campaign load — pause the campaign (resumable) and deploy on
a quiet machine (~100s). Still open from the review pass: the **7 grading
disputes in `catalog/data/grounding_overrides.jsonl`** need Jack's call, and a
post-hoc code review of the ~65 shipped commits is still worth doing.
### 4b. Retire the v2 per-node brain assets (~340MB of the 440MB deploy)
Phase-5 leftover, now the deploy-size tax. Three coupled changes:
(1) `brainNodeExists` (wiki/src/brain.ts) — swap the per-node-shard oracle for
a labels.json membership set (8.7MB, already isolate-memoized for search;
aliases.json is NOT sufficient — only decls+slugs). (2) stop `brain/
build_shards.py` emitting per-node q*/xref_*/decl_*/path_*/lit_* files (keep
cells/, views/, labels, aliases, manifest, sources, xref_index/explorer —
check each against consumers first). (3) retire GET /api/brain/node → 410 like
the other v2 routes. Then prune site/assets/brain leftovers + rebuild public
(→ ~100MB) + tests + deploy.

### 5. ~~Codify the gold-census construction as ONE shared helper~~ DONE
`bench/construct.py`: `assemble_gold` / `prepare_candidate` / `rename_last_decl`
(LAST decl — bundled auxiliary defs keep their names). Verified on both pins.
Original text follows for context:
### 5-old. Codify the gold-census construction as ONE shared helper
The candidate side is unified (score_bridge renames the produced decl `__cand__` —
fresh env CONTAINS the golds, so name collisions were real). The GOLD census
construction (rename-to-`__gold__` + namespace-opens, which passed 100/100) lives
only in agent-written scratch scripts. Extract `assemble_gold(row)` +
`prepare_candidate(output_lean)` into bench/typecheck.py or a bench/construct.py,
and make any future census + the scorer both call them. Divergence here produced a
false alarm already (fresh_039).

### 6. ~~bench/README.md~~ DONE (dd7651b) — bridge harness fully documented.

## P2 — the deferred design requirements (needed for Tiers 2–3)

### 7. `brain_premises(goal_state)` — the premise-selection mode
TheoremGraph proved concept retrieval ≠ premise retrieval (their concept-tuned
config LOWERED premise Recall@10 0.224→0.165). Tier 2 (FATE-H/MathlibMPR proving)
is expected to expose this; pre-registered as an API finding. Needs a premise-level
index (decl-to-decl usage stats exist in rollup_edges + synapse witnesses).

### 8. ~~Ingest formal-conjectures + erdosproblems.com~~ DONE (merged faf142d)
The spawned agent (task_b560333b) delivered the full campaign: deterministic
adapters (`brain/ingest/formal_conjectures.py` + `erdosproblems.py`, weekly
nightly cadence, source_registry licences), 1,217 FC decls + 1,217 Erdős ext
pages, and a 100-agent tagging fleet (829/829 files, every shard
skeptic-verified) folding 2,115 joins (148 `formalizes`) via a new validated
`fc_link` row type in fold_proposals.py. Acceptance P11 (Erdős round-trip)
green in the canonical tree: 16/16 + 36/36 + 33/33. Tier 3a now has its corpus.
**NEW for Jack: 7 grading disputes routed to `catalog/data/grounding_overrides.jsonl`
await review** (see issue 4's review pass).

### 9. BEq+ equivalence grading
This campaign grades typecheck + judge + hallucinated-decl rate. The preregistered
PRIMARY metric (faithful@budget = typecheck AND BEq+ vs gold) needs the
ProofNetVerif BEq implementation (bidirectional `exact?`-style proving between
gold and candidate). Build against the two REPL servers.

### 10. ~~Repair the 21 judge-only ProofNet golds~~ DONE (ec2a9e1)
20/21 repaired+verified via construct.assemble_gold; Tier-1a typecheck-gradable
370/371. Munkres|exercise_25_9 PERMANENTLY judge-only (IsNormalSubgroup deleted
from Mathlib; restructuring a gold is off-limits).

### 11. ~~Fresh-set determinacy second annotator~~ DONE
det2 on all 100 rows (independent, informal-only protocol). Agreement 79%,
κ≈0.20 — the annotators apply COMPLEMENTARY strictness (family near-dups vs
semantic underdetermination). **Primary Tier-1b set = 74 both-determinate rows**
(excluded fraction: 14% single, 26% either). Catches: fresh_034/036 share
byte-identical NL but different decls; fresh_030's "alternating signs" gloss
misdescribes Euler's (−1)^k pattern.

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

## Numbers at handoff (2026-07-17, HEAD faf142d, ~55 commits ahead of main)
- Tier 1a: 371 tasks, 370 typecheck-gradable (gold_census ok=407/bad=64 — the 64
  are fresh rows counted vs the OLD pin; they grade 100/100 on the fresh pin).
- Tier 1b: 100 tasks, PRIMARY = 74 both-determinate (determinate AND det2).
- Tier 3a corpus (merged faf142d): 1,217 FC decls + 1,217 Erdős pages in the
  brain; 2,115 verified joins; brain = 78,437 nodes / 772,782 edges; full chain
  re-verified in the canonical tree (16/16 + 36/36 + 33/33, shards + page rebuilt).
- Campaign: `bench/run_campaign.sh dev` → `eval` → `fresh`, then score_bridge →
  judge_bridge → `--calibration 50` → Jack hand-grades → McNemar D-vs-E.
- Uncommitted tree at handoff: clean (check `git status` first regardless).

## Standing decisions on record (do not relitigate)
- Merge rule stays wide; ballooning cells = tagger signal (`cell_review.jsonl`,
  23 flagged; grounding_overrides is the fix channel).
- The 46-cell root bubble is CORRECT residue (11 hallucinations, 24 real-but-
  unfileable, 8 refuted, 3 unsure) — annotation debt, not a filing bug.
- No silent caps anywhere; round-robin-by-kind sampling; fit/measure robustly.
- Verify RENDERED pixels, not DOM attributes; drive UIs, don't read them.
- Typecheck ≠ success; judge numbers quarantined until human calibration.
