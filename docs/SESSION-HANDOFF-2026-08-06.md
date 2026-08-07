# Session handoff — 2026-08-06 (evening)

> For the next agent. Read CLAUDE.md first (invariants), then `python3
> manage/status.py` (live state), then this file. The durable policy this
> session ratified is in ROADMAP.md §Binding decisions ("Human-at-boundaries")
> — this file is the narrative + the work order it points at.

## Where things stand

Everything below is COMMITTED and (unless noted) DEPLOYED + live-verified.
Worker version at session end: `a6eea4c0` (evidence-card /proposals) —
re-verified still current 2026-08-07 17:58 EDT, and no wiki/src change has
landed since, so prod == HEAD. NOTE (2026-08-07 review): 22 commits sit
unpushed to origin (back to ed5181a9, ~2 weeks of work local-only) — fine
under the push-only-when-asked policy, but "committed" here does NOT mean
"on GitHub"; push when convenient.

Shipped 2026-08-04 → 06, in order: unified nav + header search + dynamic
/about + [edit]-hide (batch 1) · trust signals (N/M human-reviewed badge,
legend popover, least-reviewed strip) · editor save-UX + `__WL_MATCHED__`
orphan/suppressed split · anchor-rot render telemetry (log-only) · brain v2
per-node asset retirement (450→94MB, `/api/brain/node` deleted) ·
frontier/halo reconciliation (bond-weighted formal-proximity, SCHEMA "Formal
proximity"; shells destroyed) · tombstone-layer destruction (old graph routes
plain-404, RESERVED squats names) · decl-existence sweep applied to prod
(miss rate 2.96%→0.48%; 42 auto edits; 14 proposals filed) · /proposals
evidence cards (quote, real diff via applyProposalFields, docs links +
existence ticks, GitHub evidence links, no-delta banners + 3-layer guard).

## Ratified decisions (Jack, explicit, 2026-08-06)

1. **Human-at-boundaries** (ROADMAP binding decision, quoted verbatim there):
   AI decides all intra-WikiLean proposals incl. human-provenance targets
   (→ 'ai-moderated' on change, attribution never laundered); humans gate
   only cross-site pushes (mathlib4 PRs, Wikidata, Wikipedia). Nightly
   auto-decide ON. Confirmed via explicit two-part prompt, both "yes".
2. **proof_wanted ⇒ partial** ('formalized' alone is an overclaim). Already
   encoded in manage/decl_existence_sweep.py (committed 9d2d042f).

## THE WORK ORDER — build the AI decide path (next agent's first task)

Design was written and twice classifier-blocked mid-session (bulk-prod-write
class); Jack has since ratified everything explicitly, so cite the ROADMAP
binding decision when you build. Components:

1. **Bearer decide path (wiki/src/index.ts — the `approve_proposal` /
   `reject_proposal` action branch, ~:1383 as of 2026-08-07; grep the action
   strings, line numbers rot):** both actions accept PIPELINE_TOKEN alongside
   patroller/admin sessions.
   Bearer decisions: annotation_event actorType 'ai'; distinct revisions kind
   (e.g. 'proposal-decided-ai'); AI-approve over provenance:'human' sets
   'ai-moderated' (NEVER leaves 'human' on AI-changed bytes, never mints
   'human'); reject feeds the same rejected-memory. Session path byte-identical.
2. **GET /api/proposals (bearer-only):** machine twin of the /proposals page
   rows — reuse its row-builder + applyProposalFields delta, do not fork.
3. **site/resolve_proposals.py** (deterministic, NO LLM): (a) noChange →
   reject not_better; (b) decl-rename where new decl EXISTS in the union
   oracle AND old is gone → approve; (c) proof_wanted stub + proposed
   'partial' → approve; (d) else leave pending + print. Dry-run default,
   --submit. Oracle rules from memory: union (cache+source), never
   bare-suffix; import decl_existence_sweep's loaders.
4. **Nightly wiring:** step in site/ops/nightly-moderate.sh after review,
   gated WIKILEAN_AUTO_DECIDE (add to nightly.env, default 1); failure must
   not kill the rest of the night.
5. **/stats:** if bytes change (decided-by-AI row), bump page:stats:v4→v5 +
   pinning test.
6. **Docs:** propose-then-approve.md policy section; CLAUDE.md findLostHuman
   line (anti-clobber guard, not policy gate) + attribution hard line; memory
   note project_propose_then_approve.md.
7. Adversarial review before commit (session pattern: invariants lens +
   correctness lens), then deploy + live-verify. Suites: wiki tsc + npm test
   (714 tests, all green as of 2026-08-07 — see Residue), site/test_moderate.py.

## Queue state (check /stats "Pending" before acting)

UPDATE 2026-08-07 17:58 EDT: live pending = **6** — the resolver RAN (the
"if pending ≈ 15 it hasn't run" check fired negative). But the documented
remainder was 4 (Picard–Lindelöf rename → approve, evidence in
manage/data/decl_sweep_likely_rename_verdicts.json; 2 Adjoint_functors
test-artifacts → reject; 1 unexamined nightly proposal), so either it
cleared 9 of 11 or new proposals arrived since — RE-TRIAGE the 6 from the
live /proposals page before acting; do not trust this paragraph's itemized
remainder. Once the decide path ships, resolve_proposals.py handles the
queue instead.

<details><summary>Original 2026-08-06 text (superseded)</summary>

15 pending at last count. `manage/resolve_pending_20260806.py` (committed,
Jack-runnable) clears 11 of them under CURRENT deployed rules: 3 real
/Conjecture downgrades via bot path + no-delta sweep triggers for the 8
confirmations. Unknown whether Jack ran it — if pending ≈ 15, it hasn't run.
Remainder: Picard–Lindelöf rename (approve), 2 Adjoint_functors
test-artifacts (reject; the annotations themselves look like junk — under the
new policy the AI may tombstone them via the decide/edit machinery once
built), 1 pre-existing nightly proposal (unexamined).
</details>

## Residue / known issues

- **engine.golden.test.ts — RESOLVED 2026-08-07**: the nightly golden-fixture
  refresh re-pinned exactly as predicted; full wiki suite now 714/714 across
  34 files (verified 2026-08-07). The work-order Suites line's "1 known
  failure" caveat is obsolete — expect green.
- **Sweep residue: 58 missing decls / 73 citations** (post-fix re-run):
  ~25 adjudicated leave/judgment (17 judgment renames were deliberately NOT
  filed — see the verdict JSONs in manage/data/) + ~33 new drift from the
  2026-08-05 nightly. A recurring sweep→verdict→resolve loop is the natural
  next increment after the decide path.
- **Classifier lesson:** bulk production-D1 writes and standing-authority
  builds from the orchestrator's shell get blocked unless Jack explicitly
  names the action in-turn. Jack-run scripts (committed under manage/) and
  the unattended nightly are the clean channels.
- Deferred (ROADMAP): articles.anchored_count migration; editor in-place body
  swap; 2×2 factorial + 36-task human queue (bench).

## Memory system

WikiLean memory at `~/.claude/projects/-Users-jack-Desktop-LEAN-WikiLean/memory/`
was updated through this session (brain-v3 retirement, unified-map deletion,
propose-then-approve status). After building the decide path, update
`project_propose_then_approve.md` again + add the boundary policy to the
index if not already linked.
