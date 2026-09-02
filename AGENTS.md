# AGENTS.md — WikiLean

Auto-loaded every session. **Durable conventions + invariants only.** Live state comes
from `python3 manage/status.py` (a SessionStart hook runs it); evolving facts live in the
memory system; `docs/ROADMAP.md` is the canonical plan; `HANDOFF.md` is deep human
onboarding. When this file contradicts the code, trust the code and fix this file.

## What WikiLean is

An annotated-Wikipedia math mirror where every statement carries its **Mathlib**
formalization status — an experiment in **human + AI database moderation** (collecting
interaction data is a first-class goal; clean UI matters). Three components share the repo:

1. **The wiki** (`wiki/`) — the live site: a Cloudflare Worker (Hono + Drizzle/D1 +
   better-auth). LIVE at https://wikilean.jackmccarthy.org.
2. **The `@[wikidata]` tagging bot** (`bot/`) — automated PRs into
   `leanprover-community/mathlib4` (from fork `Deicyde/mathlib4`), gated by human review;
   runs unattended on GitHub Actions.
3. **Wikifunctions** (`wikifunctions/`) — experimental spec/verification sub-project.

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
| The Brain (map of mathematics) | `brain/` pipeline → `site/build_brain_page.py` + `brain/build_shards.py` → **`/brain`** (bubbles/web/ego/explorer; contract `brain/SCHEMA.md`; design `docs/BRAIN-V2.md`). v2 adds `ext` nodes (10 external DBs via `brain/ingest/*.py` → `catalog/data/external/`), `links` edges, `unit` cards, `f` facet bits. The old graph stack is **deleted** (retired 2026-07-10, tombstones destroyed 2026-08-04): `/map`, `/map-v2`, `/graph`, `/atlas`, `/article-graph`, `/graph_data.json`, `/atlas_data.json`, `/api/atlas` and `GET /api/brain/node` answer plain 404s — `RESERVED` (index.ts) only squats the names; agents use `/api/brain/*` + `POST /mcp` (docs: GET `/mcp`). `catalog/data/source_registry.json` = provenance single-source-of-truth. |
| Wikibrain agent API + MCP | `wiki/src/brain-api.ts` (`/api/brain/{unit,transfer,neighborhood,snippets,filter}`) + `wiki/src/mcp.ts` (stateless streamable-HTTP MCP at `POST /mcp`, 9 tools); reference `docs/BRAIN-API.md` + live `/brain/api`; benchmark harness `bench/` (no_tools vs wikibrain arms) |
| Nightly ops | `site/ops/` (launchd; brain 02:20, newtags 03:10, moderate 03:20); tunables in `site/ops/nightly.env`. Brain nightly = `brain-nightly.sh`: cadenced ingest → `fold_proposals` → unified `build_snapshot` (JSONL + generated local SQLite index) → `test_acceptance` (red aborts publish) → cells/shards → frozen-release verification + shadow staging/Worker checks only; nonzero `WIKILEAN_BRAIN_DEPLOY` is rejected. Exact production promotion is a separate approved operator action via `site/ops/brain-promote-release.sh`, an attested immutable public baseline, and a durable external journal. The Worker still serves static cell shards and D1 remains the community overlay; agent team `brain/sync_agents.py` (`WIKILEAN_BRAIN_AGENTS=1`) |
| Plans/docs | `docs/` — `ROADMAP.md` canonical |
| Mathlib checkout | `/Users/jack/Desktop/LEAN/mathlib4` — **read-only; the bot's; don't edit** |

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

# Site (wiki/)
cd wiki && npm run deploy          # deploy the Worker (bundles ALL of wiki/src)
cd wiki && node --experimental-strip-types scripts/build-public.ts \
  --brain-release-manifest ../site/out/brain-releases/<release-hex>/release.json \
  --brain-release-dir ../site/out/brain-releases/<release-hex>       # verified assets; RUN FROM wiki/
# Tagging bot
gh workflow run wikidata-poll.yml --repo Deicyde/WikiLean
python3 bot/poll.py --mathlib /tmp/unused --decide     # act|wait (cheap, gh-only)
# Management
python3 manage/status.py [--live]  # ground-truth snapshot (the SessionStart hook runs this)
python3 manage/refresh.py [--pull] # rebuild the control plane (centrality/coverage/worklists)
```

## Hard invariants — do not break

**Site / D1**
- **`articles.revid` advances ONLY atomically with a re-anchored annotations payload** — the
  product's "stale-but-consistent" guarantee. `latest_revid` / `last_upstream_check` may be
  written freely and must **never** bump `version`.
- **D1 is canonical; never re-seed from disk.** Human edits live only in D1. Any new D1 write
  path outside the Worker must bump `version`, or readers see stale pages for up to 30 days.
- **`findLostHuman` 422 is the floor** — a bot save that drops/alters any `provenance:"human"`
  annotation (tombstones included) must 422. Bots can't approve/endorse (session-only; 403).
- **Render-cache keys are manually versioned + load-bearing** (currently `render:v17:`,
  `page:home:v10`, `page:articles:v2`, `page:articles-index:v1`, `page:about:v1`,
  `page:sitemap:v4`, `page:stats:v4`, `page:wikifunctions:v3`,
  `page:wikifunctions-verify:v3` — all in `index.ts`) — bump the
  prefix whenever output bytes change, or readers get stale HTML for up to 30 days. Asset
  changes need `?v=` bumps.
- **The `RESERVED` set (`index.ts`) must list every non-article top-level path** or the
  `/:slug` catch-all swallows it. Provenance is matched by exact string
  (`human`/`ai-moderated`/`ai`) — don't rename.
- Schema changes go through ordered `wrangler d1 migrations`.

**Bot pipeline (deterministic — no LLM on the git path)**
- The ONLY LLM in the whole pipeline is `triage.py`. Gate / tag application / split / comments /
  table are plain fetch/parse/git/gh. Keep it that way.
- No local `lake build` on the live path. Fresh-clone runner: `git checkout -B <branch>
  FETCH_HEAD` (never `reset --hard`); **never `git add -A`** (stage explicit paths + leak guard).

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
- Never take custody of others' API keys. Never edit the mathlib4 checkout — do Lean work in
  `wikifunctions/lean/`.

## CI test boundaries
- `npm test` / `npm run test:unit` excludes corpus-dependent Vitest files. `npm run test:ci` is
  the named Worker gate; `./scripts/ci-python.sh` is the Python 3.12 gate and unsets credential
  variables before running all five offline commands with required SDK coverage.
- `npm run test:corpus` preflights `site/cache/*.html`, `site/out/*.html`,
  `site/annotations/*.json`, and `wiki/public/assets/decl-index/manifest.json`, then runs the
  render-golden, decl-index, and seed suites. `brain/test_fold_xref.py` is also corpus-only because
  it needs the private declaration cache at `.claude/skills/mathlib-search/.cache/declaration-data.json`
  (with `BRAIN_MATHLIB_CHECKOUT` as its checkout override).
- Playwright uses Chromium. Local install is `npx playwright install chromium`; Linux CI uses
  `npx playwright install --with-deps chromium`.

## Deploy notes
- `npm run deploy` bundles **all** of `wiki/src` — don't leave unreleased Worker WIP committed
  before an operator-approved deployment. Nightly jobs never deploy: the 02:20 Brain job is
  shadow-only and rejects nonzero `WIKILEAN_BRAIN_DEPLOY`; exact promotion uses
  `site/ops/brain-promote-release.sh` and the reviewed release runbook.
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
