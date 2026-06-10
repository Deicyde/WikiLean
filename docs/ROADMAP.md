# WikiLean Development Roadmap

> **Living document.** Produced 2026-06-10 from a full architecture audit (8 investigation
> agents + 2 adversarial verifiers, cross-checked against the codebase). Update statuses
> as work lands; do not re-litigate the binding decisions below without new evidence.

## End goal (verbatim from Jack)

A complete interface mapping Wikipedia statements to their formal implementation in
Mathlib (and eventually CSLib, PhysLib, etc.). Primarily AI-moderated via three routine
operations: **(1)** generate annotations for new articles, **(2)** review previous
articles and correct mistakes, **(3)** update articles as Wikipedia's content changes.
Anyone can donate compute ("token donations": run the script locally on their own
Claude account/API key). Humans can quickly correct errors on the site. The project is
an **experiment in human+AI database moderation** — collecting data about that
interaction is a first-class goal. Clean UI is key.

## The central finding

The human+AI loop is currently **severed in four places**:

1. **AI moderation never sees human edits.** Pipeline reads `site/annotations/*.json`
   (disk); humans write only to D1. No D1→disk path exists, so `_preserve_human`
   (batch_annotate.py) has never run against a real human edit.
2. **Human-edited articles are frozen out of AI review forever** —
   `seed-refresh.ts` permanently skips slugs with a `user_id IS NOT NULL` revision.
3. **The review selector self-terminates** — `find_old_articles()` only selects
   pre-v3 articles; nothing records "last reviewed at."
4. **Wikipedia-update tracking is 0% built** — revids pinned forever at four layers
   (local HTML cache, WP_HTML KV, D1 COALESCE, save handler); nothing detects drift.

## Binding architecture decisions (verifier-adjudicated)

These resolved real conflicts between competing proposals. Build each thing **once**:

- **D1-direct pipeline, not disk-canonical.** The pipeline reads via
  `GET /api/article/:slug.json` and writes via bearer-authenticated
  `POST /api/article/:slug` with `expected_version`. Disk files demote to
  cache/backup artifacts. `seed-delta`/`seed-refresh` retire to legacy migration tools.
- **One optimistic-concurrency mechanism.** Field name `base_version`; server returns
  409 + current `{version, annotations}` on mismatch. Same contract for editor and
  pipeline. (Four investigators proposed this independently under different names.)
- **One work table** (`moderation_state`: last_reviewed_at, last_reviewed_version,
  wp_latest_revid, wp_drifted, flag_count, state, proposal) + `GET /api/work` with the
  priority policy in one ORDER BY: **flagged > drifted > human-edited-since-review >
  oldest-reviewed > new**. No separate `article_updates` table; the claim-based `jobs`
  table is deferred behind the first-real-donor trigger and replaces /api/work's
  internals, not the runner.
- **One runner script** — `site/moderate.py` with subcommands `new | review | wp-update`,
  flags `--auth subscription|api-key` and `--mode trusted|contributor`. No separate
  contrib_runner.py.
- **One bearer scheme.** Start with a single `PIPELINE_TOKEN` Worker secret checked in
  `getUser` (single auth seam, auth.ts). Graduate to an `api_tokens` table with a
  `scope` column when a second token-holder exists.
- **One status enum**, defined once and imported by wrap.ts, the save validator, and
  any future gauntlet: `{formalized, partial, not_formalized}` now; `rejected`
  (human-deletion tombstones) is added **in the same patch** that ships tombstones +
  the wrap-skip, never separately.
- **One revid policy (the most important invariant in the system):**
  `articles.revid` advances **only atomically with a re-anchored annotations payload**.
  Stale-but-consistent is the product guarantee. `latest_revid` /
  `last_upstream_check` columns may be written freely (they never bump `version` and
  the staleness UI is injected per-request, never baked into the cached base page).
- **One Mathlib decl index artifact** (~300k names from doc-gen4 declaration-data):
  sharded static files consumed by editor autocomplete AND server-side
  hallucinated-decl validation. The current mathlib-index.json (4,598 self-bootstrapped
  decls) is an autocomplete boost tier, not an oracle.
- **Token donations: never take custody of keys.** Local runner with the donor's own
  key/subscription is v1. The safe "donate a key" is a GitHub Actions fork template
  (key lives in the donor's fork secrets). Claude subscription credentials are
  categorically un-donatable. No server-side key vault, ever, unless demand forces a
  re-evaluation priced as a major ongoing security commitment.
- **Attribution lives at revision level** (`revisions.kind` + `meta` JSON), never in
  the annotation-level provenance enum — `_preserve_human` and the PRIORITY ladder do
  exact string matching on `human`/`ai-moderated`/`ai`.
- **Schema v4 (multi-library `formalizations[]`) is deferred** and must be **bundled
  into the annotation-ID backfill migration** (one corpus rewrite, not two). Written
  trigger: CSLib covers a typical undergrad algorithms course. Wikidata property
  proposal stays Mathlib-only (one external-ID property per library is the Wikidata
  convention).

---

## P0 — Before public announcement  `[DEPLOYED 2026-06-10 — version 99a27390]`

Security + embarrassment fixes. Shipped to production via 4 parallel agents (disjoint
file ownership) + adversarial integration review + live smoke test.

- [x] **XSS-1 (launch blocker):** `status`/`provenance` now htmlEscape'd in wrap.ts
  attributes; `a.status` escaped in script.js. **DEPLOYED.**
- [x] **XSS-2 (launch blocker):** `a.mathlib_url` escapeHtml'd at the script.js href
  sink; `mathlibDocsUrl` now encodeURIComponent's module segments + decl fragment.
  **DEPLOYED.** (Smoke-tested: cross-origin POST→403, normal anchors intact.)
- [x] **Server-side validation** in POST /api/article/:slug: shared
  ANNOTATION_STATUSES enum (incl. `rejected` for future tombstones), per-field caps
  (MAX_FIELD_LEN=300 ids / MAX_TEXT_LEN=2000 free-text — raised from a too-tight 200
  that the review caught would 400 saves on 120 articles with long notes), payload
  caps (256 KB / 2000 → 413). Re-validated: 0 violations across all 1,369 files.
- [x] **Optimistic concurrency:** `window.__WL_VERSION__` injected; editor sends
  `base_version`; CAS-guarded UPDATE (`WHERE slug=? AND version=?`, 0-change→409);
  409 returns `{error:'stale', version, annotations}`; client reloads. Back-compat:
  absent base_version writes unconditionally. **DEPLOYED.**
- [x] **Role gate:** `requireRole` in auth.ts; revert gated to patroller/admin (403
  else); `role='blocked'`→getUser returns null (anonymous everywhere). Save stays
  open. **Jack seeded as admin (jack.mccarthy107@gmail.com).** DEPLOYED.
- [x] **app.onError** structured {event:'error'} logging + clean 500. DEPLOYED.
- [x] **Origin-header allowlist** on both write endpoints (vs request URL origin);
  `useSecureCookies:true` + `sameSite:'lax',secure:true` on better-auth. DEPLOYED.
- [x] **Editor: panel close** — × button + Escape-to-close + Cmd/Ctrl+Enter-save.
- [x] **Editor: save() spread fix** — `{...original, ...built}` preserves unknown
  fields (proof_note etc.). DEPLOYED.
- [x] **CC BY-SA attribution footer** on every article page (Wikipedia link + CC
  BY-SA 4.0 + annotations CC0). DEPLOYED + smoke-tested present.
- [x] **Data-collection notice** in editor panel footer + CONTRIBUTING.md ("Data &
  research notice") + token-donation policy ("Donating compute", marked planned).
- [x] **Mobile triage CSS** — bottom-sheet panel + wrapping bar @640px; overflow-x
  table wrappers in pages.ts. DEPLOYED.
- [x] **Cache prefix bump v6→v7** + asset `?v=` bumps (style/script v4, review v3,
  editor v5). DEPLOYED — evicts any XSS-poisoned cached pages.
- [x] **Deploy + verify live** (version 99a27390) + pre-deploy backup
  (backups/wikilean-20260610T070201Z.sql, 32 MB).

**P0 manual follow-ups:**
- [x] `CLOUDFLARE_API_TOKEN` repo secret added (account API token; needed **D1
  Edit**, not just Read — export creates a job via POST). Workflow verified
  end-to-end 2026-06-10: run 27260232307 green, 5 MB artifact (32 MB raw),
  nightly cron live at 08:27 UTC. Workflow made self-contained (no dependency on
  wiki/ in the checkout) and committed to main (c79296c).
- [x] **Backend committed to git (2026-06-10).** Jack's standing instruction: commit
  everything to Deicyde/WikiLean going forward — maximum version control. Excluded
  (gitignored): secrets (.dev.vars), node_modules/.venv/.wrangler/backups,
  re-fetchable site/cache/*.html + derived sections.json (351 MB + 35 MB),
  generated site/out (241 MB), generated seed/delta/refresh.sql, wiki/public build
  output. INCLUDED deliberately: site/cache/*.meta.json revid sidecars (5.4 MB —
  NOT reproducible; they pin which Wikipedia revision each article was annotated
  against). Secret scan run pre-commit: no values, only env-var name references.
- [ ] Minor: bump actions/checkout + setup-node for the Node 24 runner migration
  (GitHub deprecation notice; forced June 16, 2026 — low stakes, fold into next PR).

**P0 asset pipeline note:** canonical sources are `site/assets/{script.js,review.css,
style.css}` and `wiki/assets/editor.js`; `wiki/scripts/build-public.ts` copies them
into `wiki/public/assets/`. Edit sources, then run build-public, never edit
`wiki/public/assets/` directly.

## P1 — Close the loop (the core re-architecture)

- [ ] **One-time rescue pull** (`wiki/scripts/pull-annotations.ts`): materialize D1 →
  disk once. Unfreezes the 7 user-edited slugs, creates the 47 missing sidecars
  (709 D1 rows vs 662 disk slugs), commits human edits to git. Then demotes to
  backup tool. Run BEFORE any further --moderate run.
- [ ] **Stable annotation IDs** (12-hex): one-shot D1+disk backfill, Worker lazy-heal
  on save, pipeline stamps new annotations, editor preserves via spread fix, agent
  echo-validation post-pass (unknown id → NEW; dropped id → _preserve_human
  re-insert). **Prerequisite for:** annotation_events, tombstones, flags-by-id,
  patrol diffs, v4 migration.
- [ ] **Worker API read/write path:** GET /api/article/:slug.json; PIPELINE_TOKEN
  bearer branch in getUser (backed by a real 'pipeline' users row); bot POSTs carry
  expected_version + explicit revid. Sequence: revisions kind/meta backfill must
  precede first bearer write (user_id='pipeline' breaks NULL-keyed conventions).
- [ ] **Server-side provenance stamping keyed on actor:** session saves force
  provenance='human' on changed/new annotations only (diff by id); bearer writes
  never get 'human'. Human-preservation assertion in the POST handler (server-side
  twin of _preserve_human: no write may lose a provenance='human' annotation, with
  an explicit carve-out later for anchor-only rewrites by update jobs).
- [ ] **Tombstones:** editor delete → {id, anchor, provenance:'human',
  status:'rejected'} instead of splice; wrap.ts + render.py + coverage counts skip
  rejected; moderation prompt treats it as a veto; add 'rejected' to the shared enum
  in the same patch.
- [ ] **moderation_state table + GET /api/work** (priority ORDER BY as decided above).
- [ ] **Unified runner `site/moderate.py`** (new|review|wp-update; --auth; --mode;
  budget/abort semantics inherited from batch_annotate.run; WIKILEAN_MATHLIB env
  replaces the hardcoded path; mathlib_sha + model + prompt_sha recorded per run).
  find_old_articles() replaced by the D1-backed selector. seed-delta/refresh retire.
- [ ] **Wikipedia drift detection:** Worker cron (prop=info lastrevid, 50 titles/req;
  ~15 req/day at 709 articles; free-plan chunking via KV cursor if needed) writing
  articles.latest_revid/last_upstream_check (never bumps version). Capture
  redirect/missing flags → page-move/deletion states.
- [ ] **Stage-0 re-pin** in moderate.py wp-update: fetch new-revid HTML (render.py
  gains target_revid param + revid-keyed cache), dry-run wrap; if matched==total,
  apply with new revid via bot POST. Stages 1-2 (TextQuoteSelector fuzzy ≥0.95 /
  AI semantic judge) wait for anchor-rot telemetry to prove need. Hazard on record:
  'if'→'iff' edits keep high text similarity but invalidate the formalization.
- [ ] **Anchor-rot telemetry:** structured render log {slug, version, matched, total};
  articles.anchored_count written only from live-pinned renders.
- [ ] **Staleness banner** (per-request injection, post-cache) with one-click
  Wikipedia ?diff=cur&oldid= link.
- [ ] **Dynamic homepage/sitemap from D1** — new articles are currently invisible
  (static index.html/sitemap.xml); lifecycle 1 is incomplete without this.
- [ ] **Integration test harness** (miniflare/wrangler-dev D1): the edit-safety
  invariant as an automated test — seed → human save → moderate → push → assert
  human annotation intact. Land BEFORE the POST-handler refactor stack.
- [ ] **WP_HTML TTL** (e.g. 90d) + delete-old-key on re-pin (unbounded KV growth).
- [ ] **Token-budget memo:** tokens/article (from cache/.batch_run.log) × corpus ×
  cadence vs Max-plan limits. Gates the "AI-moderated" claim and sizes donations ask.
- [ ] Fix serveArticle double-read race (pass the row into renderArticleBase).
- [ ] Remove the GET-path revid write (index.ts ~47-49) once seeding guarantees revid.
- [ ] discover_articles.py: diff live WikiProject Math list vs D1 → feeds moderate.py new.

## P2 — Experiment instrumentation + contribution UX

- [ ] **One revisions migration:** kind (edit|revert|seed|contribution|pipeline),
  meta TEXT (run_id, model, tokens, cost, mathlib_sha, auth_mode), parent_id, run_id.
  Backfill (comment LIKE 'revert to #%' → revert; user_id IS NULL → seed) BEFORE
  first bearer write.
- [ ] **annotation_events table:** server-side field-level diffs at save time, keyed
  by annotation id; event_type add|modify|delete|endorse|revert_restore; actor_type
  from session-vs-bearer. Provenance-only flip = 'endorse' (the cleanest
  human-agreement signal; currently indistinguishable from an edit). Runs for ALL
  POST paths.
- [ ] **Ladder stats:** _preserve_human returns {restored, reinserted} + ids;
  downgrades_blocked from the PRIORITY pass; moderation_flag counts → rec['ladder']
  + decisions sidecar. (Reads zero until P1 ships — that's expected, not a bug.)
- [ ] **decisions.jsonl** per AI pass: annotation_id, run_id, pass, model, prompt_sha,
  confidence (uncalibrated covariate), considered-candidates (stripped from stored
  blob by a deterministic post-pass), truncated tool transcript. pipeline_runs
  registry in D1.
- [ ] **Anonymous flag pipeline:** flags table keyed by annotation_id (anchor_hash
  fallback), POST /api/flag/:slug (no auth; IP-keyed limiter; Origin check;
  Turnstile as escalation), ⚑ micro-form in tooltip (2 taps, mobile-first), /flags
  patrol page; flag_count feeds /api/work priority.
- [ ] **Patrol tooling:** GET /:slug/diff/:fromId/:toId field-level diff (pure read
  over revision snapshots); /recent-changes filter by revisions.kind (NOT user_id
  nullness); patrolled_by/at columns + mark-patrolled gated on role; one /patrol
  surface for both revisions and (later) contributions.
- [ ] **Full Mathlib decl index** (doc-gen4 declaration-data → sharded static assets
  + server lookup; curated 4,598 as boost tier regenerated from D1, not disk;
  on-blur existence tick in editor).
- [ ] **/stats page** with per-research-question live counts (zero count after the
  feature ships = broken instrumentation) + nightly pseudonymized research export
  riding the backup workflow. docs/research-plan.md (RQ1-RQ8: correction rates by
  field, endorse-vs-modify ratio, ladder blocks, time-to-correction survival,
  AI-vs-AI agreement, confidence calibration, cost per accepted annotation).
- [ ] Editor save UX: kind/match_kind as selects; clear comment after save; panel
  title by label not index; orphaned-anchor re-select flow; alt-click links;
  in-place body swap (deliberate refactor with initAnnotations(), NOT a line item).
- [ ] Trust signals: "N/M human-reviewed" badge; legend popover; least-reviewed list
  on the (now dynamic) homepage.
- [ ] Privacy: stop storing session ip_address if better-auth allows; IRB exemption
  filed before any paper.

## P3 — Deferred, with written triggers

| Item | Trigger |
|---|---|
| jobs/contributions/api_tokens queue + validation gauntlet + trust ladder (design is written — see audit; atomic claim via single UPDATE…RETURNING) | First real compute donor asks |
| GitHub Actions donation fork template; Batches-API path for tool-free Agent 1 (50% cost) | First donor on the Actions path |
| Schema v4 `formalizations[]` + libraries registry (write docs/schema_v4.md anytime) | CSLib covers a typical undergrad algorithms course; bundle migration with ID backfill if still pending |
| Mass-revert: offline admin script over revision snapshots (NOT a deployed endpoint; D1 Time Travel is whole-DB and not a substitute) | First vandalism spree |
| Re-anchoring stages 1-2 (fuzzy + AI semantic) | Anchor-rot telemetry shows stage-0 clears <90% |
| WikiProject CS corpus ingestion, Agent 2 multi-checkout, library picker UI | With schema v4 trigger |
| physlib evaluation (its "informal definition" stubs must NOT count as formalized — needs a distinct match_kind) | When physlib registration is proposed |

## Standing risks & invariants (check before touching these areas)

- Any change to wrap output bytes requires a render-cache prefix bump (render:vN).
- Any new D1 write path outside the Worker must bump `version` or readers see stale
  pages for up to 30 days.
- editor.js / review.css / script.js changes require `?v=` bumps (pages.ts and
  engine/page.ts).
- Never re-seed D1 from disk; transform D1 blobs in place (human edits live only in D1
  until the rescue pull, and are canonical in D1 always).
- revisions.user_id NULL = system convention is load-bearing until the kind/meta
  migration lands; retire NULL-keyed logic and backfill in the same change.
- The ~9 pending D1 schema changes go through ordered wrangler d1 migrations.
- Sequencing hazard: do NOT remove seed-refresh's user-edit skip or run --moderate
  against user-touched slugs before the D1 read path is live and verified.
- EDIT_LIMITER is per-isolate (advisory, not global); don't treat it as a hard cap.
- Wikipedia page moves/deletions: drift cron must handle redirect/missing or the
  update loop wedges on first contact.

## Status log

- 2026-06-10 — Roadmap created from architecture audit. P0 started.
- 2026-06-10 — **P0 deployed** (Worker version 99a27390). 4 parallel coding agents on
  disjoint files (backend/auth, render/XSS, editor/frontend, ops/docs) → adversarial
  integration review (found + fixed the MAX_FIELD_LEN=200 blocker) → deploy → live
  smoke test (XSS sinks escaped, 403 cross-origin, CC BY-SA footer, 401 anon). Jack
  promoted to admin. Pre-deploy D1 backup taken.
- 2026-06-10 — **VERSION-CONTROL RISK SURFACED:** `wiki/`, `site/`, `docs/`,
  `CONTRIBUTING.md` are untracked in git — the whole live backend has never been
  committed. P0 (and everything before it) exists only in the working tree + the live
  Worker + D1 backups. Recommend an initial commit of the backend on the `p0-hardening`
  branch before P1. (Not done autonomously — it's a large one-time decision for Jack:
  what to track, `.gitignore` for `.dev.vars`/`.wrangler/`/`backups/`, etc.)
