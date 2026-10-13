# D1 backups: keys, decryption, restore

WikiLean's canonical data lives in the production D1 database `wikilean`. It
holds articles and annotations, plus the better-auth tables (`users`,
`sessions`, `accounts`) and `flags`. A backup is therefore as sensitive as the
database itself: it contains emails, session IPs and user agents, and OAuth
tokens.

## What exists

| Path | Horizon | Where it lives |
|---|---|---|
| D1 Time Travel | 7 days on Workers Free, 30 days on Workers Paid (Cloudflare D1 limits) | Cloudflare, on-platform |
| `Backup D1` workflow (`.github/workflows/backup-d1.yml`) | 30 days | GitHub Actions artifacts, **age-encrypted** |
| `wiki/scripts/backup-d1.sh` (`npm run backup:d1`) | as long as you keep the file | the machine that runs it, **age-encrypted** (see below) |

The workflow runs daily (scheduled for 08:27 UTC; GitHub often starts it hours
late) and on demand. Each run uploads two artifacts:

- `wikilean-d1-<ts>`, holding `wikilean-<ts>.sql.age`: the full
  `wrangler d1 export wikilean --remote`, every table.
- `research-export-<ts>`, holding `research-export-<ts>.jsonl.age`: the
  pseudonymized `annotation_events` stream from the bearer-gated
  `GET /api/research/export.jsonl`.

The repository is public, so any signed-in GitHub account can download these
artifacts and read the run logs. Assume the ciphertext is public. Both files
are encrypted with [age](https://age-encryption.org) to the public key(s) in
the repo variable `BACKUP_AGE_RECIPIENTS`, and only the matching private key
can read them.

- On the runner, the plaintext D1 dump exists only in the temp directory, from
  export to encryption. The research export streams straight into age.
- `wrangler d1 export --remote` logs a presigned URL that serves the plaintext
  dump to anyone for an hour. The workflow runs wrangler with
  `WRANGLER_LOG=warn` and discards its stdout, so that URL stays out of the log.
- The job fails **before exporting anything** if `BACKUP_AGE_RECIPIENTS` is
  empty or is not a valid age recipient list. A pasted private key, a malformed
  key and a plugin recipient without its plugin are all rejected.

Runs from before this encryption existed uploaded plaintext and logged that
URL. See [One-time cutover](#one-time-cutover-from-plaintext-backups).

## One-time cutover from plaintext backups

Before this change, every run uploaded the dump as a plaintext `.sql`, and the
research export as plaintext `.jsonl`, under the same artifact names. Each run
log also printed that one-hour presigned plaintext URL. Any GitHub account
could download those artifacts for 30 days. The URLs have expired, but the
artifacts last until their retention ends. These steps are destructive and
outward-facing, so the maintainer decides and runs them:

1. Disable the workflow until the variable is set and this change is merged:
   `gh workflow disable backup-d1.yml --repo Deicyde/WikiLean`.
2. Delete every `wikilean-d1-*` and `research-export-*` artifact created before
   the first encrypted run. List them with the `gh api` command under
   [Key rotation](#key-rotation), filtering on `created_at`, and delete each id.
3. Treat their contents as exposed: revoke the OAuth app's user tokens, rotate
   `BETTER_AUTH_SECRET` (this signs everyone out), and decide whether users
   must be notified.

## One-time setup

Do this on a trusted machine (`brew install age`).

1. **Generate the keypair.** The private key never goes to GitHub: not to the
   repo, not to a secret, not to a runner. The workflow only encrypts, so it
   never needs the private key.

   ```bash
   age-keygen -o wikilean-backup.key          # prints "Public key: age1…"
   age-keygen -y wikilean-backup.key > backup-recipients.txt
   ```

   Keep `wikilean-backup.key` offline with Jack, for example in a password
   manager plus one offline copy. Losing it makes every encrypted backup
   unreadable. A second keypair held separately is a cheap spare: append its
   `age1…` line to `backup-recipients.txt`, and either key can decrypt.
   (`age-keygen -pq` makes a post-quantum `age1pq1…` key. The workflow's
   pinned age v1.3.2 accepts it, but decrypting it needs age ≥ 1.3. age refuses
   to mix post-quantum and classic recipients, so the keys in the list must be
   all `age1pq1…` or all classic. A mixed list fails every run.)

2. **Set the repo variable.** It holds public keys only, one per line, and `#`
   comments are allowed. It is a *variable* rather than a secret because it
   isn't secret: it is printed in the workflow's public run log.

   ```bash
   gh variable set BACKUP_AGE_RECIPIENTS --repo Deicyde/WikiLean < backup-recipients.txt
   gh variable get BACKUP_AGE_RECIPIENTS --repo Deicyde/WikiLean   # should show only age1… lines
   ```

   If an `AGE-SECRET-KEY-…` line ever lands in the variable, the workflow
   refuses it. It is still exposed, though, so rotate (below).

3. **Re-enable the workflow and prove a run end to end.**

   ```bash
   gh workflow enable backup-d1.yml --repo Deicyde/WikiLean
   gh workflow run backup-d1.yml --repo Deicyde/WikiLean
   gh run list --workflow backup-d1.yml --repo Deicyde/WikiLean --limit 1   # note the run id
   ```

   Then download and decrypt that run's artifact (next section). A run is good
   when the artifact holds a single `.sql.age` file that decrypts to SQL.

## Download and decrypt

```bash
mkdir -p ~/wikilean-restore && cd ~/wikilean-restore      # not inside a synced folder
gh run download <run-id> --repo Deicyde/WikiLean -n wikilean-d1-<ts> -D .
age -d -i /path/to/wikilean-backup.key -o wikilean-<ts>.sql wikilean-<ts>.sql.age
```

The research export works the same way: download `research-export-<ts>` and
decrypt `research-export-<ts>.jsonl.age`.

The decrypted `.sql` is the production database, credentials included. Keep it
on an encrypted disk and out of synced folders, and delete it when you're done.

## Restore drill: always local first

Restore into a throwaway local D1 before touching production. `--persist-to`
keeps the drill out of your normal `wrangler dev` state, and an empty directory
avoids collisions with the dump's `CREATE TABLE` statements. Run this from the
repository root:

```bash
cd wiki && npm ci
npx wrangler d1 execute wikilean --local --persist-to ~/wikilean-restore/d1 \
  --file=$HOME/wikilean-restore/wikilean-<ts>.sql
npx wrangler d1 execute wikilean --local --persist-to ~/wikilean-restore/d1 \
  --command "SELECT (SELECT COUNT(*) FROM articles) AS articles, (SELECT COUNT(*) FROM annotation_events) AS events, (SELECT COUNT(*) FROM d1_migrations) AS migrations"
```

Record the wall-clock time and the row counts: a measured restore time is one
input to the roadmap's backup RPO/RTO items. Delete `~/wikilean-restore`
afterwards.

## Restoring production

Pick the shortest path that covers the damage. Both paths below are
destructive operator actions, and both need Cloudflare credentials.

**Time Travel (recent damage, database still exists).** It restores the
database in place to any minute inside the retention window above.

```bash
cd wiki
npx wrangler d1 time-travel info wikilean --timestamp=2026-10-13T08:00:00Z   # check the point exists
npx wrangler d1 time-travel restore wikilean --timestamp=2026-10-13T08:00:00Z
```

`restore` overwrites all data in the database and prints the previous
bookmark. Save that bookmark: restoring to it (`--bookmark=<previous>`) undoes
the restore.

**From an encrypted dump (older than Time Travel, or the database is gone).**
Decrypt it, rehearse it locally as above, and then import it into a **fresh,
empty** D1. The dump's `CREATE TABLE` statements fail against a populated
database.

```bash
npx wrangler d1 create wikilean-restore
npx wrangler d1 execute wikilean-restore --remote --file=$HOME/wikilean-restore/wikilean-<ts>.sql
```

Everything that names the old database has to move with it, or it keeps
reading the abandoned copy without any error:

- `database_id` in `wiki/wrangler.jsonc`, the Worker's `DB` binding. This is a
  Worker change, so it ships through the reviewed release and promotion path.
  Commands run from `wiki/` follow this id as long as `database_name` stays
  `wikilean`. That covers `wiki/scripts/backup-d1.sh`,
  `site/discover_articles.py` and every `wrangler d1 execute wikilean --remote`.
- The database name `wikilean` in `.github/workflows/backup-d1.yml`. The
  workflow runs without `wrangler.jsonc` and resolves the database by name
  through the API. While the old database exists, the workflow keeps backing it
  up and stays green.
- The Brain D1 snapshot acquisition. It pins the id in
  `brain/acquire_d1_snapshot.py`, `brain/d1_snapshot_bundle.py` and the request
  descriptor `brain/authority/requests/d1-community-snapshot-v1.json`. Both
  Python files also pin that descriptor's sha256, so moving the id takes a new,
  reviewed request version. An in-place edit will not work.

Then decrypt the next backup and confirm that it contains post-restore data.

**After either path:** a restore rewinds `articles.version`, so new edits
reuse version numbers whose rendered HTML is still cached under
`render:v<N>:<slug>:<version>` in `RENDER_CACHE` for up to 30 days. Before
reopening writes, bump the render-cache prefix (a Worker change) or purge those
keys. Otherwise readers can be served pages from the discarded timeline. The
`page:*` entries expire within an hour on their own.

## Key rotation

Rotate when a key holder changes, when the private key may have leaked, or on a
routine schedule.

1. Generate the new keypair (setup step 1) and update the variable to the new
   public key(s) (step 2). The next run uses them, with no workflow change.
2. Artifacts made before the change are still encrypted to the old key, and
   they expire 30 days after their run. Keep the old private key until then,
   or delete those artifacts.
3. **If the old private key leaked**, every artifact encrypted to it is
   readable by whoever holds it. Delete them all right away:

   ```bash
   gh api --paginate repos/Deicyde/WikiLean/actions/artifacts \
     --jq '.artifacts[] | select(.name | test("^(wikilean-d1|research-export)-")) | "\(.id) \(.name) \(.created_at)"'
   gh api -X DELETE repos/Deicyde/WikiLean/actions/artifacts/<id>
   ```

   Then treat their contents as exposed: revoke the OAuth app's user tokens,
   rotate `BETTER_AUTH_SECRET`, and consider whether users must be notified.

## Upgrading the pinned age

The workflow downloads `age-<version>-linux-amd64.tar.gz` from the upstream
GitHub release. It checks the tarball against `AGE_SHA256` before extracting,
and installs only the `age` binary. To move to a new release:

```bash
V=v1.x.y   # the new release tag
gh release download "$V" --repo FiloSottile/age -p "age-$V-linux-amd64.tar.gz*"
shasum -a 256 "age-$V-linux-amd64.tar.gz"
gh api "repos/FiloSottile/age/releases/tags/$V" \
  --jq '.assets[] | select(.name == "age-'"$V"'-linux-amd64.tar.gz") | .digest'
```

The two hashes must match. Also verify the tarball's Sigsum proof as described
in age's [SIGSUM.md](https://github.com/FiloSottile/age/blob/main/SIGSUM.md),
then update `AGE_VERSION` and `AGE_SHA256` together.

## The local script

`wiki/scripts/backup-d1.sh` (`npm run backup:d1`) does the same job on your
machine. It exports, encrypts to the same recipients, and keeps only
`wikilean-<ts>.sql.age`. The plaintext lives in a private temp directory and is
deleted as soon as it is encrypted. Like the workflow, it refuses to export if
age or the recipients are missing or invalid. Run it from the repository root:

```bash
cd wiki
export BACKUP_AGE_RECIPIENTS="$(gh variable get BACKUP_AGE_RECIPIENTS --repo Deicyde/WikiLean)"
npm run backup:d1                       # → wiki/backups/wikilean-<ts>.sql.age
```

Decrypt and restore its output exactly like a workflow artifact. Dumps made by
older versions of the script are plaintext `wikilean-<ts>.sql` files in
`wiki/backups/`. Encrypt or delete them:

```bash
age -R backup-recipients.txt -o wikilean-<ts>.sql.age wikilean-<ts>.sql && rm wikilean-<ts>.sql
```
