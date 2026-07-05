# CLAUDE.md — WikiLean

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
| The Brain (map of mathematics) | `brain/` pipeline → `site/build_brain_page.py` + `brain/build_shards.py` → **`/brain`** (bubbles/web/ego; contract `brain/SCHEMA.md`). The old `/map` page is **retired**: `/map`, `/graph`, `/atlas` 301→`/brain`. Data builders `build_graph_page.py` (→`graph_data.json`) + `build_atlas.py` (→`atlas_data.json`) still run nightly to feed the KV-first agent endpoints `/graph_data.json` + `/atlas_data.json` + `/api/atlas`; `catalog/data/source_registry.json` = provenance single-source-of-truth. |
| Nightly ops | `site/ops/` (launchd, 03:00 local); tunables in `site/ops/nightly.env` |
| Plans/docs | `docs/` — `ROADMAP.md` canonical |
| Mathlib checkout | `/Users/jack/Desktop/LEAN/mathlib4` — **read-only; the bot's; don't edit** |

Repo remote: `origin` = `Deicyde/WikiLean`. Branch from `main`; PRs → `Deicyde/WikiLean`.

## Commands

```bash
# Site (wiki/)
cd wiki && npm run deploy          # deploy the Worker (bundles ALL of wiki/src)
cd wiki && npx tsc --noEmit        # typecheck
cd wiki && npm test                # Worker tests
cd wiki && node --experimental-strip-types scripts/build-public.ts   # rebuild static assets (RUN FROM wiki/)
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
- **Render-cache keys are manually versioned + load-bearing** (currently `render:v14:`,
  `page:home:v3`, `page:stats:v2` in `index.ts`) — bump the prefix whenever output bytes
  change, or readers get stale HTML for up to 30 days. Asset changes need `?v=` bumps.
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
- `.dev.vars` is gitignored and holds secrets — never commit, never print values; set Worker
  secrets via `wrangler secret put` / `gh secret set`.
- **Max-auth gotcha**: unset `ANTHROPIC_API_KEY` before `claude`/SDK calls or they fail silently
  ("error result: success", 0 tokens).
- Never take custody of others' API keys. Never edit the mathlib4 checkout — do Lean work in
  `wikifunctions/lean/`.

## Deploy notes
- `npm run deploy` bundles **all** of `wiki/src` — don't leave unreleased Worker WIP committed
  if the nightly may deploy. (The nightly graph refresh uses `wrangler kv put`, NOT a deploy —
  `/graph_data.json` is served KV-first; see `manage/README.md`.)
- Edit asset **sources** (`site/assets/*`, `wiki/assets/editor.js`), then run build-public from
  `wiki/`; never edit `wiki/public/` directly (it's generated + gitignored).
- `index.html` / `sitemap.xml` are served dynamically from D1 — deliberately NOT copied to public.

## How Jack works
- Lead dev + product owner. **Consult each session; surface trade-offs (a recommendation, not a
  survey); review before anything ships upstream** (mathlib PRs, Wikidata submissions).
- **Commit everything to git** (standing instruction — maximum version control); push only when
  asked. Stage explicit paths (an unrelated `site/annotations/*` diff is often in the tree).
- Verify before asserting; be honest about failures. Ultracode is typically on — use Workflows
  for substantive work and adversarially review your own changes before committing.
- Credentials: lead with the earned **Fordham BS**, not the in-progress Stony Brook MA.
