# Secrets: inventory, rotation, expiry

Every credential WikiLean uses, **by name only**. No value belongs in this file,
in git, in a plist, in a command line or in a log. Values live in the stores named
below and nowhere else.

Last reviewed: 2026-10-17, against `origin/main` @ `11cb6551` plus the
`claude/security-hardening` branch, then rebased onto `43d3ed74`; the `main` commits
in between add no credential (`CF_VERSION_METADATA` is a binding, not a secret).
Dates below come from `gh secret list` (names and update times only) and from the
repository; "unknown" means nothing in the repository records it.

Contents: [rules](#rules) · [Worker secrets](#worker-secrets-wrangler) ·
[credentials stored by the Worker](#credentials-the-worker-stores) ·
[GitHub Actions](#github-actions-secrets-and-variables) ·
[nightly and operator host](#nightly-host-and-operator-machine) ·
[rotation](#rotating-a-worker-secret-without-bypassing-the-promoter) ·
[expiry register](#expiry-register) · [security notes](#security-notes) ·
[P0 actions for the maintainer](#p0-actions-for-the-maintainer)

## Rules

- **Never on argv.** A process's arguments are visible to every user on the host
  (`ps`, `/proc/<pid>/cmdline`). Pass a value on stdin or from a 0600 file:
  `wrangler versions secret put NAME` and `gh secret set NAME` both read stdin;
  curl takes headers with `-H @-` (stdin) or `-H @file`.
  `scripts/test_workflow_credentials.py` fails on an `Authorization` header passed
  as an argument anywhere in tracked code, workflows or docs (shell continuations and
  one-element-per-line argv lists included), and on a Python `--token`/`--secret`-style
  flag. Tools read credentials from the environment or a 0600 file instead.
- **Never print a value.** Not in CI logs, not in chat, not in a commit message.
  `wrangler secret list` and `gh secret list` show names only; use those to audit.
- **One owner per credential.** The holder column below is who can read or replace
  it. Add a row here in the same PR that introduces a new secret.
- **Agents never hold production credentials.** Pipeline agents run sandboxed
  (`site/agent_sandbox.py`): no shell, no writes, cwd outside every checkout.

## Worker secrets (`wrangler`)

Set on the `wikilean` Worker. `wiki/src/env.ts` is the typed list; `AUTH_MODE`
and `BETTER_AUTH_URL` are plain vars in `wiki/wrangler.jsonc`, not secrets.

| Name | Holder | Scope / power | Consumers | Rotation | Expiry |
|---|---|---|---|---|---|
| `PIPELINE_TOKEN` | Jack (Cloudflare account); copies below | Full `bot` role: PUT/POST articles, decide proposals, `/api/work`, `/api/runs`, `/api/admin/revert-run`, `/api/research/export.jsonl`, `/api/queue` | nightly host (`wiki/.dev.vars`), Actions secret `PIPELINE_TOKEN` (backup job, poller as `WIKILEAN_API_TOKEN`) | [Rotation](#rotating-a-worker-secret-without-bypassing-the-promoter), then update every copy (step 5 there). Emergency: the kill switch below | none (static); last set 2026-06-10/11 (ROADMAP) |
| `RESEARCH_EXPORT_SALT` | Jack | Salt for research-export pseudonyms (`index.ts researchExportSalt`) | Worker only | Setting or changing it re-keys every pseudonym once. Unset, the salt falls back to `PIPELINE_TOKEN` | not set yet |
| `BETTER_AUTH_SECRET` | Jack | Signs every better-auth session cookie | Worker only | Rotation signs everyone out | unknown; rotation pending (P0 3) |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | GitHub OAuth app owner (wiki login) | Identity only (`read:user`, `user:email`, `auth.ts`) | Worker login | Generate a new client secret in the OAuth app, put it, then delete the old one in GitHub | GitHub OAuth client secrets do not expire |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | — | Google sign-in | — | — | not set |
| `GITHUB_API_TOKEN` | its creator (unrecorded) | Should be a no-scope classic or read-only fine-grained PAT; `/api/review` GET reads, `/api/repos/github` | Worker only | New PAT with no scopes, put, revoke old | unknown; whether it is set is unverified |
| `REVIEW_GITHUB_CLIENT_ID`, `REVIEW_GITHUB_CLIENT_SECRET` | owner of GitHub App `wikilean-review` | User-to-server tokens with `issues: write`, `pull_requests: write` where the App is installed | `/review` posting | New client secret in the App settings, put, delete old | App client secrets do not expire |
| `REVIEW_GITHUB_APP_SLUG` | same | Not secret (builds the install URL) | `/review` | — | — |
| `REVIEW_TOKEN_KEY` | Jack | Seals stored review GitHub tokens at rest (`review.ts`: HKDF → AES-256-GCM, cookie id as additional data, KV key = SHA-256 of the cookie id). At least 32 characters | Worker only | [Rotation](#rotating-a-worker-secret-without-bypassing-the-promoter) signs reviewers out of `/review` posting (they reconnect). Unset, tokens are stored as plaintext JSON | not set yet ([P1](#p1-seal-review-tokens-and-give-them-their-own-kv-namespace)) |
| `REVIEW_POSTING_PAT` | Deicyde | **Classic `public_repo`**: write to every public repo of its owner | `/review` POST when the connectee is the PAT owner | Prefer retiring it for the GitHub App. Else: new classic PAT with an explicit expiry, put, revoke old, record the expiry here | unknown |
| `IP_HASH_SECRET` | — | Nothing reads it (removed in `783e55c8`) | — | If `wrangler secret list` shows it, delete it with the next promotion (`wrangler versions secret delete IP_HASH_SECRET`) | orphan if present |

Kill switch for `PIPELINE_TOKEN` (no deploy, no rotation): the bearer resolves to the
`users` row `pipeline` (`auth.ts`). Blocking that row makes every bearer request
anonymous at once:

```bash
cd wiki && npx wrangler d1 execute wikilean --remote \
  --command "UPDATE users SET role = 'blocked' WHERE id = 'pipeline'"
# undo: ... SET role = 'bot' WHERE id = 'pipeline'
```

## Credentials the Worker stores

| What | Where | Holder | Notes |
|---|---|---|---|
| Review GitHub user tokens (access + refresh, 12 h TTL): plaintext `reviewtok:<cookie-uuid>`, or sealed `reviewtok:s1:<sha256(cookie)>` once `REVIEW_TOKEN_KEY` is set | KV `REVIEW_TOKENS` when bound, else KV `RENDER_CACHE` (`review.ts reviewTokenStore`) | plaintext: anyone with KV read in the account; sealed: only the Worker | Cloudflare grants KV access per account, so a separate namespace does not keep out a credential that can write `RENDER_CACHE` (the nightly host's `wrangler login`); only the seal does. [P1 step](#p1-seal-review-tokens-and-give-them-their-own-kv-namespace) |
| better-auth OAuth tokens (`accounts.access_token`, `refresh_token`, `id_token`) and sessions (`sessions.token`, `ip_address`, `user_agent`) | D1 `wikilean` | anyone with D1 read; every D1 backup | Logins from 2026-06-15/16 may hold `public_repo` tokens (`git log -- wiki/src/auth.ts`). Pre-2026-10-13 backups were public plaintext: P0 3 |

## GitHub Actions secrets and variables

Repository `Deicyde/WikiLean`. `ci.yml` uses none of them (validation only).

| Name | Kind | Holder | Scope / power | Consumer | Rotation | Last set / expiry |
|---|---|---|---|---|---|---|
| `CLOUDFLARE_API_TOKEN` | secret | Jack (Cloudflare) | Should be an account API token with **D1 Edit on this account only** (`wrangler d1 export` POSTs) | `backup-d1.yml` export step | New token in the Cloudflare dashboard with only that permission, `gh secret set CLOUDFLARE_API_TOKEN --repo Deicyde/WikiLean` (paste at the prompt), delete the old token | set 2026-06-10; never rotated; set an expiry when you rotate |
| `PIPELINE_TOKEN` | secret | Jack | Worker bearer (above) | `backup-d1.yml` research export; `wikidata-poll.yml` as `WIKILEAN_API_TOKEN` (queue publish) | With the Worker secret | set 2026-06-20 |
| `MATHLIB_FORK_PAT` | secret | Deicyde | Classic PAT, `public_repo` + `workflow` (`bot/README.md`, "Token rotation") | `wikidata-poll.yml` | `bot/README.md` steps; record the expiry there | set 2026-06-23; most likely expired 2026-09-21 |
| `CLAUDE_CODE_OAUTH_TOKEN` | secret | Jack (Max subscription) | Claude usage on the Max plan | `bot/triage.py` (tool-less child) | `claude setup-token`, then `gh secret set CLAUDE_CODE_OAUTH_TOKEN` | set 2026-06-20; validity unknown, record it here |
| `QUICKSTATEMENTS_TOKEN` | secret | Jack (Wikidata account "Mynus grey") | Wikidata edits | none today (`WIKILEAN_PUSH_PROPERTY: "0"`) | Delete it until the push is switched on: `gh secret delete QUICKSTATEMENTS_TOKEN --repo Deicyde/WikiLean` | set 2026-07-02 |
| `BACKUP_AGE_RECIPIENTS` | **variable** (public keys) | Jack | Encryption recipients for backups | `backup-d1.yml`, `wiki/scripts/backup-d1.sh` | `docs/BACKUP.md`, "Key rotation" | **not set** (P0 1) |
| `GITHUB_TOKEN` | automatic | GitHub | Per-workflow `permissions:` | all workflows | automatic | per job |

## Nightly host and operator machine

| Name / store | Holder | Used by | Notes |
|---|---|---|---|
| `wiki/.dev.vars` (`PIPELINE_TOKEN=`; for local dev also `AUTH_MODE`, `BETTER_AUTH_SECRET`, OAuth test values) | the host's macOS user | `site/moderate.py`, `site/update_from_upstream.py`, `bot/publish_queue.py`, `site/ops/nightly-runtime.sh` | gitignored; mode 0600; never copy to a shared host |
| `WIKILEAN_API_TOKEN` | env | same readers, checked before `.dev.vars` | export from a 0600 file, never a plist |
| `CLAUDE_CODE_OAUTH_TOKEN` | `site/ops/nightly.local.env` (gitignored) | current nightly wrapper | optional alternative to the CLI's keychain login (`docs/OPERATIONS.md`) |
| Claude CLI login | keychain of the desktop session | nightly review | broken since 2026-09-18 (P0 6) |
| `wrangler login` (OAuth) | wrangler's local auth store | nightly KV pushes, promoter, `d1 execute` | broad account scopes by default; prefer a scoped `CLOUDFLARE_API_TOKEN` exported only for the command |
| `CLOUDFLARE_API_TOKEN` (local) | operator | promoter (`site/ops/brain-promote-release.sh`), `brain/acquire-d1-snapshot.sh` (needs only D1 Read) | export for the one command; never in a file the nightly reads |
| age private key (`wikilean-backup.key`) | Jack, offline | decrypting backups | never on GitHub or a runner (`docs/BACKUP.md`) |
| `gh auth` login | operator | `bot/poll.py --decide`, maintainer commands below | — |
| `ANTHROPIC_API_KEY` | — | must be **unset** for Max auth | `WIKILEAN_KEEP_API_KEY=1` is the explicit API-billing opt-in |

## Rotating a Worker secret without bypassing the promoter

`wrangler secret put NAME` creates **and deploys** a new Worker version on the spot.
That is a deployment outside `site/ops/brain-promote-release.sh`: no journal, no
canary, and the live version no longer matches the last promotion record. Use the
version-only path instead; it rides the next reviewed promotion:

1. Generate the value without echoing it, and pipe it straight into Wrangler, which
   reads stdin when it is not a terminal. `wrangler versions secret put` patches the
   **latest** Worker version with the secret and uploads that as a new version; it
   does **not** deploy it (wrangler 4.120.0 prints "To deploy this version ... use
   `wrangler versions deploy`"):

   ```bash
   cd wiki
   openssl rand -base64 48 | tr -d '\n' \
     | npx wrangler versions secret put <NAME> --message "rotate <NAME> (rides next promotion)"
   ```

   For a value you must also give to another system (`PIPELINE_TOKEN`), write it
   once to a 0600 file outside every checkout, feed that file to each consumer, and
   delete it afterwards:

   ```bash
   umask 077; openssl rand -base64 48 | tr -d '\n' > ~/.wikilean-rotate.tmp
   npx wrangler versions secret put <NAME> --message "rotate <NAME>" < ~/.wikilean-rotate.tmp
   ```

2. Do not run `wrangler versions deploy` for it. The next promotion deploys the
   reviewed release with `wrangler deploy`, and the new version inherits the latest
   version's secrets, the rotated one included.
3. After that promotion, confirm the binding (names only, never values):
   `npx wrangler versions view <deployed-version-id>`.
4. Until that promotion lands, production still accepts the **old** value. Keep
   consumers on the old value until then.
5. For `PIPELINE_TOKEN`, switch the consumers right after the promotion, between
   nightly runs (the nightly starts 03:20 host time; the poller runs every few
   hours): `gh secret set PIPELINE_TOKEN --repo Deicyde/WikiLean < ~/.wikilean-rotate.tmp`,
   then replace the `PIPELINE_TOKEN=` line in `wiki/.dev.vars` on the nightly host
   with an editor, then `rm ~/.wikilean-rotate.tmp`. Set `RESEARCH_EXPORT_SALT`
   first (once, same procedure) so research pseudonyms do not change with the bearer.
6. Record the date in the [expiry register](#expiry-register).

**Emergency** (a leaked `PIPELINE_TOKEN`): use the kill switch above first; it takes
effect immediately and needs no deploy. Use `wrangler secret put` only with Jack's
explicit approval, record the out-of-band deployment in the `docs/ROADMAP.md` status
log, and let the next promotion take the then-live deployment as its prestate.

## Expiry register

Update this table in the PR that rotates anything. Audit names quarterly with
`gh secret list --repo Deicyde/WikiLean`, `gh variable list --repo Deicyde/WikiLean`
and `cd wiki && npx wrangler secret list` (names only).

| Name | Last rotated | Expires | Recorded by |
|---|---|---|---|
| (one row per secret, filled in by the PR that rotates it) | | | |

## Security notes

- **Pipeline agents** (`site/agent_sandbox.py`, enforced by `site/test_agent_sandbox.py`):
  positive `tools=` sets of at most Read/Grep/Glob (plus ToolSearch), `permission_mode="dontAsk"`
  (never `bypassPermissions`), no user/project settings, strict MCP. Agent 1 and the Brain
  team get no tools; Agent 2 and the catalog tagger get Read/Grep/Glob limited to the
  Mathlib checkout plus named, argument-validated MCP search tools (`site/search_tools.py`).
  - **No Bash, even scoped.** The bundled CLI (2.1.239) never checks input-redirect
    targets against the working directory or deny rules, and auto-approves `cat < file`
    as read-only, so `Bash(python3 <cli>:*)` still read any file.
  - **Claude's own directories are denied.** Outside cwd the CLI auto-allows reads of
    `~/.claude/projects/<cwd>/` (every session transcript run there), agent and auto
    memory, tasks and teams, and `/tmp/claude-<uid>/`. The helper adds `Read(...)` deny
    rules for `~/.claude`, `$CLAUDE_CONFIG_DIR` and the `claude-<uid>` temp roots; the CLI
    checks them first and applies them to Grep and Glob.
  - **cwd guard.** A cwd inside or above any checkout (this repository, every worktree
    sharing its `.git`, and `WIKILEAN_PROTECTED_ROOTS`, `:`-separated), the home directory
    or `/` is refused. Agent 2's cwd comes from `WIKILEAN_MATHLIB`, so a mis-set variable
    now fails instead of exposing `wiki/.dev.vars`.
  - `bot/triage.py` is the one CLI agent: `--tools ""`, `--setting-sources ""`,
    `--strict-mcp-config`; the test requires those flags on any pipeline `claude` argv.
  - The production host still runs `claude-agent-sdk` 0.2.82; the helper refuses an SDK
    without `tools=`. `SANDBOX_VERSION` is hashed into `prompt_sha`, so the sandboxed cohort
    is distinguishable in research data.
- **Static asset headers.** `wiki/scripts/build-public.ts` writes `_headers` from
  `SECURITY_HEADERS`; `/brain` may be framed same-origin only. The Brain page gets its own
  Report-Only policy (`BRAIN_CSP_REPORT_ONLY`: the shared one plus the `cdn.jsdelivr.net` d3
  script and `connect-src` for `en.wikipedia.org` summaries and `www.wikidata.org` search),
  so the landing page's `/brain?embed=1` frame no longer reports on every view. A test checks
  that list against `site/build_brain_page.py`'s external scripts and fetches.
- **Request caps.** `/api/queue` 2 MiB and 1000 items; `/mcp` 64 KiB; a POST to `/api/auth/*`
  (anonymous, forwarded to better-auth in oauth mode) 16 KiB (`wiki/src/auth.ts`
  `MAX_AUTH_BODY_BYTES`); every other JSON body
  through `readJsonCapped` (`wiki/src/security.ts` `MAX_JSON_BODY_BYTES`): `/api/flag` 16 KiB
  (anonymous), article create/save 4 MiB, `/api/runs` 64 KiB, Brain edge/node 64 KiB,
  quickstatements 1 MiB, `/api/repos` 256 KiB, `/review` posting 1 MiB. Each answers 413 on a
  declared or streamed body past its cap, after the route's auth and rate-limit checks.
- **Dependency advisories** (`npm audit` in `wiki/`, 2026-10-17):
  - `hono` (runtime): upgraded 4.12.34 to 4.13.12 on 2026-10-17, then to 4.13.13 on
    2026-10-19 (#92), once it was past the two-week cooldown (published 2026-10-04).
    `npm audit --omit=dev` is clean at that commit.
  - `wrangler` 4.120.0 (dev), via miniflare: `undici` ≤7.29.0 (high) and `sharp`
    ≤0.35.5-rc.1 (high). Fixed from wrangler 4.149.0 (sharp 0.35.5, undici 7.29.1;
    9 days old) or 4.151.1 (npm's pick, 1 day old). **Not upgraded**, because:
    the only fully fixed releases are under two weeks old, and this is the binary
    that holds Cloudflare credentials; Wrangler 4.120.0 is pinned by version,
    package integrity and `cli.js` sha256 in `brain/acquire_d1_snapshot.py` and
    `brain/d1_snapshot_bundle.py` (whose own digest is pinned per generation in
    `brain/d1_source_export_profiles.json`), the release runbook verified rollback
    commands against it, and moving it needs a reviewed re-pin of that provenance.
    Exposure is low: undici here is Wrangler's client to the Cloudflare API and the
    local dev server; sharp serves only Miniflare's local Images binding. Upgrade
    together with that re-pin, then run `python3 scripts/backup_wrangler_lock.py
    --write` so the backup job follows.
  - `vitest` 3.2.6 (dev): `tinypool` ≤2.1.1 (critical, prototype pollution to RCE)
    and `@vitest/mocker` (moderate, path traversal). The fix is vitest 5, a major
    upgrade: not done here. Both need an attacker-controlled test run; CI has no
    secrets.
  - `ajv` 8.17.1 (dev, moderate ReDoS with `$data`): 8.20.0 would change the
    generated companion validator (`npm run check:companion-validator`), which is
    Worker source; the generator does not use `$data`. Not upgraded.
  - `source-map-js` ≤1.2.1 (dev, high DoS on crafted source maps): fixable with
    `npm audit fix` (lockfile only); left for the next dependency PR.
- **Stale pin, already on `origin/main`:** `brain/acquire_d1_snapshot.py` and
  `brain/d1_snapshot_bundle.py` pin `PACKAGE_LOCK_SHA256 = 533f09a6…`, which
  `wiki/package-lock.json` has not matched since `d50f5da5`, so the operator D1
  snapshot refuses to run until that pin is reviewed and updated (issue #87). Every
  lockfile change moves the digest again.

## Outstanding production actions

The maintainer tracks outstanding production actions (backup restores, credential revocations and rotations, repository security settings) privately, not in this public repository.

## P1: seal review tokens and give them their own KV namespace

Not P0, but maintainer steps. **The seal is the protection**: Cloudflare API tokens and
`wrangler login` get KV access for the whole account, not per namespace, so any
credential that can write `RENDER_CACHE` can also read a `REVIEW_TOKENS` namespace.

1. Set `REVIEW_TOKEN_KEY` with the
   [version-only procedure](#rotating-a-worker-secret-without-bypassing-the-promoter)
   (`openssl rand -base64 48 | tr -d '\n' | npx wrangler versions secret put REVIEW_TOKEN_KEY`).
   From the next promotion, new and refreshed tokens are stored sealed, and each
   plaintext token is sealed (and its plaintext deleted) on its next read; the rest
   expire within 12 h. Nobody is signed out.
2. Optionally, a namespace of its own (it keeps the tokens out of the cache the nightly
   rewrites; it is not access isolation):

   ```bash
   cd wiki
   npx wrangler kv namespace create REVIEW_TOKENS       # prints the new <namespace-id>
   ```

   Add `{ "binding": "REVIEW_TOKENS", "id": "<namespace-id>" }` to `kv_namespaces` in
   `wiki/wrangler.jsonc` in a reviewed PR and ship it with a promotion. Existing tokens
   in `RENDER_CACHE` keep working: with step 1 done, each one, sealed or plaintext, is
   moved to `REVIEW_TOKENS` sealed (and its `RENDER_CACHE` copies deleted) on its next
   read, and logout deletes it from both; the rest expire within 12 h. Nobody is signed
   out. New and refreshed tokens go only to `REVIEW_TOKENS`.
