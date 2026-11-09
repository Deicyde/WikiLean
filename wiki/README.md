# WikiLean Worker

The Cloudflare Worker serves the article reader, contribution and account pages,
annotation APIs, and Brain APIs at
[wikilean.jackmccarthy.org](https://wikilean.jackmccarthy.org).
It uses Hono, Drizzle/D1, better-auth, KV, and R2.

## Local development

Use Node 22 and Python 3.12. From this directory:

```sh
npm ci
npm run test:ci
npx playwright install chromium
npm run test:e2e
```

For a local preview with synthetic articles and accounts, run:

```sh
npx tsx e2e/server.ts
```

Open `http://127.0.0.1:4173`. This fixture server blocks external requests,
uses disposable in-memory data, and needs no production credentials. Its small
corpus is for UI development, not an estimate of production coverage.

`npm run dev` starts Wrangler for development that needs local Cloudflare bindings.
Use `.dev.vars` for local secrets; it is ignored by git. Never commit or print
credentials. The [secret inventory](../docs/SECRETS.md) documents configuration.

## Code map

| File | Responsibility |
|---|---|
| `src/index.ts` | Worker routes, scheduled entry, canonical annotation API |
| `src/home.ts`, `src/site-nav.ts` | Home, About, Contribute, primary navigation |
| `src/articles.ts` | Published reader store, feedback, community tag routes |
| `src/articles-render.ts` | Reader and article directory HTML |
| `src/articles-overlay.ts` | Combine pipeline links with community tags |
| `src/auth.ts`, `src/agents.ts` | Sign-in and owned agent accounts |
| `src/brain-api.ts`, `src/mcp.ts` | Brain REST and MCP interfaces |
| `src/native-companion.ts` | Literature Links validation and compatibility |
| `src/db/`, `migrations/` | Schema and ordered D1 migrations |
| `test/`, `e2e/` | Hermetic Worker and browser tests |

The public contribution flow is `/articles` → passage → tag or feedback.
`/contribute` explains it; `/agents` manages agent identities. Legacy article
URLs and moderation tools remain for compatibility, but old slug editing is off.
See [Articles](../docs/ARTICLES.md), [Brain API](../docs/BRAIN-API.md), and
[Literature Links](../docs/COMPANION-NATIVE.md) for contracts.

## Data and generated assets

D1 is canonical for annotations, revisions, and community tags. Never re-seed
production from `site/annotations/`. The historical `seed:*` scripts are not a
supported contribution or synchronization path.

Edit asset sources in `../site/assets/` and `assets/editor.js`. Do not edit the
generated, ignored `public/` tree. Rebuild assets from this directory with:

```sh
node --experimental-strip-types scripts/build-public.ts
```

A complete asset build requires the corpus inputs described by that script.
The fixture server serves source assets directly, so it does not need that build.
Bump the asset query version when changing an asset, and the corresponding
render-cache prefix when changing cached HTML. See [AGENTS.md](../AGENTS.md).

## Production operations

Follow the [release runbook](../docs/BRAIN-RELEASE-RUNBOOK.md). Production changes
go only through `site/ops/brain-promote-release.sh`, with an exact verified
release, journal, and canary. Worker changes require a new release.
`npm run deploy` deliberately refuses; direct `wrangler deploy` is not supported.

Use [BACKUP.md](../docs/BACKUP.md) for encrypted backups and restore checks, and
[SECRETS.md](../docs/SECRETS.md) for rotation. Secret changes use the versioned
procedure in those runbooks; `wrangler secret put` deploys immediately and bypasses
the release procedure.

The live `/api/version` reports current Worker and Brain identities. Historical
activation records are evidence of previous deployments, not a current-status
registry. See [HANDOFF.md](../HANDOFF.md) for the operator entry points.
