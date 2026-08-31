# Roadmap — user-submitted brain edits (Project 2)

> Status: **historical rollout record; Phases 0–4 shipped, deployed, and merged.**
> The initial production deployment was 2026-07-05 (wrangler version b1683966;
> remote migration 0010 applied). Community harvesting/folding is now wired into
> nightly operations, while Brain release deployment remains separately opt-in.
>
> **Architecture status (2026-08-31):** this document describes the current
> mutable-row overlay. Its future replacement is the release-pinned, append-only
> Phase 3 plan in `docs/ROADMAP.md`. Treat the implementation narrative below as
> an as-built record, not the active TODO list; do not extend this schema into
> promotion or rebase machinery before the complete Phase 2 contracts are frozen.

## Goal

Let authenticated users and API callers **add connections to the Brain** —
between two existing nodes, or from an existing node to an external database
(LMFDB, Wikidata, nLab, MathWorld, Stacks, Kerodon, OEIS, …). API-first, so a
script or an agent can post them too. Provenance is tracked on every edge:
**who** submitted it, and **whether it's human- or AI-generated.**

### Scope decisions (Jack, 2026-07-05)

- **New nodes: validated Wikidata QIDs only** (Jack reversed the earlier "no new
  nodes" after hitting the case first-hand — tagging a Mathlib decl with a QID
  the brain hadn't ingested, e.g. `Q5530428` GNS construction). An edge endpoint
  may be a QID not yet in the brain; the server validates it live against
  Wikidata (`wbgetentities`) and mints it into the D1 `brain_nodes` table with
  its Wikidata label. This is the safe form of new-nodes — a QID either resolves
  or it doesn't, so no free-form junk. Migration `0011_brain_nodes.sql`;
  `resolveNodeEndpoint` + `validateWikidataQid` in brain-edits.ts (KV-cached);
  the overlay returns `node_labels`; the panel renders such nodes with their
  Wikidata name + an outbound Wikidata link; the search offers a QID you paste.
- Otherwise edge endpoints must be existing brain nodes — except `xref` edges,
  whose `dst` is an external-database identifier (the "add a database entry" case).
- **The high-value case is cross-database links** (`xref`): "this Mathlib decl
  is `group.abelian` in LMFDB." Because the Brain already infers `xref-shared`
  edges when two nodes carry the *same* external page, each user-added `xref`
  can **unlock new discovered connections** through shared-database join — the
  cross-pollination Jack wants, for free, with no new nodes.
- **No patrol / no moderation queue.** Everything a logged-in user or API caller
  posts goes **live immediately**, carrying an **"added by"** attribution and a
  human/AI label. Correction happens by **deletion**, not review.
- **Deletion leaves a gravestone.** An edge is never hard-deleted; a delete flips
  it to a tombstone row carrying **"deleted by"** so every removal is attributable
  (who deleted which edge). Wiki-style: open to act, fully accountable.
- **Looser rate limit for API/bearer scripts** (a separate, higher limiter than
  the per-user browser one).

## Implementation context and current corrections

- **Auth seam** `getUser(c)` (wiki/src/auth.ts): resolves a bearer `PIPELINE_TOKEN`,
  a better-auth GitHub OAuth session, or a dev cookie → `{id, name, role}` or
  `null`. This is the same identity annotations use.
- **Write-guard reference** on `POST /api/article/:slug` (wiki/src/index.ts):
  `checkOrigin` (CSRF), per-user rate limiting, CAS-on-version, batched writes,
  and a server-derived actor. The Brain routes reuse auth, origin/rate limits,
  and server-derived identity, but current node/edge writes are mutable row
  operations rather than one atomic changeset and deletes do not accept an
  expected revision. Phase 3 in `docs/ROADMAP.md` owns that correction.
- **`proposals` table** (migrations/0009): the house style for our new table —
  12-hex TEXT PK, `created_at` ms, `status` enum, `decided_at`/`decided_by`,
  status/slug indexes. Migration-apply gotcha: run remote via
  `wrangler d1 execute wikilean --remote --file=…`, not `d1 migrations apply`.
- **Brain serving:** the supported static/SQLite read surface is
  `/api/brain/{unit,transfer,neighborhood,snippets,filter}`. The retired
  `GET /api/brain/node` route intentionally returns 404. Community writes are
  separate: `POST /api/brain/node`, `POST /api/brain/edge`, and their soft-delete
  routes. Node ids remain durable (`Q…`, `decl:Lib:Name`, `path:…`, `lit:…`).
- **Annotations are ALREADY brain edges** (brain/build_common.py:123): the nightly
  build turns `site/annotations/*.json` into `mentions`/`formalizes` edges +
  `article_annotations` node summaries. The only gap is the **live→nightly lag**;
  the overlay below (Phase 2) can close it for annotations too if we want.
- **Cross-database plumbing** already in the page: `XREF_NAME`/`XREF_URL` maps +
  `source_registry.json` (provenance SoT) + the `xrefPages` shared-page inference
  in build_brain_page.py. Community `xref` edges plug straight into it.

## Design

### `brain_edges` (D1, migration `0010_brain_edges.sql`)

```
id          TEXT PRIMARY KEY        -- 12-hex, minted server-side
src         TEXT NOT NULL           -- release node id or validated/minted Wikidata QID
dst         TEXT NOT NULL           -- same, OR "xref:<db>:<value>" for cross-db
kind        TEXT NOT NULL           -- relates|formalizes|mentions|matches|cites|xref
evidence    TEXT NOT NULL           -- JSON: {note, ...; for xref: {db, value, url}}
added_by    TEXT NOT NULL           -- users.id who added it (or 'pipeline' for a bearer token)
actor_type  TEXT NOT NULL           -- 'human' | 'ai'   ← the human-vs-AI distinction
status      TEXT NOT NULL DEFAULT 'live'  -- 'live' | 'deleted' (gravestone)
created_at  INTEGER NOT NULL        -- ms
deleted_by  TEXT                    -- users.id who deleted it (nullable; set on tombstone)
deleted_at  INTEGER                 -- ms (nullable)
version     INTEGER NOT NULL DEFAULT 1
-- indexes: (src), (dst), (status, created_at), (added_by)
```
No patrol columns — an edge is `live` from creation. A delete never removes the
row; it flips `status='deleted'` and stamps `deleted_by`/`deleted_at`, so the
gravestone preserves who added AND who removed every edge. (A `brain_edge_events`
audit table is optional; the row itself already records add + delete attribution,
so we can skip it unless we want a full history of re-adds.)

**Node identity:** edges reference durable node ids only (never session UUIDs).
Non-`xref` endpoints resolve against the selected Brain release or, for an absent QID,
through live Wikidata validation followed by a `brain_nodes` mint. For `xref`, validate
`db` ∈ the known registry (`source_registry.json`) and the value shape.

### API

- **`POST /api/brain/edge`** — create. `getUser` (401 if none) · `checkOrigin` ·
  rate limit (per-user for browser, looser for bearer). Body
  `{src, dst, kind, evidence:{note,…}, actor_type?}`.
  - `actor_type`: **forced `human`** for an OAuth/browser session (a person
    clicked). For a **bearer/API** call it is **required** and the caller
    declares `human` or `ai` — the server can't infer intent, so the caller
    asserts it (same trust boundary as any signed API claim; misuse is
    attributable to the token).
  - Resolves `src`/`dst` against the selected release or a validated QID mint,
    validates `kind` ∈ enum and (xref) `db` ∈ registry, caps an optional evidence
    note, and dedupes live rows on `(src,dst,kind)`.
  - `added_by` = the authenticated identity. Every edge is `status='live'`.
  - Returns `{ok, id}`.
- **`GET /api/brain/edges?id=<node>`** — the live D1 overlay: every non-deleted
  community edge touching the node, `Cache-Control: no-store` (live tail). The
  page merges these into panel/ego/canvas with a **"community" chip** showing
  who added it (`added by …`) and a `human` vs `AI` label.
- **`DELETE /api/brain/edge/:id`** (or `POST …/delete`) — soft-delete. Session
  or bearer, `getUser` required. Flips `status='deleted'`, stamps
  `deleted_by`/`deleted_at` (the gravestone). The row is kept; the overlay stops
  serving it. Bots/bearer may delete too (attributable via `deleted_by`).

### Provenance model (the core of the ask)

| field | source | meaning |
|---|---|---|
| `added_by` | **server-derived** from `getUser` | GitHub identity, or `'pipeline'` for a bearer token. Never client-claimed. |
| `actor_type` | OAuth session → forced `human`; API → caller-declared | Human-submitted vs AI-generated. **This is the human/AI switch users asked for.** |
| `deleted_by` | server-derived on delete | who removed the edge (the gravestone) |
| chip in UI | derived | `community · human · added by @x` vs `community · AI · added by @x`. |

Everything is live and attributed; the human/AI label lets viewers weight/filter
AI-submitted edges, and a bad edge is corrected by **deletion** (which leaves the
gravestone), not a review queue. At nightly graduation, the harvester reuses the
declaration/QID endpoint-oracle helpers for AI rows; it does not run community rows through
the complete proposal-fold state machine. Failed AI endpoint checks do not enter the next
static base.

### Read model (locality preserved)

Static release assets stay the base layer and `GET /api/brain/edges?id=` supplies the live
D1 tail. The current narrow guarantee is that the overlay immediately hides a deleted D1
row and the next harvest excludes it. A previously published static copy can remain visible
until a new release, and rollback to an older release can expose it again. Preventing
tombstone resurrection across publication and rollback is a Phase 3 requirement in the
canonical roadmap.

### Cross-pollination (Jack's insight — Phase 4)

When a community `xref` gives node A the external page `lmfdb:group.abelian`,
and node B already carries the same page, the Brain infers an `xref-shared`
edge A↔B ("same object across databases"). This is the existing `xrefPages`
logic; we extend it to include community xref edges in the overlay and, after
graduation, in the nightly build. **Each accepted cross-db link can surface new
connections with no new nodes.**

## Phased plan

| Phase | Deliverable | Acceptance |
|---|---|---|
| **0. Schema ✅** | `0010_brain_edges.sql`; Drizzle types; apply local + remote (per gotcha) | migration applies; table queryable |
| **1. Write/read/delete API ✅** | `POST /api/brain/edge`, `GET /api/brain/edges`, `DELETE /api/brain/edge/:id`; auth + origin + rate-limit + release/QID/kind/registry validation + provenance + dedupe + soft-delete gravestone; unit tests | ✅ 18 tests + adversarial security review (1 finding fixed) |
| **2. Overlay UI ✅** | overlay fetch in `renderPanel` (`renderCommunity`); community chip (added-by + human/AI); "add a connection" panel (labels search for target, kind dropdown, xref DB picker + value, evidence note) + a delete affordance on community edges | ✅ verified in preview: list renders with chips, add-form + xref toggle work, graceful-degrades when the API is absent |
| **3. Cross-pollination ✅** | `xref-shared` inference over community + static xref edges (build emits `xref_index.json`; overlay endpoint infers partners; UI "Same object elsewhere" block) | ✅ 3 tests (community↔community, community→static both ways, no false partners) + verified in preview |
| **4. Graduation ✅** | `harvest_community_edges.py`: live (non-deleted) D1 edges → `brain/data/community_edges.jsonl` (human trusted; AI through the oracle); `build_shards.py` folds graduated xrefs into `xref_index.json`; wired into nightly moderation (`WIKILEAN_COMMUNITY_HARVEST`) | ✅ 6 tests + offline end-to-end run (AI-to-bogus-node dropped; graduated xref appears in the static index). A later Brain release incorporates the harvested file; production release deployment remains opt-in and is currently blocked on `docs/ROADMAP.md` P1A. |

The original build order—backend, UI, cross-pollination, then graduation—is complete.
All new architecture work is tracked in `docs/ROADMAP.md`.

## Invariants to hold (from the recon)

- `added_by`/`deleted_by` are **server-derived** identity (never client-claimed);
  `actor_type` is forced `human` for OAuth and explicitly declared for API.
- Durable node ids only; non-`xref` endpoints are release-resolved or validated/minted QIDs,
  while xref `dst` values are registry-validated.
- Rate limits, `checkOrigin`, authentication, and server-derived identity remain mandatory.
  The current overlay's lack of atomic append-only changesets and expected-revision CAS is
  known migration debt, not an invariant to preserve.
- **Soft-delete only in D1** — a delete never removes the row; it writes the gravestone
  (`status='deleted'`, `deleted_by/at`). Cross-release no-resurrection is not yet guaranteed
  and is explicitly owned by Phase 3.
- Nightly graduation applies additional endpoint-oracle checks to AI rows before they enter
  the next static base; this is narrower than the full `fold_proposals` state machine.

## Resolved (Jack, 2026-07-05)

- No patrol/moderation queue → **"added by" attribution** + **delete-with-gravestone**.
- **Looser rate limit** for API/bearer scripts (separate from the per-user browser limiter).
- AI edges live directly in `brain_edges` (dedicated row, not the annotation-shaped
  `proposals` table); the human/AI label is `actor_type`.

## Deletion policy (resolved)

Any authenticated user may soft-delete any community edge or community-added node. The
gravestone records the actor. Phase 3 must preserve that history while adding stable
assertion identity, expected revisions, and deterministic restore/rebase behavior.
