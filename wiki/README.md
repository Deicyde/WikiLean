# wiki/ — the WikiLean live wiki backend

A Cloudflare Worker that serves [wikilean.jackmccarthy.org](https://wikilean.jackmccarthy.org) dynamically — reading article state from D1, injecting an in-page editor for authenticated users, and persisting their edits with full revision history.

```
        wikilean.jackmccarthy.org  (Cloudflare route)
                       │
                       ▼
        Cloudflare Worker  ←──── KV: RENDER_CACHE, WP_HTML
        (Hono + Drizzle + better-auth)
                       │
                       ▼
                   D1 (SQLite)
                   ├ articles    one row per slug, annotations as JSON
                   ├ revisions   every edit (seed + user)
                   ├ users       better-auth user records
                   ├ sessions
                   ├ accounts
                   └ verifications
```

## Stack

| | |
|---|---|
| Runtime | Cloudflare Worker (`compatibility_date 2026-05-01`, `nodejs_compat`) |
| Web framework | [Hono](https://hono.dev/) |
| DB | [D1](https://developers.cloudflare.com/d1/) (Cloudflare's serverless SQLite) |
| ORM | [Drizzle](https://orm.drizzle.team/) |
| Auth | [better-auth](https://better-auth.com/) — GitHub OAuth, sessions in D1 |
| Cache | KV: `RENDER_CACHE` (rendered pages), `WP_HTML` (upstream Wikipedia HTML) |
| Rate limiter | Cloudflare native ratelimit — 30 writes/minute per user (`EDIT_LIMITER`) |
| Tests | [Vitest](https://vitest.dev/) |

## Layout

```
src/
  index.ts          Worker entrypoint + routing (Hono)
  pages.ts          server-side chrome injection (auth bar, editor, history page)
  auth.ts           better-auth wiring; selects mode by env.AUTH_MODE
  env.ts            Env binding types
  wikipedia.ts      upstream MediaWiki fetcher (with KV caching)
  engine/
    html.ts         HTML escaping + the annotation wrap pipeline
    page.ts         renders a full article from D1 row + cached upstream HTML
    types.ts        Annotation shape (must match site/annotations/*.json)
    wrap.ts         the actual span-wrapping algorithm (mirrors site/render.py)
  db/schema.ts      Drizzle schema (matches the SQL migrations)
  seed/build.ts     reads site/ artifacts → seed rows

migrations/
  0001_init.sql     articles + revisions
  0002_users.sql    user table (better-auth compatible layout)
  0003_auth.sql     sessions, accounts, verifications

scripts/
  seed.ts           generate seed.sql from site/ (full populate)
  seed-delta.ts     generate delta.sql: only NEW slugs (never clobbers)
  seed-refresh.ts   generate refresh.sql: UPDATE changed slugs, SKIP user-edited
  backup-d1.sh      wrangler export, age-encrypted → ./backups/wikilean-<utc>.sql.age
  build-mathlib-index.ts   builds public/assets/mathlib-index.json

public/              static assets served by the Worker (`ASSETS` binding)
  index.html, concepts.html, graph.html, sitemap.xml, …
  assets/editor.js, review.css, style.css, script.js
```

## Routes

| Verb | Path | What |
|---|---|---|
| GET | `/login` | Sign in (better-auth UI) |
| GET | `/api/auth/get-session` | Current session (null when anon) |
| GET | `/api/auth/*` | better-auth's OAuth callbacks etc. |
| GET | `/api/version` | Live Worker version and Brain release: public, no D1, always 200, `Cache-Control: no-store`. `worker.version_id`, `version_tag` and `uploaded_at` come from the `version_metadata` binding and are null without it. `brain_release` (`release_id`, `manifest_sha256`, and `git_commit`, the WikiLean commit the release was built from) is null when the release can't be resolved. A `brain-<release hex12>-<attempt>` tag is evidence of a promoter deploy, not proof: any credentialed `wrangler deploy --tag` can set it. |
| GET | `/recent-changes` | Feed of recent edits |
| GET | `/:slug/history` | Revisions for one article |
| GET | `/:slug` *(or `.html`)* | Render article — injects sign-in CTA when anon, editor + full annotation model when signed in |
| POST | `/api/article/:slug` | Save edited annotations (requires auth, rate-limited) |
| POST | `/api/article/:slug/revert/:revid` | Revert to a previous revision (auth + rate-limited) |
| GET | `/api/runs` | Public per-run pipeline counts, newest received first (`created_at`, then `run_id`), as `{runs: [...]}`. `?kind=` is `review`, `wp-update`, `new` or `all` (`all` is the kind a combined `moderate.py all` run records, not a wildcard; the nightly posts separate `wp-update` and `review` rows; omit `kind` for every kind). `?limit=` is 1..50, default 10. An unknown kind or an out-of-range limit returns 400 (no clamping). Fields: `run_id`, `kind`, `started_at`, `finished_at`, `articles_processed`, `errors`, `tokens`, `cost_usd_equiv`, `model`, `prompt_sha`; no notes. `Cache-Control: public, max-age=300`. |

## Local dev

```sh
cd wiki
npm install
cp .dev.vars.example .dev.vars   # if it exists; otherwise create — see below

# Run the Worker locally (uses local D1; .dev.vars overrides production secrets)
npm run dev

# Run tests
npm test
```

### `.dev.vars` (gitignored)

For local development without real OAuth:

```sh
AUTH_MODE=dev               # accepts a cookie stub instead of real OAuth (exactly "dev";
                            # any value other than dev/oauth disables login entirely)
BETTER_AUTH_SECRET=$(openssl rand -base64 32)
BETTER_AUTH_URL=http://localhost:8787
```

For testing with real GitHub OAuth locally:

```sh
AUTH_MODE=oauth
BETTER_AUTH_SECRET=$(openssl rand -base64 32)
BETTER_AUTH_URL=http://localhost:8787
GITHUB_CLIENT_ID=<from a GitHub OAuth app, callback=http://localhost:8787/api/auth/callback/github>
GITHUB_CLIENT_SECRET=<…>
```

Production reads the same names as Worker secrets (`wrangler secret put NAME`).

## Seeding D1 from `site/`

> **Legacy.** D1 is canonical and is never re-seeded from disk. The pipeline now reads and
> writes D1 directly through the Worker API (`site/moderate.py`). `seed-delta` and
> `seed-refresh` are retired legacy migration tools (docs/ROADMAP.md); do not run them
> against production.

The pipeline that produces local annotations lives in `site/`. To sync new and changed annotations to the live D1:

```sh
# (insurance) snapshot D1 first. Needs age (`brew install age`) and the backup
# public keys; without them it exits before exporting (see docs/BACKUP.md).
export BACKUP_AGE_RECIPIENTS="$(gh variable get BACKUP_AGE_RECIPIENTS --repo Deicyde/WikiLean)"
npm run backup:d1

# 1. INSERT new articles (slugs not yet in D1). Idempotent.
npm run seed:delta
npx wrangler d1 execute wikilean --remote --file=delta.sql

# 2. UPDATE changed articles — but SKIP any with user revisions.
#    Idempotent. Preserves edits made through the live editor.
npm run seed:refresh
npx wrangler d1 execute wikilean --remote --file=refresh.sql
```

> Don't use `seed:sql` (the full `seed.sql`) on production — it INSERTs without conflict handling and will fail on existing rows. Reserve it for first-time provisioning of an empty database.

## Deploy

Production deploys go only through the exact Brain release promoter,
`site/ops/brain-promote-release.sh`, following
[`docs/BRAIN-RELEASE-RUNBOOK.md`](../docs/BRAIN-RELEASE-RUNBOOK.md). Since 2026-10-04 production
serves an immutable Brain release ([activation record](../docs/BRAIN-ACTIVATION-2026-10-04.md)).
`npm run deploy` refuses and points to the promoter. Never run `wrangler deploy` directly: it
uploads the Worker built from the local `src/` and the whole local `public/` directory outside the
promoter, replacing the live Brain selector and the whole asset tree without the journal or canary.
Worker source changes are release-affecting and ship only in a new release. The only emergency
lever is `wrangler rollback` to a recorded version, per the runbook's "Wrangler rollback" section.
There is no forward hotfix path; one, if ever wanted, would be a journaled promoter mode.

```sh
npm run tail          # follow Worker logs
```

Worker secrets needed in production:
- `BETTER_AUTH_SECRET` — random 32-byte secret (already set)
- `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` — GitHub OAuth app (already set)

Optional:
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` — adds Google sign-in (not currently set)

## Edit-safety guarantees

The seeding scripts (`seed-delta`, `seed-refresh`) and the AI moderation pipeline (`site/batch_annotate.py --moderate`) share one invariant: **human contributions are never silently lost**.

- `seed-delta` only INSERTs new slugs.
- `seed-refresh` explicitly skips any slug with a `user_id IS NOT NULL` revision.
- `--moderate` mode in the annotation pipeline runs `_preserve_human()` after both agents: human annotations are restored verbatim by anchor signature; `ai-moderated` provenance can't be silently downgraded to `ai`.

These are deterministic guards backed by unit tests — not just prompt instructions. See `site/batch_annotate.py`.

## Operational notes

- **Backups**: `npm run backup:d1` dumps full SQL via D1's official export endpoint and keeps only the age-encrypted `wiki/backups/wikilean-<utc>.sql.age`. It needs `age` installed and `BACKUP_AGE_RECIPIENTS` set, and exits without exporting otherwise; see [`docs/BACKUP.md`](../docs/BACKUP.md) for setup, decryption and restore. Run before big seed operations.
- **Rate limit**: 30 writes per user per minute (`EDIT_LIMITER` binding). Adjust in `wrangler.jsonc`.
- **Cache invalidation**: KV `RENDER_CACHE` keys are versioned by annotation `version` (incremented on each save), so saves are seen immediately by the next reader.
- **Asset versioning**: when bumping `public/assets/editor.js` or `review.css`, also bump the `?v=N` query in `src/pages.ts` so browsers refetch.
