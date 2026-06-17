# Propose-then-approve: letting AI reviewers update human annotations

**Status:** designed, awaiting Jack's go on the UX surface (§5) before build.
**Owner decision recorded:** "Just because a review is human-curated does NOT
mean it shouldn't be updated by reviewers." Chosen policy: **propose → you
approve** (not: freeze forever, not: let the bot overwrite).

## Motivation

A human annotation is currently *frozen*: the bot save path runs `findLostHuman`
(wiki/src/validation.ts) and 422s any write that drops or alters a
`provenance:"human"` annotation, tombstones included. That's correct as a
floor — the bot must never silently overwrite Jack — but it's too strict as a
ceiling. Example: Jack tagged "Ring of differentiable functions" on
*Commutative ring* as **not_formalized**. If Mathlib later gains that decl, the
AI should be able to say "this is now formalized — here's the decl" and have
Jack one-click accept it, instead of the annotation being immortally stale.

## What already exists (build on this — do not rebuild)

| Piece | Where | State |
|---|---|---|
| `moderation_state.proposal` (JSON column, "pending payload awaiting review") | wiki/src/db/schema.ts:66 | **dormant** — nothing reads/writes it |
| Agent dissent: `moderation_flag` string on a human annotation | batch_annotate.py prompt (MODERATE_AGENT1_SYSTEM §1) | emitted |
| `_preserve_human`: restores the human annotation verbatim **and harvests** the flag (F14) | batch_annotate.py:343 | working |
| Dissent rides `meta.ladder.moderation_flags` → `revisions.meta` | moderate.py:318, build_meta | preserved but **not actionable** |
| `annotation_events` + field-diff (`diffFields`/`serializeFieldChanges`) | validation.ts | reusable |
| `endorse` action (session-only, optimistic-CAS, atomic revision+event) | index.ts:1014 | **template for approve** |
| `/review` PR-style curation tool | wiki/src/review.ts | candidate host for the queue |

So the dissent channel is already plumbed end-to-end; it just (a) carries a bare
*concern*, not a concrete *proposed change*, and (b) dead-ends in `revisions.meta`
instead of a queue Jack can act on.

## Design

### 1. Agent emits a *structured* proposal (not just a flag)
Extend `MODERATE_AGENT1_SYSTEM`: when the agent believes a human annotation
should change, it STILL copies the human annotation verbatim (still required —
findLostHuman stays as the floor), and attaches an optional
`moderation_proposal` alongside the existing `moderation_flag`:

```json
{"moderation_proposal": {
   "fields": {"status": "formalized", "mathlib": {"decl": "..."}},
   "reason": "Mathlib now has <decl>; verified via decl_exists."}}
```

Hard rule in the prompt: a status→formalized or decl proposal MUST be
search-verified (`decl_exists` exact hit) — no proposing a decl the agent
didn't confirm exists. Bare concerns with no concrete delta keep using
`moderation_flag`.

### 2. Pipeline harvests proposals
`_preserve_human` already harvests `moderation_flag`; harvest
`moderation_proposal` the same way into `stats["proposals"] =
[{annotationId, fields, reason}]`, ride it up `meta.ladder.proposals`
(build_meta). Zero new transport.

### 3. Worker stores proposals into the dormant column
On a bot save, if `meta.ladder.proposals` is non-empty, UPSERT
`moderation_state.proposal` = JSON list of pending proposals
`{proposalId, annotationId, fields, reason, runId, model, createdAt}`.
De-dupe against already-pending and against the rejected-memory (§Anti-spam).
This is advisory data only — it does NOT touch the rendered annotations.

### 4. Approve / reject endpoint (mirror `endorse`)
`POST /api/article/:slug` with `{action, proposal_id, base_version}`,
**session-only (403 for bots)**, optimistic-CAS on version:
- `approve_proposal`: apply `fields` to the target human annotation in place,
  keep `provenance:"human"` (Jack approved it), bump version, write a
  `revisions` row (`kind:"proposal-approved"`, parentId chained), emit an
  `annotation_event` (`actorType:"human"`, `fieldChanges` = the applied delta),
  and remove that proposal from `moderation_state.proposal`.
- `reject_proposal`: drop it from pending and append `{annotationId, fieldsSig}`
  to a rejected-memory so the agent's identical re-proposal is suppressed.

### 5. UX surface — **Jack, pick one** (the only thing blocking build)
- **A. Global queue page `/proposals`** (recommended): one cross-article list,
  "AI proposes: not_formalized → formalized (decl X) on *Commutative ring*
  [approve] [reject]", ordered by article. Best triage throughput. Mirrors how
  `/api/work` ranks, but human-facing.
- **B. A tab in the existing `/review` tool**: reuses the curation surface and
  its OAuth, less new UI.
- **C. Inline banner on each article's moderation view**: maximal context, but
  you have to visit each article to find proposals.

Recommendation: **A** for triage, optionally add **C** later for context.

### Anti-spam (don't re-propose a rejected delta)
Add `moderation_state.rejected_proposals` (JSON, one `ALTER TABLE`) holding
`{annotationId, fieldsSig}` of rejected proposals. The agent prompt is told not
to re-propose a delta already in the rejected set for that annotation; the
Worker also filters re-proposals defensively at store time (§3).

## Safety invariants (must hold)
- `findLostHuman` stays the floor: a direct bot overwrite of a human annotation
  still 422s. The ONLY ways a human annotation changes are (a) a human edit, or
  (b) Jack approving a proposal. The agent never mutates a human annotation.
- Proposals are inert until approved — they live in `moderation_state`, not in
  `articles.annotations`, so they never reach the rendered page or a reader.
- Approve/reject is session-only; a bot cannot approve its own proposal (403),
  same as `endorse`.

## Migration
- `moderation_state.proposal` already exists — no migration to store proposals.
- One additive `ALTER TABLE moderation_state ADD COLUMN rejected_proposals TEXT`
  for the anti-spam memory.

## Rollout (each step independently shippable)
1. **Backend store + endpoint** (§3, §4) — inert until proposals exist; safe to
   deploy with no behavior change.
2. **Agent prompt** (§1) + **harvest** (§2), gated by `WIKILEAN_PROPOSALS=1` so
   proposals only start flowing when you flip it on.
3. **UX surface** (§5) once you've picked A/B/C.

## Open questions for Jack
1. UX surface: A (global `/proposals`), B (`/review` tab), or C (inline)?
2. Should an *approved* proposal keep `provenance:"human"` (Jack owns it now —
   my default) or get a distinct `provenance:"human-approved-ai"` for analytics?
3. Auto-approve threshold? (Default: never — every proposal is hand-approved.)
