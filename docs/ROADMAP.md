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
  never-moderated > oldest-reviewed**. (REVISED 2026-06-12 with evidence, per the
  review pass: never-moderated sorts before stale-reviewed — every article already
  carries one pipeline annotation pass, so first-moderation coverage beats re-review;
  the original wording had "new" last. Also: "flagged" means flagged-SINCE-last-review,
  or open flags livelock the queue front; moved/deleted/needs_human states are
  excluded from selection entirely.) No separate `article_updates` table; the
  claim-based `jobs` table is deferred behind the first-real-donor trigger and
  replaces /api/work's internals, not the runner.
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

- [x] **One-time rescue pull** — DONE 2026-06-10. `wiki/scripts/pull-annotations.ts`
  (`npm run pull`); 709 rows pulled: 47 sidecars created, 8 real content updates
  (the 7 user-edited slugs' human edits rescued + Tangent_bundle stale-sidecar fix),
  manifest at site/annotations/.d1_pull_manifest.json. Human edits now in git.
- [x] **Stable annotation IDs** — DONE 2026-06-11, applied to production: 31,394 ids
  across 706 articles (CAS-guarded SQL, idempotent re-run verified, zero drift).
  Worker lazy-heal ADOPTS stored ids on sig-match (identity continuity), mints
  fresh only for new; malformed/duplicate → 400. Editor stamps on add. Runner
  echo-validates (unknown/missing id → inherit-by-sig else fresh).
- [x] **Worker API read/write path** — DONE 2026-06-10 (deployed 613da078). GET
  /api/article/:slug.json (public); bearer branch in getUser vs PIPELINE_TOKEN
  secret (backed by users row 'pipeline', role 'bot'; kill switch = delete row or
  role='blocked'); bot POSTs REQUIRE base_version (400), may carry revid (atomic
  re-pin in the same UPDATE) + meta (revisions.meta, 16KB cap); revisions
  kind='pipeline'/'edit'/'revert' + parent_id stamped. Token in wiki/.dev.vars.
- [x] **Server-side provenance stamping + human preservation** — DONE. Session
  saves: changed/new → forced 'human', unchanged keep stored provenance (anti-
  laundering both directions; judged with provenance stripped). Bot saves:
  provenance verbatim + findLostHuman 422 if any stored human annotation
  (incl. tombstones) is missing or altered (deep-equal, id-else-anchor-sig match).
  Anchor-only carve-out for update jobs still TODO when stage-1 re-anchoring lands.
- [x] **Tombstones** — DONE. Editor delete on persisted annotations → status
  'rejected' + provenance 'human' (spread-preserving); never-persisted still
  splice. Both engines skip rejected in lockstep (matched=true semantics — not
  anchor rot); excluded from badges + anonymous __WL_ANNOTATIONS__ (null
  placeholder keeps data-anno-indices aligned); editors still see/undo vetoes.
- [x] **moderation_state + GET /api/work** — DONE. Bot-only; modes review|wp-update;
  priority: flag_count DESC, wp_drifted DESC, human-edited-since-review,
  last_reviewed_at ASC NULLS FIRST; per-row reason string. Bot saves upsert
  last_reviewed_at/version + conditional wp_drifted reset.
- [x] **Unified runner `site/moderate.py`** — DONE (new|review|wp-update|all;
  --auth subscription|api-key via guarded key-pop; WIKILEAN_MATHLIB env; ID3 meta
  with ladder + id-discipline stats; 409/422/429 handling; D1-backed selection via
  /api/work). update_old_annotations.py deprecated; seed-delta/refresh retired to
  legacy. KNOWN GAP: `new` mode has no D1 create path — POST 404s on unknown slugs
  (Wave D: bot-only article-create endpoint or seed-delta handoff).
- [x] **Wikipedia drift detection** — DONE (cron 17 6 * * * deployed; first tick
  pending). wiki/src/drift.ts: prop=info batches of 50, ≤8 batches/run with KV
  cursor (drift:cursor in RENDER_CACHE), full sweep every ~2 days at 709 articles.
  Drifted → latest_revid + moderation_state.wp_drifted=1; missing → state
  'deleted'; redirect → 'moved' (NB: redirects=0 param deliberately OMITTED —
  MediaWiki treats presence as true). Never bumps version. Staleness banner
  injected per-request when latest_revid > revid, with ?diff=cur&oldid= link.
- [x] **Stage-0 re-pin** — DONE (site/update_from_upstream.py; render.py gained
  target_revid + revid-keyed cache, legacy path byte-identical). FIRST PRODUCTION
  RUN 2026-06-11: 8/10 drifted articles re-pinned cleanly (incl. 102/102 anchors on
  Algebraic_K-theory); 2 held back with failing anchors recorded to
  .wp_update_report.jsonl. Stages 1-2 still gated on telemetry volume. Hazard on
  record: 'if'→'iff' edits keep high text similarity but invalidate formalization.
- **DRIFT REALITY CHECK (cron tick 1, 2026-06-11):** 145 of the first 400 articles
  (36%) had drifted from their pinned revisions. Upstream churn is much higher than
  assumed — wp-update is a first-class workload, not an edge case. Stage-0 clears
  ~80% of drift for zero tokens (first-run sample).
- [ ] **Anchor-rot telemetry:** structured render log {slug, version, matched, total};
  articles.anchored_count written only from live-pinned renders.
- [ ] **Staleness banner** (per-request injection, post-cache) with one-click
  Wikipedia ?diff=cur&oldid= link.
- [x] **Dynamic homepage/sitemap from D1** — DONE 2026-06-12 (Wave D). GET / and
  /sitemap.xml render from per-article count columns (KV-cached 5min/1h); static
  copies removed from build-public so the Worker routes aren't shadowed.
- [x] **Integration test harness** — DONE (Wave A, extended every wave since;
  111 tests incl. the full edit-safety cycle: seed → human save → bot echo →
  intact / bot drop → 422).
- [x] **WP_HTML TTL** (90d, Wave A) + delete-old-key on re-pin (Wave D).
- [ ] **Token-budget memo:** tokens/article (from cache/.batch_run.log) × corpus ×
  cadence vs Max-plan limits. Gates the "AI-moderated" claim and sizes donations ask.
- [x] Fix serveArticle double-read race (Wave A).
- [x] Remove the GET-path revid write (Wave D; all 709 revids verified non-null).
- [x] discover_articles.py (Wave C) → feeds moderate.py new --from-file (Wave D).

## P2 — Experiment instrumentation + contribution UX

- [ ] **One revisions migration:** kind (edit|revert|seed|contribution|pipeline),
  meta TEXT (run_id, model, tokens, cost, mathlib_sha, auth_mode), parent_id, run_id.
  Backfill (comment LIKE 'revert to #%' → revert; user_id IS NULL → seed) BEFORE
  first bearer write.
- [x] **annotation_events table** — DONE 2026-06-12 (Wave D, migration 0005).
  Field-level diffs by annotation id on every write path; event types add|modify|
  delete|endorse|reject|revert_restore; actor session-vs-bearer. Endorse is now an
  explicit action (POST {action:'endorse', annotation_id, base_version}) since
  stampProvenance deliberately reverts bare provenance flips.
- [x] **Ladder stats** — DONE (Waves C + fix wave; moderation_flag dissent
  harvested per F14; flows into revisions.meta and the decisions sidecar).
- [x] **decisions.jsonl + pipeline_runs** — DONE 2026-06-12 (P2 wave): per-article
  decision lines (outcome taxonomy posted|noop|409-rebased|422|error|dry-run) in
  site/cache/.decisions.jsonl; pipeline_runs table (migration 0006) + POST /api/runs
  (idempotent); runner registers real runs, tolerates pre-deploy 404s.
  DEFERRED pieces: per-annotation confidence/considered fields + tool transcripts
  (need an Agent-2 output-schema change; see research-plan RQ6/RQ7).
- [x] **Anonymous flag pipeline** — DONE 2026-06-12 (Wave D). flags table by
  annotation_id; POST /api/flag/:slug (no auth, FLAG_LIMITER 5/min/IP, 5-open cap
  silent); ⚑ micro-form in the tooltip; /flags patrol queue with role-gated
  resolve; flag_count feeds /api/work priority. Verified live end-to-end.
  Turnstile remains the documented escalation if abuse appears.
- [x] **Patrol tooling** — DONE (diff pages Wave D; kind filter + patrolled_by/at
  + mark-patrolled with CAS, P2 wave migration 0006).
- [x] **Full Mathlib decl index** — DONE 2026-06-12: 411,273 decls from doc-gen4
  declaration-data.bmp, 849 recursive-prefix shards (<400KB each) in
  public/assets/decl-index/ (rebuild: npm run build:decl-index); editor
  autocomplete = curated boost tier + full-index shards + on-blur existence tick
  (never blocks saves). Server-side oracle consumption deferred to the
  contribution gauntlet.
- [x] **/stats + research export + research plan** — DONE 2026-06-12: /stats
  (public, RQ-labeled live counts, 300s KV cache); GET /api/research/export.jsonl
  (bot/admin, streamed, pseudonymized — sha256(user_id+salt), no PII) + nightly
  artifact riding backup-d1.yml (PIPELINE_TOKEN repo secret set);
  docs/research-plan.md (RQ1-RQ8 with exists-today status per question).
  NOTE: annotation_events is legitimately ZERO so far (re-pins echo verbatim,
  no-op reviews skip events) — first substantive edit starts the dataset.
- [ ] Editor save UX: kind/match_kind as selects; clear comment after save; panel
  title by label not index; orphaned-anchor re-select flow; alt-click links;
  in-place body swap (deliberate refactor with initAnnotations(), NOT a line item).
- [ ] **Propose-then-approve: AI may propose updates to human annotations** —
  designed in [propose-then-approve.md](propose-then-approve.md), awaiting Jack's
  UX pick (§5) before build. Foundation already exists (dormant
  `moderation_state.proposal` column + F14 `moderation_flag` harvest + `endorse`
  template); the agent never mutates a human annotation — it proposes, Jack
  one-click approves. `findLostHuman` 422 stays the floor. Jack's directive:
  "human-curated does NOT mean it shouldn't be updated by reviewers."
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
| **/review posting: replace the `public_repo` OAuth App with a least-privilege GitHub App.** Today the review OAuth app holds `public_repo` = write to ALL the reviewer's public repos (over-broad), and is blocked anyway by leanprover-community's OAuth-App access restrictions. A GitHub App can request **Pull requests: write only**, and **user-to-server** tokens still post *as the reviewer* (preserving attribution + settle.py's maintainer-by-author gate). CAVEAT (researched 2026-06-20): GitHub Apps do NOT bypass org approval — writing to leanprover-community still needs the app *installed* on that org (owner action). So this doesn't make the button work unilaterally; it makes the ask least-privilege + revocable per-repo instead of "write to all your public repos." Copy review stays the no-approval fallback. | A leanprover-community owner will install an app, OR before opening /review to reviewers beyond Jack + maintainers |

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

## Review + eval infrastructure (added overnight 2026-06-12)

- **Four-agent review pass** (moderation workflow + UI, both adversarial): 16
  workflow findings (4 HIGH: wrong-revision reviews F1, flagged-queue livelock F2,
  drift-tier token misdirection F3, moved/deleted unconsumed F4) + 12 UI findings
  (4 pre-announcement: .html flag 404s, unreachable tombstone recovery,
  guaranteed-fail revert buttons, keyboard-inaccessible tooltips) + 3 test-agent
  findings (anchorSig anchors[] blind spot, no-op save churn, bot /flags access).
  All triaged into one fix wave. The "verified-solid" lists from both reviews are
  in the agent reports (session transcripts) — the 422 human-preservation core
  held under every constructed interleaving.
- **Test suites**: 334 Worker tests (authz matrix 13 endpoints × 8 actors;
  /api/work ladder; annotation_events integrity incl. zero-events-on-failure;
  boundary sweeps) + 45 Python tests + cross-language parity harness
  (wiki/test/fixtures/parity.json, 74 cases consumed by BOTH vitest and Python —
  pins the three sig/match/equality implementations against drift; 6 genuine
  divergences found and pinned, 1 crash-grade fixed in the fix wave).
- **Moderation evals**: site/eval_moderation.py --offline — 6 planted-defect
  scenarios (drop-human, tombstone-resurrect, id-rename, provenance-downgrade,
  create-launder, coverage-extend) through the REAL deterministic pipeline,
  scorecard + non-zero exit as a CI gate. --live mode exists but is token-gated;
  never run it in CI. Routine commands:
  `python3 site/test_moderate.py && python3 site/test_parity.py &&
  python3 site/eval_moderation.py --offline` and `cd wiki && npm test`.

## Status log

- 2026-06-16 — **Search-verified moderation + nightly automation live.** Reviewer
  search skills (.claude/skills/{mathlib,wikidata,wikipedia}-search) built and
  wired into Agent 2 as SDK custom tools (site/search_tools.py) — quality read of
  a real batch confirmed they FIX stale/hallucinated Mathlib decls
  (Basis.ofVectorSpace→Module.Basis.ofVectorSpace etc.), not churn. Durability:
  checkpoint-and-retry-POST (moderate.py flush) + run-level revert endpoint
  (deployed). NIGHTLY launchd schedule live (site/ops/, 03:00 local, flush→
  wp-update→review, token-capped) — needs Full Disk Access on /bin/bash (Desktop
  TCC); verified end-to-end (Max auth works under detached launchd). Security:
  wiki login narrowed to identity-only — public_repo dropped (it was leaking
  repo-write to every editor via the shared review-tool OAuth; that tool now
  needs its own GitHub OAuth app).

- 2026-06-12 (overnight autonomous run) — **UI redesign + review pass + eval
  infrastructure + fix wave + P2 completion, all deployed.** (1) Warm
  academic-minimalist redesign across homepage/shells/article chrome. (2)
  Four-agent adversarial review: 31 findings; the fix wave closed all of them —
  headline: F1 reviews-against-wrong-revision (verified fixed in production:
  suffixed pinned-revid cache), F2 flagged-queue livelock (verified released
  live), F3 stage-0 wired into the runner (12 more drifted articles re-pinned,
  zero tokens), F4 moved/deleted parked out of selection, revert CAS, no-op
  save short-circuit. (3) Tests 115 → 436 Worker + 76 Python + 82-case parity
  harness + 7-scenario eval gate. (4) P2 complete: /stats, pseudonymized
  research export (nightly artifact), pipeline_runs + decisions.jsonl,
  411k-decl Mathlib index with editor autocomplete + existence tick, patrol
  kind-filter + mark-patrolled. Migration 0006 applied; deployed 4b70fb99.
  Remaining in P2: editor save-UX niceties (selects, in-place body swap),
  trust badges, ip_address storage decision. P3 items unchanged (triggers).

- 2026-06-10 — Roadmap created from architecture audit. P0 started.
- 2026-06-10 — **P0 deployed** (Worker version 99a27390). 4 parallel coding agents on
  disjoint files (backend/auth, render/XSS, editor/frontend, ops/docs) → adversarial
  integration review (found + fixed the MAX_FIELD_LEN=200 blocker) → deploy → live
  smoke test (XSS sinks escaped, 403 cross-origin, CC BY-SA footer, 401 anon). Jack
  promoted to admin. Pre-deploy D1 backup taken.
- 2026-06-10 — **P1 Waves A+B shipped.** Wave A: rescue pull (7 articles' human
  edits now on disk + git), 18-test integration harness, migration 0004 applied to
  prod (949 seed/15 edit backfill verified), WP_HTML TTL + double-read fix, budget
  memo ($1.34/article; quarterly review of 709 solo-feasible). Wave B (deployed
  613da078, 64/64 tests): bearer pipeline path + provenance stamping +
  human-preservation 422 + /api/work + tombstones + drift cron + staleness banner.
  Pipeline user seeded; PIPELINE_TOKEN secret set (value in wiki/.dev.vars).
  Smoke-tested live: :slug.json shape, /api/work 403/jobs, bot-save 400 contract.
  Note: numeric-slug articles (0, 1, 100…) checked — genuine number articles, not
  junk. Next: Wave C (ID backfill, moderate.py, wp-update stage-0, discovery).
- 2026-06-11 — **P1 Wave C shipped — THE LOOP IS CLOSED.** Stable IDs applied to
  production (31,394 annotations, 706 articles, idempotent-verified); Worker
  lazy-heal deployed (d547d917, 74/74 tests); moderate.py runner live (dry-run
  verified: ids_echoed 35/35, fresh 0); drift cron tick 1 found 145/400 drifted
  (36% — far above assumptions); FIRST PRODUCTION STAGE-0 RUN re-pinned 8/10
  drifted articles for zero AI tokens, 2 recorded for stage-1. First real AI
  review pass (2 articles) launched. Remaining P1: article-create endpoint for
  `new` mode (POST 404s on unknown slugs — C2 friction), dynamic homepage/sitemap,
  GET-path revid write removal, WP_HTML delete-on-re-pin. Then P2 instrumentation.
- 2026-06-11 — **FIRST REAL AI REVIEW RUN VERIFIED (run 9abcf468).** 2 articles
  (the queue correctly surfaced stage-0's two needs-work articles first), 0 errors,
  ~$2.96 equiv / 707s. D1 round-trip confirmed: revisions kind='pipeline' with
  parseable comments + full meta (ladder, tokens, id discipline — Addition echoed
  69/69 ids through both agent passes; article '0' got 4 fresh ids for coverage
  extensions, 35→39 annotations, anchors now 39/39); moderation_state stamped so
  both sort to the back of the review queue. The three-script lifecycle from the
  project goal is now: generate=moderate.py new (needs create endpoint), review=
  WORKING, update=WORKING (stage-0).
- 2026-06-10 — **VERSION-CONTROL RISK SURFACED:** `wiki/`, `site/`, `docs/`,
  `CONTRIBUTING.md` are untracked in git — the whole live backend has never been
  committed. P0 (and everything before it) exists only in the working tree + the live
  Worker + D1 backups. Recommend an initial commit of the backend on the `p0-hardening`
  branch before P1. (Not done autonomously — it's a large one-time decision for Jack:
  what to track, `.gitignore` for `.dev.vars`/`.wrangler/`/`backups/`, etc.)
