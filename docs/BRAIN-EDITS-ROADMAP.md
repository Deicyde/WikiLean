# Roadmap — user-submitted brain edits (Project 2)

> Status: **Phases 0–4 SHIPPED + DEPLOYED 2026-07-05** (wrangler version
> b1683966; remote migration 0010 applied). Branch `feat/brain-community-edges`
> — local, awaiting merge to `main`. Full auto-nightly graduation waits on the
> brain-rebuild-in-ops backlog item; the harvest + fold are done. This is the
> plan for letting people (and scripts/agents) add connections to the Brain
> through the same GitHub-login auth used for article annotations.

## Goal

Let authenticated users and API callers **add connections to the Brain** —
between two existing nodes, or from an existing node to an external database
(LMFDB, Wikidata, nLab, MathWorld, Stacks, Kerodon, OEIS, …). API-first, so a
script or an agent can post them too. Provenance is tracked on every edge:
**who** submitted it, and **whether it's human- or AI-generated.**

### Scope decisions (Jack, 2026-07-05)

- **NO new graph nodes** (too much pollution risk). Edges only, and their
  endpoints must be existing brain nodes — except `xref` edges, whose `dst` is
  an external-database identifier (that's the "add a database entry" case).
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
- **This session: write the roadmap only.** Build after Jack confirms.

## What already exists (recon findings — reuse, don't rebuild)

- **Auth seam** `getUser(c)` (wiki/src/auth.ts): resolves a bearer `PIPELINE_TOKEN`,
  a better-auth GitHub OAuth session, or a dev cookie → `{id, name, role}` or
  `null`. This is the same identity annotations use.
- **Write guards** on `POST /api/article/:slug` (wiki/src/index.ts): `checkOrigin`
  (CSRF), `EDIT_LIMITER.limit({key:"edit:"+user.id})` (per-user rate limit),
  CAS-on-version, `db.batch([...])` atomicity, and an `annotation_events` audit
  log with a server-derived `actor_type ('human'|'pipeline')` — **provenance is
  never client-claimed.** We reuse all of this.
- **`proposals` table** (migrations/0009): the house style for our new table —
  12-hex TEXT PK, `created_at` ms, `status` enum, `decided_at`/`decided_by`,
  status/slug indexes. Migration-apply gotcha: run remote via
  `wrangler d1 execute wikilean --remote --file=…`, not `d1 migrations apply`.
- **Brain serving** (wiki/src/brain.ts): read-only `GET /api/brain/node|search`
  over static shards; **no write path today.** Node ids: `Q…`, `decl:Lib:Name`,
  `path:…`, `lit:…`. The shard set is the node-existence oracle (via `ASSETS.fetch`).
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
src         TEXT NOT NULL           -- existing brain node id (shard-validated)
dst         TEXT NOT NULL           -- brain node id, OR "xref:<db>:<value>" for cross-db
kind        TEXT NOT NULL           -- relates|formalizes|depends|mentions|matches|cites|xref
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
`src` is always shard-validated. `dst`: for `xref`, validate `db` ∈ the known
registry (`source_registry.json`) and the value shape; otherwise shard-validate.

### API (all reuse the annotation guards)

- **`POST /api/brain/edge`** — create. `getUser` (401 if none) · `checkOrigin` ·
  rate limit (per-user for browser, looser for bearer). Body
  `{src, dst, kind, evidence:{note,…}, actor_type?}`.
  - `actor_type`: **forced `human`** for an OAuth/browser session (a person
    clicked). For a **bearer/API** call it is **required** and the caller
    declares `human` or `ai` — the server can't infer intent, so the caller
    asserts it (same trust boundary as any signed API claim; misuse is
    attributable to the token).
  - Validates `src`/`dst` against the shards, `kind` ∈ enum, evidence note
    non-empty, and (xref) `db` ∈ registry. Dedupe on `(src,dst,kind)`.
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
gravestone), not a review queue. The deterministic `fold_proposals` verifier
still runs at **nightly graduation** (Phase 5) as a quality gate on what becomes
*permanent* in the static base — so unverified AI edges can appear live but don't
silently become permanent graph facts without passing the oracle checks.

### Read model (locality preserved)

Static shards stay the base layer (nightly, immutable, version-pinned). The page
already fetches per-node shards in `getEntry`; we add **one overlay fetch** to
`GET /api/brain/edges?id=` and merge — so a node's community edges appear the
instant they're posted, without loading the whole graph. Rebuild rule: **a live
(non-deleted) community edge is never dropped** by a rebuild; a `deleted`
gravestone stays a tombstone and is never resurrected (the annotation
tombstone-never-delete law, applied to edges).

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
| **1. Write/read/delete API ✅** | `POST /api/brain/edge`, `GET /api/brain/edges`, `DELETE /api/brain/edge/:id`; auth + origin + rate-limit + shard/kind/registry validation + provenance + dedupe + soft-delete gravestone; unit tests | ✅ 18 tests + adversarial security review (1 finding fixed) |
| **2. Overlay UI ✅** | overlay fetch in `renderPanel` (`renderCommunity`); community chip (added-by + human/AI); "add a connection" panel (labels search for target, kind dropdown, xref DB picker + value, evidence note) + a delete affordance on community edges | ✅ verified in preview: list renders with chips, add-form + xref toggle work, graceful-degrades when the API is absent |
| **3. Cross-pollination ✅** | `xref-shared` inference over community + static xref edges (build emits `xref_index.json`; overlay endpoint infers partners; UI "Same object elsewhere" block) | ✅ 3 tests (community↔community, community→static both ways, no false partners) + verified in preview |
| **4. Graduation ✅** | `harvest_community_edges.py`: live (non-deleted) D1 edges → `brain/data/community_edges.jsonl` (human trusted; AI through the oracle); `build_shards.py` folds graduated xrefs into `xref_index.json`; wired into the nightly (`WIKILEAN_COMMUNITY_HARVEST`) | ✅ 6 tests + offline end-to-end run (AI-to-bogus-node dropped; graduated xref appears in the static index). *Note:* the brain SHARD rebuild+deploy is not yet nightly (separate ops backlog), so full auto-graduation runs when that lands; the harvest + fold are done and on-demand-runnable |

**Suggested build order for the first PR:** Phases 0 + 1 (backend, curl-verified),
then 2 (make it real on `/brain`), then 3–4 as follow-ups.

## Invariants to hold (from the recon)

- `added_by`/`deleted_by` are **server-derived** identity (never client-claimed);
  `actor_type` is forced `human` for OAuth and explicitly declared for API.
- Durable node ids only; `src` always shard-validated; xref `dst` registry-validated.
- CAS-on-`version`, `db.batch` atomicity, rate limit, `checkOrigin` — reused verbatim.
- **Soft-delete only** — a delete never removes the row; it writes the gravestone
  (`status='deleted'`, `deleted_by/at`). A `deleted` edge is never resurrected by
  a rebuild; a live edge is never silently dropped.
- The deterministic `fold_proposals` verifier gates what AI edges become
  **permanent** in the static base (nightly graduation), not what appears live.

## Resolved (Jack, 2026-07-05)

- No patrol/moderation queue → **"added by" attribution** + **delete-with-gravestone**.
- **Looser rate limit** for API/bearer scripts (separate from the per-user browser limiter).
- AI edges live directly in `brain_edges` (dedicated row, not the annotation-shaped
  `proposals` table); the human/AI label is `actor_type`.

## Open question

- **Who may delete an edge?** Options: (a) **anyone logged in** can delete any
  edge (fully wiki-style; the gravestone makes bad deletes visible and reversible)
  — the assumed default; (b) only the **`added_by` author + admins**; (c)
  author + admins, with a separate "flag for deletion" for everyone else. Default
  = (a) open + accountable, matching the "everything live, tracked" ethos; say the
  word if you want it tighter.
