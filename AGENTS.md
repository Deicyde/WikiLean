# AGENTS.md — WikiLean

Auto-loaded every session. **Durable conventions + invariants only.** Live state comes
from `python3 manage/status.py` (a SessionStart hook runs it) and the live site (`/stats`,
`/assets/brain/current.json`); evolving facts live in `docs/`: the `docs/ROADMAP.md` status
log, the Brain checkpoint named by `docs/BRAIN-SQLITE-HANDOFF.md`, and
`docs/BRAIN-ACTIVATION-2026-10-04.md` for production. Agent memory is per-host and may be
empty, so don't rely on it. `docs/ROADMAP.md` is the canonical plan; `HANDOFF.md` is the
onboarding map. When this file contradicts the code, trust the code and fix this file.

## What WikiLean is

An annotated-Wikipedia math mirror where every statement carries its **Mathlib**
formalization status — an experiment in **human + AI database moderation** (collecting
interaction data is a first-class goal; clean UI matters). Three components share the repo:

1. **The wiki** (`wiki/`) — the live site: a Cloudflare Worker (Hono + Drizzle/D1 +
   better-auth). LIVE at https://wikilean.jackmccarthy.org.
2. **The `@[wikidata]` tagging bot** (`bot/`) — automated PRs into
   `leanprover-community/mathlib4` (from fork `Deicyde/mathlib4`), gated by human review;
   runs unattended on GitHub Actions.
3. **Wikifunctions** (`wikifunctions/`) — the QID → Wikifunctions ZID → Mathlib data
   pipeline behind `/wikifunctions`. The verification project (Lean proofs, spec corpus)
   moved to `Deicyde/wikifunctions` on 2026-07-02.

Mission = three routine AI operations: (1) generate annotations for new articles,
(2) review/correct existing ones, (3) update on Wikipedia drift.

## Where things live

| What | Where |
|---|---|
| Live site (Worker) | `wiki/` — entry `wiki/src/index.ts` (`export default { fetch, scheduled }`) |
| Annotation data | `site/annotations/*.json` — disk is cache/backup; **D1 is canonical** |
| Tagging bot | `bot/` + `.github/workflows/wikidata-poll.yml`; state in `bot/state/` |
| Tag catalog | `catalog/data/*.jsonl` |
| Management control plane | `manage/` — centrality × coverage → worklists (see `manage/README.md`) |
| The Brain (map of mathematics) | `brain/` pipeline → `site/build_brain_page.py` + `brain/build_shards.py` → **`/brain`** (bubbles/web/ego/explorer; contract `brain/SCHEMA.md`; design `docs/BRAIN-V2.md`). v2 adds `ext` nodes (10 external DBs via `brain/ingest/*.py` → `catalog/data/external/`), `links` edges, `unit` cards, `f` facet bits. The old graph stack is **deleted** (retired 2026-07-10, tombstones destroyed 2026-08-04): `/map`, `/map-v2`, `/graph`, `/atlas`, `/article-graph`, `/graph_data.json`, `/atlas_data.json`, `/api/atlas` and `GET /api/brain/node` answer plain 404s — `RESERVED` (index.ts) only squats the names; agents use `/api/brain/*` + `POST /mcp` (docs: `/mcp` in a browser, which other clients get as a JSON 405, and `/brain/api`). `catalog/data/source_registry.json` = provenance single-source-of-truth. |
| Wikibrain agent API + MCP | `wiki/src/brain-api.ts` (release-qualified, rate-limited reads: `GET /api/brain/{cell,unit,transfer,neighborhood,snippets,filter,search,decl,premises,bridge}`; reference page `GET /brain/api`) + `wiki/src/brain-edits.ts` (community overlay writes in D1: `POST /api/brain/edge`, `GET /api/brain/edges`, `POST /api/brain/node`, `DELETE /api/brain/{edge,node}/:id` or `POST …/:id/delete`, `POST /api/brain/quickstatements`) + `wiki/src/mcp.ts` (stateless streamable-HTTP MCP at `POST /mcp`, 9 tools); reference `docs/BRAIN-API.md` + live `/brain/api`; benchmark harness `bench/` (no_tools vs wikibrain arms) |
| Nightly ops | `site/ops/` (launchd; brain 02:20, moderate 03:20). newtags (03:10) has a plist but is not a running job: `/stats` still shows 7 `new` runs in total, the last `ai-create` revision is from 2026-07-03, and switching it on is Jack's decision. **Nightly host:** jobs run only on the one operator Mac where the LaunchAgents are installed and loaded, and the repo does not record which Mac that is. Check `launchctl print gui/$(id -u)/org.wikilean.moderate` before assuming a checkout runs anything; a checkout without them runs nothing. Tracked tunables in `nightly.env`, host-local paths in gitignored `nightly.local.env`, and generated plists via `nightly-launchd.py`. Community graduation is opt-in from an explicit sealed D1 bundle; nightly never acquires it implicitly. Brain nightly = `brain-nightly.sh`: cadenced ingest → canonical proposal-QID plan → sealed Wikidata entity acquisition only when nonempty → offline-only `fold_proposals` with the explicit verified bundle → unified `build_snapshot` (JSONL + generated local SQLite index) → `test_acceptance` (red aborts publish) → cells/shards → frozen-release verification + shadow staging/Worker checks only. Planning/acquisition/fold failure aborts the build; the Brain lock is never stolen by age and requires verified manual recovery. Nonzero `WIKILEAN_BRAIN_DEPLOY` is rejected. Exact production promotion is a separate approved operator action via `site/ops/brain-promote-release.sh`, an attested immutable public baseline, and a durable external journal. The Worker still serves static cell shards and D1 remains the community overlay; agent team `brain/sync_agents.py` (`WIKILEAN_BRAIN_AGENTS=1`) |
| Plans/docs | `docs/` — `ROADMAP.md` canonical |
| Mathlib checkout | External, read-only, and host-configured through `WIKILEAN_MATHLIB` / `BRAIN_MATHLIB_CHECKOUT`; the bot's checkout must never be edited |

Repo remote: `origin` = `Deicyde/WikiLean`. Branch from `main`; PRs → `Deicyde/WikiLean`.

## Commands

Use Node 22 and Python 3.12. The JavaScript package root is `wiki/`; use `npm ci`
there to reproduce the lockfile exactly.

```bash
# Required, hermetic CI checks
cd wiki && npm ci
cd wiki && npm run test:ci         # typecheck + hermetic Worker tests
./scripts/ci-python.sh             # deterministic Python checks (run at repo root)

# Browser soak (Chromium; local loopback only, no external requests)
cd wiki && npx playwright install chromium
cd wiki && npm run test:e2e

# Corpus-dependent local checks (not required fresh-checkout CI)
cd wiki && npm run test:corpus     # renderer/annotation/decl-index corpora
python3 brain/test_fold_xref.py    # real catalog + private decl cache/Mathlib checkout

# Site (wiki/). Production deploys ONLY via the exact promoter; see docs/BRAIN-RELEASE-RUNBOOK.md.
# Never plain `npm run deploy` / `wrangler deploy`: it uploads the local wiki/public and
# replaces the Brain selector and the whole asset tree.
bash site/ops/brain-promote-release.sh sha256:<release-hex> --release-root <abs> ...  # see runbook
cd wiki && node --experimental-strip-types scripts/build-public.ts \
  --brain-release-manifest ../site/out/brain-releases/<manifest-hex>/release.json \
  --brain-release-dir ../site/out/brain-releases/<manifest-hex>       # verified assets; RUN FROM wiki/
# Tagging bot
gh workflow run wikidata-poll.yml --repo Deicyde/WikiLean
python3 bot/poll.py --mathlib /tmp/unused --decide     # act|wait (cheap, gh-only)
# Management
python3 manage/status.py [--live]  # ground-truth snapshot (the SessionStart hook runs this)
python3 manage/refresh.py [--snapshot-bundle /absolute/path] # rebuild control plane
# Operator-only live read: the launcher enforces isolated CPython 3.12 (-I -S).
brain/acquire-d1-snapshot.sh [--store /absolute/private/snapshot/store]
# Proposal Wikidata acquisition: emit the exact plan first; skip acquisition for qids:[].
python3 brain/fold_proposals.py \
  --write-wikidata-request-plan /absolute/private/request-plan.json
brain/acquire-wikidata-entities.sh /absolute/private/request-plan.json \
  --store /absolute/private/entity-bundle/store
# Then rerun fold_proposals.py with --wikidata-entity-bundle <printed-absolute-path>.
```

## Hard invariants — do not break

**Site / D1**
- **`articles.revid` advances ONLY atomically with a re-anchored annotations payload** — the
  product's "stale-but-consistent" guarantee. `latest_revid` / `last_upstream_check` may be
  written freely and must **never** bump `version`.
- **D1 is canonical; never re-seed from disk.** Human edits live only in D1. Any new D1 write
  path outside the Worker must bump `version`, or readers see stale pages for up to 30 days.
- **`findLostHuman` 422 is the floor** — a bot save that drops/alters any `provenance:"human"`
  annotation (tombstones included) must 422. It is an anti-clobber guard, not a policy gate:
  under Human-at-boundaries (ROADMAP, ratified 2026-08-06) the PIPELINE_TOKEN bearer MAY
  decide proposals (approve/reject), and an AI approve that changes human bytes re-labels
  them `ai-moderated` — **AI-changed bytes are never left marked `human`, and bots never
  mint `human`** (endorse stays session-only; 403). Humans gate only cross-site pushes.
- **Render-cache keys are manually versioned + load-bearing** (currently `render:v17:`,
  `page:home:v10`, `page:articles:v2`, `page:articles-index:v1`, `page:about:v1`,
  `page:sitemap:v4`, `page:stats:v5`, `page:wikifunctions:v3`,
  `page:wikifunctions-verify:v3` — all in `index.ts`) — bump the
  prefix whenever output bytes change, or readers get stale HTML for up to 30 days. Asset
  changes need `?v=` bumps.
- **The `RESERVED` set (`index.ts`) must list every non-article top-level path** or the
  `/:slug` catch-all swallows it. Provenance is matched by exact string
  (`human`/`ai-moderated`/`ai`) — don't rename.
- Schema changes go through ordered, numbered files in `wiki/migrations/`. **Remote migration
  tracking is out of sync**: the headers of `0009`–`0012` say so and give the manual command.
  Apply a new migration the same way, as an operator action after a D1 backup (`npm run backup:d1`):
  `cd wiki && npx wrangler d1 execute wikilean --remote --file=migrations/<NNNN_name>.sql`.
  Never `wrangler d1 migrations apply --remote`, which could re-run files the remote tracking
  table does not list (`0008`'s `ALTER TABLE … ADD COLUMN` fails on a re-run). Keep new files
  idempotent (`CREATE … IF NOT EXISTS`, as `0009`–`0012` are). Reconciling the remote
  `d1_migrations` table is an open operator task.

**Bot pipeline (deterministic — no LLM on the git path)**
- The ONLY LLM in the whole pipeline is `triage.py`. Gate / tag application / split / comments /
  table are plain fetch/parse/git/gh. Keep it that way.
- No local `lake build` on the live path. Fresh-clone runner: `git checkout -B <branch>
  FETCH_HEAD` (never `reset --hard`); **never `git add -A`** (stage explicit paths + leak guard).

**Brain acquisition / replay**
- `fold_proposals.py` is offline-only: emit its canonical Wikidata QID plan first, skip
  acquisition for an empty plan, otherwise pass the exact absolute bundle printed by
  `acquire-wikidata-entities.sh`. Never restore an implicit network fallback.
- Wikidata acquisition receipt/lineage logical IDs are clock-free. The bundle directory ID
  separately binds the exact canonical audit-bearing receipt and lineage bytes; do not
  collapse a later unchanged acquisition onto an older evidence generation. A bundle is not
  v3 source authority until its evidence is explicitly bound into a reviewed source plan.

**Security / credentials**
- `.github/workflows/ci.yml` is validation only: read-only repository permissions, checkout
  credentials disabled, no secrets, no deployments, no production writes, and no token-consuming
  agent calls. Its `required` job aggregates only the `worker` and `python` hermetic gates.
- The separate Playwright `browser` job is a non-required soak on every CI trigger. Failures upload
  `wiki/playwright-report/` and `wiki/test-results/` as `browser-failure-artifacts` for 7 days.
  Promote it into `required` only through a reviewed workflow change after stable run history; there
  is no automatic threshold.
- `.dev.vars` is gitignored and holds secrets — never commit, never print values; set Worker
  secrets via `wrangler secret put` / `gh secret set`.
- **Max-auth gotcha**: unset `ANTHROPIC_API_KEY` before `claude`/SDK calls or they fail silently
  ("error result: success", 0 tokens).
- Never take custody of others' API keys. Never edit the mathlib4 checkout; do Lean work in a
  separate project (the Wikifunctions Lean proofs live in `Deicyde/wikifunctions`).

## CI test boundaries
- `npm test` / `npm run test:unit` excludes corpus-dependent Vitest files. `npm run test:ci` is
  the named Worker gate; `./scripts/ci-python.sh` is the Python 3.12 gate and unsets credential
  variables before running the full offline suite (the summary reports the check count).
- `npm run test:corpus` preflights `site/cache/*.html`, `site/out/*.html`,
  `site/annotations/*.json`, and `wiki/public/assets/decl-index/manifest.json`, then runs the
  render-golden, decl-index, and seed suites. `brain/test_fold_xref.py` is also corpus-only because
  it needs the private declaration cache at `.claude/skills/mathlib-search/.cache/declaration-data.json`
  (with `BRAIN_MATHLIB_CHECKOUT` as its checkout override).
- Playwright uses Chromium. Local install is `npx playwright install chromium`; Linux CI uses
  `npx playwright install --with-deps chromium`.

## Deploy notes
- **Production deploys go only through `site/ops/brain-promote-release.sh`** and the reviewed
  release runbook. A plain `npm run deploy` (`wrangler deploy` with `assets.directory`
  `./public`) uploads whatever the local `wiki/public` holds: it replaces the live Brain
  selector and the whole asset tree, and it skips the journal and canary. Since 2026-10-04
  production serves an immutable release (`docs/BRAIN-ACTIVATION-2026-10-04.md`).
- Worker source, `wiki/scripts/`, `wiki/package*.json`, lockfiles, `site/*.py` and site assets
  are release-affecting (`site/ops/brain_release_authority.py`). A change there ships only in
  a new release built at the new commit, with a fresh P1B and Jack's approval; no Worker-only
  hotfix path exists. Nightly jobs never deploy: the 02:20 Brain job is shadow-only and
  rejects nonzero `WIKILEAN_BRAIN_DEPLOY`.
- Edit asset **sources** (`site/assets/*`, `wiki/assets/editor.js`), freeze/verify a Brain release,
  then run build-public from `wiki/` with its explicit manifest and directory; never edit
  `wiki/public/` directly (it's generated + gitignored). `brain.html` is copied from the frozen
  release, not mutable `site/out`.
- `index.html` / `sitemap.xml` are served dynamically from D1 — deliberately NOT copied to public.

## How Jack works
- Lead dev + product owner. **Consult each session; surface trade-offs (a recommendation, not a
  survey); review before anything ships upstream** (mathlib PRs, Wikidata submissions).
- **Commit everything to git** (standing instruction — maximum version control); push only when
  asked. Stage explicit paths (an unrelated `site/annotations/*` diff is often in the tree).
- Verify before asserting; be honest about failures. Ultracode is typically on — use Workflows
  for substantive work and adversarially review your own changes before committing.
- Credentials: lead with the earned **Fordham BS**, not the in-progress Stony Brook MA.
