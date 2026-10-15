# WikiLean research plan (RQ1–RQ8)

> Compact analysis reference for the human+AI database-moderation experiment
> (the project's first-class research goal — see docs/ROADMAP.md "End goal").
> Written 2026-06-12 (P2c). For each question: what it measures, the event
> types / tables it needs, whether the instrumentation EXISTS TODAY, and the
> /stats row that should be nonzero once data flows (a zero count after the
> feature ships = broken instrumentation, per the roadmap's /stats rule).

## Instrumentation inventory (refreshed 2026-10-13)

The 2026-06-12 inventory listed `pipeline_runs` as pending and `/stats` as not built. Both
have shipped. Every source below is live; counts are from
[`/stats`](https://wikilean.jackmccarthy.org/stats) on 2026-10-13.

| Source | Status | Notes |
|---|---|---|
| `annotation_events` (D1) | **LIVE** (migration 0005, 2026-06-12) | Field-level diffs by annotation id on every write path; `actor_type` `human\|pipeline`. 13,063 events all-time, 184 in the last 30 days. Left-truncated: annotations created before 0005 have no `add` event. |
| `revisions.kind` + `meta` (D1) | **LIVE** (migration 0004) | Kinds seen: `seed` 949, `pipeline` 3,309, `edit` 32, `proposal-approved` 6, `proposal-decided-ai` 6. Pipeline `meta` carries run_id, model, tokens, cost, ladder, and id stats. |
| `flags` (D1) | **LIVE** (migration 0005) | 2 open, 0 resolved. |
| `moderation_state` (D1) | **LIVE** (migration 0004) | `last_reviewed_at` is stamped by every bot save, including zero-token stage-0 re-pins, so `/stats` "Review fresh" overstates AI review coverage. |
| Stable annotation ids | **LIVE** (backfilled 2026-06-11) | Lazy-heal plus runner echo-validation keep identity continuity. |
| `pipeline_runs` (D1) + `POST /api/runs` | **LIVE** (migration 0006) | 227 runs: review 118, new 7, wp-update 102. |
| Revision patrol marks | **LIVE** (migration 0006) | 0 of the 32 `edit` revisions patrolled. Those 32 include `system` import revisions, not only human edits. |
| `proposals` lifecycle table (D1) | **LIVE** (migration 0009; propose-then-approve, 2026-08-06) | 12 approved, 3 rejected, 6 decided by AI, 1 pending; mean 19.0 h to decision. |
| `/stats` page | **LIVE** (cache key `page:stats:v5`) | RQ-labelled rows; footer explains what a zero means. |
| Research export | **LIVE** endpoint | `GET /api/research/export.jsonl` (bot bearer or admin), pseudonymized. A nightly artifact step rides the D1 backup workflow and needs the `PIPELINE_TOKEN` repo secret. |
| `site/cache/.decisions.jsonl` sidecar | **LIVE, host-local** | Written by `site/moderate.py` on the nightly host; gitignored, not in this repo. RQ3's ladder data lives here. |
| Per-annotation `confidence` | **NO**, deferred | Gates RQ7. |
| Considered-candidates / tool transcript | **NO**, deferred | Planned as a deterministic post-pass strip. |

## RQ status (2026-10-13)

No RQ1–RQ8 analysis exists yet: no analysis code or report is committed. The Bridge study
(`docs/research/BRIDGE-REPORT.md`) is separate research output, not an RQ analysis.

| RQ | Instrumented | Data so far (`/stats`, 2026-10-13) | Analysis |
|---|---|---|---|
| RQ1 human-correction rate | yes | 7 human `modify` and 1 human `add` events all-time, 0 in the last 30 days; every human revision in the article histories is from one account | none; blocked on contributors |
| RQ2 endorse vs modify | yes | 0 endorsements, 7 human modifications; proxy: proposal acceptance 80% (12 approved / 3 rejected) | none; blocked on contributors |
| RQ3 AI dissent and ladder blocks | yes (revisions `meta.ladder`, decisions sidecar) | not on `/stats`; sidecar is host-local | none |
| RQ4 time to correction | yes | 0 rejections, 2 open flags and 0 resolved; proposals average 19.0 h to decision | none; blocked on contributors |
| RQ5 survival across AI passes | yes (ids, `revisions.kind`, events) | 3,309 pipeline revisions, 13,063 events; the planned survival-rate row is not on `/stats` (its RQ5 rows show review freshness) | none |
| RQ6 inter-generation agreement | deferred | no double-run sample | none |
| RQ7 confidence calibration | deferred | no confidence field | none |
| RQ8 cost per accepted annotation | yes | 27,413,887 tokens, $1,433.15 API-equivalent over 227 runs; [token_budget.md](token_budget.md) (2026-08-04) gives $1.54 per reviewed and $1.32 per new article | none; no cost-per-accepted figure |

## RQ1 — Human-correction rate of AI annotations, by field

How often do humans modify AI-authored annotations, and which fields
(status, mathlib.decl, mathlib.module, match_kind, anchor, note, kind, label)
get corrected most?

- **Needs:** `annotation_events` `modify`/`delete` with `actor_type='human'`,
  joined to the annotation's authorship (its `add` event `actor_type`, or
  provenance in the revision snapshot for pre-0005 annotations);
  `field_changes` dotted paths give the per-field breakdown.
- **Exists today:** YES (events since 0005). Pre-0005 cohort needs the
  revisions-blob fallback for authorship.
- **/stats row:** "human modifies of AI annotations (by field)" — nonzero
  after the first human edit of an AI annotation.

## RQ2 — Endorse-vs-modify ratio

When a human touches an AI annotation, do they endorse it (explicit
`{action:'endorse'}` POST) or change it? A proxy for AI precision as
perceived by humans.

- **Needs:** `annotation_events` `endorse` vs `modify` counts,
  `actor_type='human'`, on AI-authored annotations.
- **Exists today:** YES — endorse is an explicit event type (0005; the
  stampProvenance rule means bare provenance flips never masquerade as
  endorsements).
- **/stats row:** "endorsements" and "endorse:modify ratio" — endorsements
  nonzero after the first ✓ click.

## RQ3 — AI dissent on human annotations, ladder blocks, and vindication

How often does the AI try to alter human annotations (blocked by the
ladder), how often does it flag dissent (`moderation_flag`), and are those
flags later *vindicated* (the human annotation is subsequently modified,
tombstoned, or reverted in the direction the flag suggested)?

- **Needs:** `revisions.meta.ladder` (restored / reinserted /
  downgrades_blocked / moderation_flags as [annotation_id, flag] pairs) +
  decisions sidecar `ladder`; vindication = join flagged annotation ids to
  LATER `annotation_events` (`modify`/`reject` by a human, `revert_restore`).
- **Exists today:** meta.ladder YES (Wave C); decisions sidecar YES after
  this wave; the vindication join is possible because flags carry annotation
  ids (F14). Reads zero until AI passes run over human-edited articles —
  expected, not a bug (roadmap P2 note).
- **/stats rows:** "ladder restores+reinserts", "moderation_flags harvested",
  "flags vindicated by later human action".

## RQ4 — Time-to-correction survival

Survival analysis: time from an annotation's `add` to its first human
`modify`/`reject`, stratified by `status` and `mathlib.match_kind` at add
time (do `partial`/`generalization` claims rot faster than `exact`?).

- **Needs:** `annotation_events` `add` timestamp + first human event per
  annotation_id; covariates from the add event's `field_changes` (or the
  revision snapshot). Right-censoring: annotations never touched.
- **Exists today:** YES for the post-0005 cohort; the 31,394 pre-0005
  annotations are left-truncated (no add event — use revision history or
  restrict the cohort).
- **/stats row:** "annotations with ≥1 human event" + "median days to first
  human touch" — first value nonzero after the first post-0005 human edit.

## RQ5 — Annotation survival across AI passes, by author type

Do human-authored annotations survive successive AI moderation passes at a
higher rate than AI-authored ones (they must, by construction — the 422
guarantee), and what is the churn rate of AI annotations between passes
(id present in pass N but not N+1)?

- **Needs:** stable annotation ids + per-pass snapshots: decisions sidecar
  lines (ids stats per pass) + `revisions` where `kind='pipeline'` +
  `annotation_events` `delete` with `actor_type='pipeline'`.
- **Exists today:** ids YES; revisions.kind YES; decisions sidecar YES after
  this wave; events YES.
- **/stats row:** "AI-annotation survival rate across passes" (human rate is
  pinned at 100% by the 422 check — report it anyway as an invariant check;
  anything below 100% is a bug alarm, the experiment's canary).

## RQ6 — Inter-generation AI agreement  `[DEFERRED — needs the double-run sample]`

Run two independent AI passes over the same article at the same pinned
revid and measure agreement (same statements found, same decl/status/
match_kind). Upper-bounds the reliability of any single pass.

- **Needs:** a deliberate double-run experiment (same work list, two
  run_ids, no intervening writes) — compare decisions/revisions pairs by
  anchor sig + id. **Token-gated:** each pass is ~$1.34-equiv/article
  (docs/token_budget.md); the sample is a budgeted decision, not ambient
  telemetry. No new instrumentation needed once the sidecar exists; the
  sample doesn't.
- **Exists today:** NO sample. Do not build analysis code before the sample
  is funded.
- **/stats row:** none until the experiment runs.

## RQ7 — Confidence calibration  `[DEFERRED — needs the confidence field]`

Are agent-reported confidences calibrated against later human corrections
(RQ1 outcomes as ground truth)?

- **Needs:** per-annotation `confidence` (uncalibrated covariate) in agent
  output → decisions sidecar. **Not collected:** no new agent output fields
  this wave — the prompt change breaks prompt_sha comparability mid-wave and
  the wire schema is deliberately frozen. When added, it lives in the
  decisions sidecar (and a stripped copy may ride revisions.meta), never in
  the stored annotation blob.
- **Exists today:** NO.
- **/stats row:** none until the field ships.

## RQ8 — Cost per accepted annotation

Tokens / USD-equivalent per annotation that *survives* (no human modify/
reject within a window, or survives the next review pass). The economic
core of the token-donation pitch.

- **Needs:** per-run aggregates from `pipeline_runs` (tokens, cost,
  articles_processed) + per-article tokens/cost + `ids_fresh` from the
  decisions sidecar / revisions.meta + acceptance signal from
  `annotation_events`.
- **Exists today:** per-article cost YES (revisions.meta since Wave C);
  decisions sidecar YES after this wave; `pipeline_runs` PENDING (the runner
  already POSTs /api/runs and tolerates 404, so rows accrue from the moment
  the endpoint deploys — no runner change needed).
- **/stats row:** "cost per accepted annotation (USD-equiv)" and "tokens per
  surviving annotation" — nonzero one review pass after /api/runs deploys.

## Privacy posture

Same model as Wikipedia, stated in CONTRIBUTING.md ("Data & research
notice"): edits are public and attributed (display name, article, timestamp,
comment), and contributors are told up front that edit *metadata* —
field-level changes, provenance, timing — is analyzed and published **in
aggregate**. Research exports use salted-hash pseudonyms by default and
never include emails or IP addresses (anonymous flags store no IP-derived
value; `flags.ip_hash` is always `NULL`. The roadmap also tracks dropping
better-auth's session `ip_address` storage). An **IRB exemption is filed
before any paper** — that's a roadmap line item (P2 "Privacy"), not an
afterthought; nothing in RQ1–RQ8 requires identifiable data.
