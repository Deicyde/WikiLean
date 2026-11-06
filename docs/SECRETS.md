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
| `PIPELINE_TOKEN` | Jack (Cloudflare account); copies below | Full `bot` role: PUT/POST articles, decide proposals, `/api/work`, `/api/runs`, `/api/admin/revert-run`, `/api/research/export.jsonl`, `/api/articles/feedback/export`, `/api/queue` | nightly host (`wiki/.dev.vars`), Actions secret `PIPELINE_TOKEN` (backup job, poller as `WIKILEAN_API_TOKEN`) | [Rotation](#rotating-a-worker-secret-without-bypassing-the-promoter), then update every copy (step 5 there). Emergency: the kill switch below | none (static); last set 2026-06-10/11 (ROADMAP) |
| `RESEARCH_EXPORT_SALT` | Jack | Salt for the pseudonyms of the research export and the formalization feedback export (`security.ts researchExportSalt`; the feedback ones are domain-separated) | Worker only | Setting or changing it re-keys every pseudonym once. Unset, the salt falls back to `PIPELINE_TOKEN` | not set yet |
| `BETTER_AUTH_SECRET` | Jack | Signs every better-auth session cookie | Worker only | Rotation signs everyone out | unknown; rotation pending (P0 3) |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | GitHub OAuth app owner (wiki login) | Identity only (`read:user`, `user:email`, `auth.ts`) | Worker login | Generate a new client secret in the OAuth app, put it, then delete the old one in GitHub | GitHub OAuth client secrets do not expire |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | — | Google sign-in | — | — | not set |
| `GITHUB_API_TOKEN` | Jack | Should be a no-scope classic or read-only fine-grained PAT; `/api/review` GET reads, `/api/repos/github` | Worker only | New PAT with no scopes, put, revoke old | about 2027-01-31; set 2026-11-02 on an undeployed version, live from the next promotion |
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
| better-auth OAuth tokens (`accounts.access_token`, `refresh_token`, `id_token`) and sessions (`sessions.token`, `ip_address`, `user_agent`) | D1 `wikilean` | anyone with D1 read; every D1 backup | Logins from 2026-06-15/16 may hold `public_repo` tokens (`git log -- wiki/src/auth.ts`). Pre-2026-10-13 backups were public plaintext: P0 3; `scripts/revoke_github_tokens.py` revokes the leaked ones |
| Agent tokens (`wla_…`, minted at `/agents`; docs/AGENT-CONTRIBUTIONS.md) | D1 `agent_tokens.token_hash`: SHA-256 only, never the token | the person who minted it holds the token | Shown once at minting. The owner revokes at `/agents`; an admin blocks the agent's or the owner's `users` row. Write power is an agent's (`role` `agent`), not the pipeline's |

## GitHub Actions secrets and variables

Repository `Deicyde/WikiLean`. `ci.yml` uses none of them (validation only).

| Name | Kind | Holder | Scope / power | Consumer | Rotation | Last set / expiry |
|---|---|---|---|---|---|---|
| `CLOUDFLARE_API_TOKEN` | secret | Jack (Cloudflare) | Should be an account API token with **D1 Edit on this account only** (`wrangler d1 export` POSTs) | `backup-d1.yml` export step | New token in the Cloudflare dashboard with only that permission, `gh secret set CLOUDFLARE_API_TOKEN --repo Deicyde/WikiLean` (paste at the prompt), delete the old token | set 2026-06-10; never rotated; set an expiry when you rotate |
| `PIPELINE_TOKEN` | secret | Jack | Worker bearer (above) | `backup-d1.yml` research export; `wikidata-poll.yml` as `WIKILEAN_API_TOKEN` (queue publish) | With the Worker secret | set 2026-06-20 |
| `MATHLIB_FORK_PAT` | secret | Deicyde | Classic PAT, `public_repo` + `workflow` (`bot/README.md`, "Token rotation") | `wikidata-poll.yml` | `bot/README.md` steps; record the expiry there | set 2026-11-01; expires about 2027-01-29 |
| `CLAUDE_CODE_OAUTH_TOKEN` | secret | Jack (Max subscription) | Claude usage on the Max plan | `bot/triage.py` (tool-less child) | `claude setup-token`, then `gh secret set CLAUDE_CODE_OAUTH_TOKEN` | set 2026-06-20; validity unknown, record it here |
| `QUICKSTATEMENTS_TOKEN` | secret | Jack (Wikidata account "Mynus grey") | Wikidata edits | none today (`WIKILEAN_PUSH_PROPERTY: "0"`) | Delete it until the push is switched on: `gh secret delete QUICKSTATEMENTS_TOKEN --repo Deicyde/WikiLean` | set 2026-07-02 |
| `BACKUP_AGE_RECIPIENTS` | **variable** (public keys) | Jack | Encryption recipients for backups | `backup-d1.yml`, `wiki/scripts/backup-d1.sh` | `docs/BACKUP.md`, "Key rotation" | set 2026-10-19 |
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
| `WIKILEAN_ARTICLES_R2_ACCESS_KEY_ID`, `WIKILEAN_ARTICLES_R2_SECRET_ACCESS_KEY` (the S3 pair of an R2 API token) | Jack | `scripts/articles/upload_store.py`, publishing the articles store (`scripts/articles/UPLOAD.md`) | Object Read & Write on the articles bucket only, with an expiry (add it to the register below); from the environment or a 0600 file on the operator machine only, never argv, Actions or the fleet host; not created yet |
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
   first (once, same procedure) so research and formalization-feedback pseudonyms do not change
   with the bearer.
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
| `GITHUB_API_TOKEN` (Worker) | 2026-11-02, [version-only](#rotating-a-worker-secret-without-bypassing-the-promoter); live from the next promotion | about 2027-01-31 (90 days) | Jack |
| `MATHLIB_FORK_PAT` (Actions) | 2026-11-01 UTC (2026-10-31 US Eastern) | about 2027-01-29 (90 days); reminder in `bot/README.md`, "Token rotation" | Jack |

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
- **The articles plane's pages enforce a policy.** Every `/articles` page enforces
  `articlesCsp` (`wiki/src/security.ts`) beside the framing ban: scripts only its two files
  (`/assets/articles.js` and `/assets/articles-reader.js` on the page's own origin, not every
  same-origin script), the Web Analytics beacon and the shell's two inline scripts by sha256;
  forms only to the list's search (`/articles`); images and media only from the site and
  Wikimedia's upload hosts. A reader page serves a stored Wikipedia revision, so the policy
  backs up the builder's sanitiser (`docs/ARTICLES.md`, section 8).
- **Request caps.** `/api/queue` 2 MiB and 1000 items; `/mcp` 64 KiB; a POST to `/api/auth/*`
  (anonymous, forwarded to better-auth in oauth mode) 16 KiB (`wiki/src/auth.ts`
  `MAX_AUTH_BODY_BYTES`); every other JSON body
  through `readJsonCapped` (`wiki/src/security.ts` `MAX_JSON_BODY_BYTES`): `/api/flag` 16 KiB
  (anonymous), article create/save 4 MiB, `/api/runs` 64 KiB, Brain edge/node 64 KiB,
  quickstatements 1 MiB, `/api/repos` 256 KiB, `/review` posting 1 MiB, `/api/articles/feedback`
  4 KiB. Each answers 413 on a declared or streamed body past its cap, after the route's
  auth and rate-limit checks.
- **Dependency advisories** (`npm audit` in `wiki/`, 2026-10-17):
  - `hono` (runtime): upgraded 4.12.34 to 4.13.12 on 2026-10-17, then to 4.13.13 on
    2026-10-19 (#92), once it was past the two-week cooldown (published 2026-10-04).
    `npm audit --omit=dev` is clean at that commit.
  - `wrangler` (dev): upgraded 4.120.0 to 4.151.3 on 2026-11-02 (#110), the day before
    it cleared the two-week cooldown (published 2026-10-20). That fixes `sharp`
    ≤0.35.5-rc.1 (now 0.35.5) and `undici` ≤7.29.0 (now 7.29.1). Still open: later `undici`
    advisories fixed in 7.30.1 (high, including a TLS-options bypass), which first ships in
    Wrangler 4.153.0 (published 2026-10-26), and a moderate Wrangler advisory fixed
    in the same release. Re-pin to 4.153.0 or later once it clears the cooldown.
    Exposure: undici here is Wrangler's client to the Cloudflare API and the local
    dev server. Wrangler holds Cloudflare credentials, so it moves only with a
    reviewed re-pin: `brain/acquire_d1_snapshot.py` and `brain/d1_snapshot_bundle.py`
    pin its version, package integrity, `cli.js` sha256 and its dependency closure in
    `wiki/package-lock.json`, dependency declarations included (#87; the same closure
    `.github/backup-wrangler` holds, so lockfile changes outside it no longer break the
    acquirer). A re-pin adds an
    acquirer generation to `brain/d1_snapshot_bundle.py`, which keeps older
    acquisitions verifiable, and a profile to `brain/d1_source_export_profiles.json`;
    then run `python3 scripts/backup_wrangler_lock.py --write`.
  - `vitest` (dev): upgraded 3.2.6 to 5.0.5 on 2026-11-02 (#91), past the two-week
    cooldown (published 2026-10-15). That clears `tinypool` ≤2.1.1 (critical,
    prototype pollution to RCE) and `@vitest/mocker` (moderate, path traversal).
    vitest 5 makes `vite` a required peer, which `wiki/.npmrc`'s
    `legacy-peer-deps=true` does not install, so `vite` 7.3.7 is a direct
    devDependency; 7.3.6 and earlier carry the `launch-editor` and dev-server
    advisories.
  - `ajv` 8.17.1 (dev, moderate ReDoS with `$data`): 8.20.0 would change the
    generated companion validator (`npm run check:companion-validator`), which is
    Worker source; the generator does not use `$data`. Not upgraded.
  - `source-map-js` ≤1.2.1 (dev, high DoS on crafted source maps): 1.2.2 since #91,
    through `vite`'s `postcss`.

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
