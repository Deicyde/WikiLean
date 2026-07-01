# WikiLean — Agent Handoff

> A handoff for a Claude agent picking up the WikiLean project. Read this top-to-bottom
> once, then use it as a map. Written 2026-07-01. When in doubt, verify against the current
> code (this doc will drift). Deep, up-to-date operational notes also live in the memory
> system — see [§9](#9-pointers).

---

## 0. TL;DR — what WikiLean is

Three things share one repo:

1. **The annotated-Wikipedia math wiki** (the flagship product). A live, publicly-editable
   mirror of ~709 Wikipedia math articles where every statement is annotated with its
   **Mathlib** formalization status (`formalized` / `partial` / `not_formalized`). It's an
   **experiment in human + AI database moderation** — an AI pipeline generates/reviews
   annotations, humans correct them on the site, and the interaction is instrumented for
   research. **Clean UI + collecting interaction data are first-class goals.**
2. **The `@[wikidata]` Mathlib-tagging pipeline** (`bot/`). A fully-automated bot that
   batches Wikipedia↔Mathlib cross-reference tags and opens them as PRs into
   `leanprover-community/mathlib4`, gated by human review. Runs unattended on GitHub Actions.
3. **Wikifunctions specs/verification** (`wikifunctions/`) — an experimental research
   sub-project that machine-checks formal specs for Abstract Wikipedia's function library.

**Mission (verbatim from `docs/ROADMAP.md`):** *"A complete interface mapping Wikipedia
statements to their formal implementation in Mathlib (and eventually CSLib, PhysLib, etc.),
primarily AI-moderated via three routine operations: (1) generate annotations for new
articles, (2) review previous articles and correct mistakes, (3) update articles as
Wikipedia's content changes."*

---

## 1. Where the project lives

| What | Where |
|---|---|
| **Repo root** | `/Users/jack/Desktop/LEAN/WikiLean` (git; branch `main`; remote **`Deicyde/WikiLean`**) |
| **Live site** | **https://wikilean.jackmccarthy.org** (Cloudflare Worker, custom domain) |
| **The Worker app** | `wiki/` (Hono + Drizzle/D1 + better-auth; the whole live site) |
| **The tagging bot** | `bot/` (15 Python scripts) + `.github/workflows/wikidata-poll.yml` |
| **Tag catalog / data** | `catalog/data/*.jsonl`, `bot/data/`, `bot/state/` |
| **Original static mirror** | `site/` (annotations JSON = live data; `site/out/*.html` = dead) |
| **Wikifunctions** | `wikifunctions/` (own pinned Lean project in `wikifunctions/lean/`) |
| **Planning docs** | `docs/` (`ROADMAP.md` is canonical — start there) |
| **Mathlib checkout** | `/Users/jack/Desktop/LEAN/mathlib4` — **read-only; the bot's domain, don't edit it.** Do Lean work in `wikifunctions/lean/`. |

The bot opens PRs from the fork **`Deicyde/mathlib4`** → **`leanprover-community/mathlib4`**.

---

## 2. Current status of the website

**Live and healthy** (HTTP 200). ~709 articles, ~29,500 annotations (≈28% formalized / 14%
partial / 58% not formalized). Deployed as a single Cloudflare Worker.

**Stack:** Hono router + Drizzle ORM over **D1** (`wikilean` DB), **better-auth** login,
static assets from `wiki/public/`. Entry: `wiki/src/index.ts` (`export default { fetch, scheduled }`).

**Storage:**
- **D1** (`DB`): `articles` (current state, pinned `revid`, `annotations` JSON, `version` CAS
  counter), `revisions` (append-only snapshots), `moderation_state` (the work queue),
  `annotation_events` (the experiment's instrument), `flags`, `pipeline_runs`, `watchlist`,
  plus better-auth `users`/`sessions`/`accounts`. Migrations in `wiki/migrations/`.
- **KV** `RENDER_CACHE` — **overloaded**: page/render caches (TTL'd) **plus durable state that
  must NOT be evicted** — the `/queue` blob (`wikidata:queue`, no TTL) and review-posting
  tokens (`reviewtok:*`). Don't "clear the cache" wholesale.
- **KV** `WP_HTML` — MediaWiki-parsed article HTML (`wp:<slug>:<revid>`, 90d TTL).
- **R2**: none (not enabled on the account).

**Cron:** `triggers.crons: ["17 6 * * *"]` → `drift.ts scheduled` (daily Wikipedia-drift sweep;
free-plan budget ≈ 8 fetches/run, so a full 709-article sweep takes ~2 daily runs).

**Deploy:** `cd wiki && npm run deploy` (= `wrangler deploy`; no separate bundler step).
Before deploying static-page/CSS changes, run the asset build:
`node --experimental-strip-types scripts/build-public.ts` (copies `site/assets/*` + `wiki/assets/editor.js`
into `wiki/public/assets/`). **Never edit `wiki/public/assets/` directly — edit the sources.**
`index.html`/`sitemap.xml` are deliberately NOT copied (served dynamically from D1). Logs: `npm run tail`.

---

## 3. Features that exist + how to use them

### 3A. The wiki (public site)

**Pages** (all in `wiki/src/`, mostly `index.ts` + `pages.ts`):
- `GET /` — homepage (D1 article list + coverage counts).
- `GET /:slug` — an annotated article. Anonymous base render is KV-cached; auth/editor/
  staleness-banner/watch-toggle are injected **per-request**.
- `GET /:slug/history`, `GET /:slug/diff/:fromId/:toId` — revision history + field-level diffs.
- `GET /recent-changes` — global patrol feed (`?kind=…`, `?watching=1`).
- `GET /flags` — open problem-report queue (login to view; resolve = patroller/admin).
- `GET /stats` — live experiment dashboard.
- `GET /u/:id` — public user profile.
- `GET /wikifunctions`, `GET /wikifunctions/verify` — the Wikifunctions tracker + methodology.
- `GET /queue` — public read of pending `@[wikidata]` tags.
- `GET /review` — **the PR-review tool** (see 3C).
- `GET /login`, `/logout`.
- Static (served from `public/` by the ASSETS binding, not the Worker): `/about`, `/concepts`,
  `/graph`, `/article-graph`, `/wikilean.ttl`, `/404`.

**Editing:** logged-in users edit annotations in-page; writes go `POST /api/article/:slug` with
optimistic concurrency (`base_version` → 409 on conflict). Anyone can file an anonymous problem
report (`POST /api/flag/:slug`, rate-limited). Roles: `user` / `patroller` / `admin` / `bot` / `blocked`.

**Auth — TWO SEPARATE FLOWS, do not conflate:**
- **Wiki login** (`auth.ts`): better-auth (GitHub + Google), scope **`read:user`,`user:email` only —
  never `public_repo`** (identity only, to attribute edits).
- **Pipeline bearer**: `Authorization: Bearer <PIPELINE_TOKEN>` → the `users` row `pipeline`
  (role `bot`). That row is the **kill switch** (delete it / set role `blocked`).

### 3B. The `@[wikidata]` tagging pipeline (`bot/`)

Fully automated. Batches ~25 Wikipedia↔Mathlib tags → opens a PR on mathlib4 → humans review →
bot trims to approved (green) tags → maintainer merges via Bors → next batch opens on merge.

- **Orchestrator:** `bot/poll.py` — event-driven off the current PR's GitHub state (not a timer).
  State cursor: `bot/state/bot_state.json`.
- **Runs on:** GitHub Actions, `Deicyde/WikiLean` repo, `.github/workflows/wikidata-poll.yml`,
  every 30 min (stateless fresh-clone; state committed back to the WikiLean repo each run).
- **Secrets:** `MATHLIB_FORK_PAT` (classic `repo` PAT, pushes the fork + is `GH_TOKEN`),
  `CLAUDE_CODE_OAUTH_TOKEN` (for `triage.py`'s one LLM call), `PIPELINE_TOKEN` (the `/queue` publish).
- **Operate it manually:**
  ```bash
  gh workflow run wikidata-poll.yml --repo Deicyde/WikiLean            # dispatch a tick
  gh workflow run wikidata-poll.yml --repo Deicyde/WikiLean -f force_heavy=true
  gh run watch <id> --repo Deicyde/WikiLean                            # follow it
  python3 bot/poll.py --mathlib /tmp/unused --decide                  # cheap gh-only: act|wait
  python3 bot/poll.py --mathlib /tmp/unused --refresh-table           # refresh the reviewer table
  python3 bot/settle.py <pr>                                          # print gate + green/recycle split
  python3 bot/pool.py -n 25 --json                                   # preview next fresh candidates
  ```
- **The review gate** (`settle.classify`): ≥2 distinct human reviewers **and** ≥1 maintainer
  (`author_association ∈ {OWNER,MEMBER,COLLABORATOR}`, seeded `MAINTAINERS={"jcommelin"}`).
  Per-tag: a maintainer verdict trumps; else any reject/revise/flag → recycle; else ≥1 approve →
  green. 👍/👎 reactions on the crossref bot's per-tag comment count as approve/reject.
- **Current state (2026-07-01):** batch 6 = **PR #41139** open, awaiting review. Batches 1–5 merged.

### 3C. The `/review` PR-review tool (`wiki/src/review.ts`)

A deterministic (no-LLM) UI for reviewing a mathlib `@[wikidata]` PR: paste `owner/repo/#`, it
renders each tagged decl with the Lean source, Wikidata label, and Wikipedia lead, and lets a
reviewer set per-tag verdicts.

- **Two post paths:** **"📋 Copy review"** (always works — pastes a `## WikiLean review` comment
  as the reviewer, no permissions) and **"✅ Submit"** (in-app posting).
- **Submit only works for the repo owner (Jack/@Deicyde)** via `REVIEW_POSTING_PAT` (a classic
  `public_repo` PAT, exempt from org OAuth restrictions, used only when the connected user IS the
  PAT owner). For anyone else (e.g. maintainer @faenuccio), GitHub blocks the post because the
  **WikiLean Review GitHub App isn't installed on `leanprover-community`** — they're steered to
  Copy review. To enable direct Submit for others: an org owner installs
  `https://github.com/apps/wikilean-review/installations/new`. (This is expected, not a bug.)

### 3D. Wikifunctions (`wikifunctions/`) — experimental, separate

Uses WikiLean's Wikidata→Mathlib maps to generate + machine-check formal specs for Wikifunctions
(join key = Wikidata QID via the `wikifunctionswiki` sitelink). 25 curated functions verified;
`wikifunctions/lean/` is a **self-contained lake project pinned to its own mathlib commit** —
deliberately isolated from the bot-managed checkout. Data surfaced on the site via
`wiki/src/wikifunctions-data.ts`. Its status is shown at `/wikifunctions`.

---

## 4. Tag supply (the pipeline's fuel) — how it works + current depth

The bot draws tags from a **catalog** of Wikidata→Mathlib mappings, in demand-rank order:
- **Catalog** = `catalog/data/{pilot,tier2,generated_candidates,refresh}_tagged.jsonl` (loaded by
  `bot/pool.py`'s `CATALOG` list; later files win on QID collision). ~1,400+ mappings total.
- **Ranking/demand** = `bot/data/most_used_qids.json` (1,001 QIDs ranked by how often each concept
  is wikilinked). `pool.candidates()` walks this in order, keeping only high-confidence, in-`Mathlib/`,
  not-already-tagged/recycled/cut QIDs.
- **Generating more mappings** (the tap): a Workflow of **triage → map → verify** over the unmapped
  most-used QIDs. Triage (cheap, classify pure-math-likely vs applied/physics/stats) lifts the
  map+verify yield from ~13% → **58%**. Verified mappings are deduped (by QID **and** decl — many
  decls are already tagged under other QIDs) and appended to `catalog/data/generated_candidates.jsonl`
  (which is wired into `pool.CATALOG`). 41 generated mappings are live so far.

**Current depth (2026-07-01):** ~**133 fresh candidates ≈ 7 batches** of runway + 9 recycled. Since
batches open one-per-merge and merges take weeks, that's **months** — not urgent. **737 of the 1,001
ranked QIDs are still unmapped** (589 not yet triaged), so the tap can refill indefinitely (~35–45
mappings ≈ ~2–3 batches per pass). **Rule of thumb: generate more when the fresh pool drops to ~3–4 batches.**

---

## 5. What to build next (roadmap) + how to guide Jack

The canonical plan is `docs/ROADMAP.md` (phases P0→P3, each P3 item behind a written trigger).
Status: **P0 shipped**, **P1 "the loop is closed"**, **P2 mostly complete**. Live candidates:

**Most likely next asks (in rough priority):**
1. **Propose-then-approve** (`docs/propose-then-approve.md`, designed, not built): let the AI
   *propose* edits to human annotations that Jack one-click approves — keeps the `findLostHuman`
   floor (the agent never mutates a human annotation; it only proposes). **Blocked on Jack's UX
   pick** — ask him which UX before building.
2. **The Wikidata "Mathlib declaration" property proposal** (`docs/wikidata_property_proposal.md`):
   the reverse direction (QID → Mathlib decl on Wikidata). Draft is complete, modeled on Metamath's
   P12888; value = fully-qualified decl name (Option A); one open question = the formatter URL
   (lean toward a WikiLean `/decl/$1` resolver). **HARD BLOCKER: the AI-seeded ~815 mappings MUST be
   human-reviewed by Jack before he submits — he explicitly asked to be reminded.** Don't submit for him.
3. **Keep the tagging pipeline fed + healthy** — run a generation pass when the pool is low (§4),
   watch batch merges (a merged PR auto-opens the next batch; nudge the workflow if the cron lags),
   and eyeball generated mappings before they go live.
4. **P2 leftovers:** the `revisions` kind/meta migration (backfill before the first bearer write),
   editor save-UX niceties, trust-signal badges, stop storing `ip_address` (privacy).
5. **P3 (only when its trigger fires):** compute-donation queue (first donor), multi-library schema
   v4 `formalizations[]` (when CSLib covers a course), mass-revert admin script (first vandalism).

**How Jack works (important — this shapes how you should operate):**
- He is the **lead dev and product owner**; he wants to be **consulted each session** and to
  **review before anything ships upstream** (mathlib PRs, Wikidata submissions, the property proposal).
- He's comfortable delegating implementation to subagents/workflows but wants the **reasoning and
  trade-offs surfaced**, not hidden. Give a recommendation, not a survey.
- **Verify before asserting; be honest about failures.** Don't claim something works without checking.
- **Ultracode is typically on** for this project — use the **Workflow tool** for substantive work
  (parallel mapping, adversarial review of your own changes before committing). This session used it
  for tag generation, change review, and this handoff.
- **Commit everything to git** (his standing instruction — maximum version control). Push when he
  asks; branch from `main`; PRs go to `Deicyde/WikiLean`.
- Credentials: only lead with his earned **Fordham BS**, not the in-progress Stony Brook MA.

---

## 6. Critical gotchas / don't-break (read before touching these)

**Website / D1:**
- **Render-cache keys are manually versioned and load-bearing.** Base article key is
  `render:v14:<slug>:<version>` (`index.ts renderArticleBase`); page keys are `page:home:v3`,
  `page:stats:v2`, etc. **Bump the version prefix whenever the output bytes change** or readers get
  stale HTML for up to 30 days. `editor.js`/CSS/JS changes need `?v=` bumps in `pages.ts`/`engine/page.ts`.
- **`articles.revid` advances ONLY atomically with a re-anchored annotations payload** — the product's
  core guarantee ("stale-but-consistent"). `latest_revid`/`last_upstream_check` (drift bookkeeping) may
  be written freely and must **never** bump `version` (the staleness banner is per-request, not cached).
- **Never re-seed D1 from disk** — human edits live only in D1 and are canonical there. Transform D1
  blobs in place. Any new D1 write path outside the Worker must bump `version`.
- **`findLostHuman` 422 is the floor:** a bot save that drops/alters any `provenance:"human"`
  annotation (tombstones included) must 422. Bots can't approve/endorse (session-only, 403 for bots).
- **`RESERVED` set (`index.ts`) must list every non-article top-level path** or the `/:slug` catch-all
  swallows it. Provenance is matched by **exact string** (`human`/`ai-moderated`/`ai`) — don't rename.
- All schema changes go through **ordered wrangler d1 migrations**.

**The bot pipeline (deterministic — do not add LLM calls to the git path):**
- **The ONLY LLM in the whole pipeline is `triage.py`** (requeue-vs-cut + a proposed retarget, which
  it verifies against live mathlib-search + Wikidata). Everything touching git — gate, tag application,
  split, comments, table — is plain fetch/parse/git/gh. Keep it that way.
- **NO local `lake build` anywhere on the live path.** Open adds the `CrossRefAttribute` import and
  trusts mathlib CI; settle uses `--no-build`; conflict-resolve uses `--reapply`. (A local build
  cold-recompiles edited high-fan-out modules for ~1h45m regardless of cache — the whole reason it was
  dropped this session; open now takes ~9 min.)
- **Fresh-clone stateless runner:** use `git checkout -B <branch> FETCH_HEAD`, **never `reset --hard`**
  (the runner is on master with no local branch). **Never `git add -A`** (a foreign file leaked into
  #40747; stage explicit paths + the leak guard). State survives only because the workflow commits
  `bot/state/` + `bot/data/` back.
- **Merge detection is Bors-aware:** `settle.is_merged` treats `state=closed` + title
  `"[Merged by Bors]"` as merged (GitHub's own `merged` flag misses the Bors path mathlib uses).
- **Cross-fork PR body isn't editable for ~1–2 min after creation** — the reviewer-UI `?pr=` link fill
  is retried + self-healed (`fill_review_link` after finalize, `pr_table.sync_review_link` on later ticks).
  If a new blank-link issue appears, this is why.

**Security / credentials (hard lines):**
- **`.dev.vars` is gitignored and holds secrets** (`PIPELINE_TOKEN`, `REVIEW_GITHUB_*`, `BETTER_AUTH_SECRET`,
  `REVIEW_POSTING_PAT`). Never commit it. Read values via `sed`; never print secret values. Set Worker
  secrets via `wrangler secret put` / `gh secret set` from `.dev.vars`, never paste secrets in chat.
- **Token donations: never take custody of others' keys.** No server-side key vault.
- **Max-auth gotcha:** the runner's `claude -p` / SDK calls fail silently ("error result: success",
  0 tokens) if `ANTHROPIC_API_KEY` shadows the Max subscription — unset it.
- **Don't edit the mathlib4 checkout** (`/Users/jack/Desktop/LEAN/mathlib4`) — the bot owns it.

---

## 7. Quick command reference

```bash
# --- Website (wiki/) ---
cd wiki && npm run deploy                                   # deploy the Worker (live)
node --experimental-strip-types scripts/build-public.ts    # rebuild static assets first
cd wiki && npm run dev                                      # local dev (reads .dev.vars)
cd wiki && npx tsc --noEmit                                 # typecheck
cd wiki && npm test                                         # 334+ Worker tests
cd wiki && npm run tail                                     # live logs

# --- Tagging pipeline (bot/) ---
gh workflow run wikidata-poll.yml --repo Deicyde/WikiLean  # dispatch a poll tick
python3 bot/poll.py --mathlib /tmp/unused --decide         # act|wait (cheap, gh-only)
python3 bot/poll.py --mathlib /tmp/unused --refresh-table  # refresh the reviewer table
python3 bot/settle.py <pr>                                 # gate + green/recycle for a PR
python3 bot/pool.py -n 25 --json                           # preview next fresh candidates

# --- Tests (site/) ---
python3 site/test_moderate.py && python3 site/test_parity.py && python3 site/eval_moderation.py --offline
```

The Python scripts use the venv at `catalog/.venv/bin/python3`. Pushes to git often race the
poller committing state — `git stash; git pull --rebase origin main; git push; git stash pop`.

---

## 8. Component status at a glance

| Component | Status | Notes |
|---|---|---|
| `wiki/` Worker | **LIVE** | the whole site; deploy = `npm run deploy` |
| `bot/` pipeline | **LIVE, automated** | GitHub Actions; batch 6 (#41139) open |
| `catalog/` | **LIVE** (tag supply) | ~1,400 mappings + 41 generated; feeds `pool.py` |
| `site/annotations/*.json` | **LIVE** (data layer) | 1,521 files; Worker renders from them |
| `site/out/*.html` | **DEAD** | stale static build, superseded by the Worker |
| `site/cache/*.json` | **LIVE support** | pinned Wikipedia revisions (not reproducible — tracked) |
| `wikifunctions/` | **EXPERIMENTAL, active** | own pinned Lean project; 25 verified functions |

---

## 9. Pointers

- **`docs/ROADMAP.md`** — the canonical plan (phases, decisions, status log). Read first.
- **`docs/research-plan.md`** — the RQ1–RQ8 research questions for the moderation experiment.
- **`docs/propose-then-approve.md`**, **`docs/wikidata_property_proposal.md`**, **`docs/token_budget.md`**.
- **`bot/README.md`** — the pipeline's own detailed docs.
- **`README.md` / `CONTRIBUTING.md`** — public overview + contribution/dev conventions.
- **Memory system** (persists across agent sessions):
  `/Users/jack/.claude/projects/-Users-jack-Desktop-LEAN-WikiLean/memory/` — `MEMORY.md` is the index;
  individual notes cover the pipeline internals, deploy setup, the review tool, the property proposal,
  and Jack's working preferences. **Read `MEMORY.md` at the start of a session.**

---

*If something here contradicts the code, trust the code and update this file. Jack values accuracy
over confidence — verify, then assert.*
