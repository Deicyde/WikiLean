# Publishing the atlas store

`upload_store.py` publishes a store built by `build_store.py` to the R2 bucket the Worker reads
as `ATLAS` (docs/ATLAS.md, section 6). Publishing is an operator action: run it from your own
machine, never from CI, a shared host or the nightly. The script uses Python's standard library
only. For TLS it uses certifi's or truststore's CA store when either is installed.

## What you need

- The atlas reader's Worker and builder (`wiki/src/atlas.ts` and `scripts/atlas/build_store.py`
  as the atlas reader change leaves them), merged before this script is used. The default
  `--builder` is the `build_store.py` beside this script: an older one builds a single
  `cf-locate-recordset/1` set with no `--campaign` and no side files, so its `--check` refuses a
  batch-1 store. The publish switch below is that Worker's too: an older Worker reads
  `current.json` only on `/atlas` and `/api/atlas/current`, so deleting it would leave the
  article pages, the JSON views and the article box serving.
- The store directory (the builder's `--out`, which holds `atlas/v1/`) and the exact builder
  arguments that built it. The script runs `build_store.py --check` with them, so run it with
  the Python the builder uses (the builder needs `jsonschema`).
- The bucket's name and the Cloudflare account id. The endpoint is
  `https://<account-id>.r2.cloudflarestorage.com`.
- An R2 API token with Object Read & Write on that bucket only, with an expiry, recorded in
  `docs/SECRETS.md`. The script reads its S3 credentials from
  `WIKILEAN_ATLAS_R2_ACCESS_KEY_ID` and `WIKILEAN_ATLAS_R2_SECRET_ACCESS_KEY`. No option takes
  a credential, the script never prints one, and the builder's check runs without them in its
  environment.

## Credentials

Keep them in a file outside every checkout. Make the file private before anything is written
to it:

```bash
install -m 600 /dev/null <file>
```

Then open it in an editor and add the two lines:

```
WIKILEAN_ATLAS_R2_ACCESS_KEY_ID=<access key id>
WIKILEAN_ATLAS_R2_SECRET_ACCESS_KEY=<secret access key>
```

Pass it with `--credentials-file <file>`. The script refuses a file that is a symbolic link,
belongs to another user, or gives anyone else any access.

To use the environment instead, read both values at prompts that do not echo them:

```bash
printf 'Access key id: ';     read -rs WIKILEAN_ATLAS_R2_ACCESS_KEY_ID;     echo
printf 'Secret access key: '; read -rs WIKILEAN_ATLAS_R2_SECRET_ACCESS_KEY; echo
export WIKILEAN_ATLAS_R2_ACCESS_KEY_ID WIKILEAN_ATLAS_R2_SECRET_ACCESS_KEY
```

Never type a value after `export` or in any other command: the shell saves commands to its
history. `unset` both variables when you are done.

## Run it

The default is a dry run. It checks the store, reads the bucket and prints the plan: every
object it would upload, in order, and every object it would delete. It writes nothing.

```bash
python3 scripts/atlas/upload_store.py --store <store-dir> --bucket <bucket> \
  --account-id <account-id> --credentials-file <file> \
  -- --record-set <set-dir> [the other builder arguments]
```

Everything after `--` goes to the builder. Leave out `--out` and `--check`; the script adds them.
When the plan is what you expect, run the same command with `--execute`. It then:

1. verifies every file against `manifest.json` and runs the builder's `--check` on the store,
   refusing if the check fails;
2. lists `atlas/v1/` in the bucket and prints the plan (a dry run stops here);
3. uploads every object except `current.json`: universes, records and reader pages first,
   then articles, index shards and `manifest.json`, each checked by its ETag;
4. uploads `current.json`, which publishes the build;
5. deletes the objects under `atlas/v1/` that the new manifest no longer lists, and nothing
   outside `atlas/v1/`;
6. lists `atlas/v1/` again and checks every object against `manifest.json`: none missing,
   none extra, and each size, ETag and sha256 (read back).

At the end it prints the sha256 of `manifest.json` and `current.json`, and the command that
rolls the atlas back to this build. Record them with the build. The site may take a minute to
show the new build: each Worker isolate keeps what it read of `current.json`, the universe and
the index shards for up to 60 seconds. On a first publish nothing shows before `current.json`
lands: until then every atlas route answers 503.

While a later publish runs, the previous `current.json` still in place, a reader can briefly see
a new object beside an old one. Each article stays consistent, so the worst cases are short-lived:
a newly added article can answer 404 from the index; an article whose new reader page
(`reader/<pageid>.html`) has landed before its new view, which names the old page's sha256, shows
its card list until the view lands; and records that have landed before their view answer 503
from `/api/atlas/records` until it does.

The plan's `listing` line says whether the bucket's listing declared its keys URL-encoded. When
it does not, a key that reads differently decoded could be either of two keys, so the script
refuses the run if it finds one.

## Roll back

A rollback re-runs the builder's `--check` on the kept build. So keep, for every published
build:

- the store directory;
- the exact builder arguments;
- an unchanged copy of every record set and side file those arguments name;
- the builder at its commit (each run prints the builder's sha256).

`--rollback-to` refuses unless they still build that store exactly. A publish ends by printing
the rollback command for its own build. Its general form:

```bash
python3 scripts/atlas/upload_store.py --rollback-to <previous-store-dir> --bucket <bucket> \
  --account-id <account-id> --credentials-file <file> -- <that build's builder arguments>
```

It runs the same steps, the check included, as a dry run until you add `--execute`. If an older
builder made that store, point `--builder` at that commit's `build_store.py` (from a worktree at
that commit). The two modes check direction by each build's `built_at`: `--store` refuses a
build older than the live one, and `--rollback-to` refuses one newer than it, so an old
directory cannot be published by mistake.

## Take the atlas offline

`current.json` is the publish switch. Without it, every atlas route but the feedback export
answers 503 "atlas store has no published build" and no article page shows the atlas box,
whatever else the bucket holds. Each Worker isolate keeps what it read of `current.json`, its
absence included, for up to a minute, so deleting `atlas/v1/current.json` alone takes every atlas
route to 503 within about a minute, with no release. Browsers may keep a JSON view they already
fetched for up to 5 minutes (`max-age=300`).

To remove everything:

```bash
python3 scripts/atlas/upload_store.py --unpublish --bucket <bucket> \
  --account-id <account-id> --credentials-file <file>
```

It lists `atlas/v1/` and prints every object it would delete, as a dry run until you add
`--execute`. With `--execute` it deletes `current.json` first, which takes the atlas offline as
above, then every other object under `atlas/v1/` and nothing outside it, then lists `atlas/v1/`
again to check that nothing is left. It needs no store and runs no builder. To bring the atlas
back, publish a kept build.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | Done and checked, or the dry run's plan printed | Nothing |
| 1 | The bucket did not match after writing began: an upload's ETag, a store file that changed during the upload, the final check, or objects left after `--unpublish` | The message says what the bucket holds and what readers see. Run again with `--execute`, or roll back |
| 2 | Refused before writing anything: the arguments, the credentials, the store, the builder's check, or a bucket listing the script will not act on | Fix what the message names |
| 3 | A request failed after three attempts, or an internal error | The message says what the bucket holds and what readers see, or that nothing was written. Run again |
| 130 | Interrupted (Ctrl-C) | The message says what the bucket holds. Run again |

Run one upload at a time.

## Tests

`scripts/atlas/test_upload_store.py` (in `scripts/ci-python.sh`) runs the script against an
in-process fake S3 server. The server refuses any request whose SigV4 signature, payload hash or
Content-MD5 does not match. The tests cover the order of uploads and deletes, the dry run,
credentials in output, a failing check, verification failures, retries, deletes confined to
`atlas/v1/`, listings the script must not act on, what each failure reports and a re-run after
it, `--unpublish`, and rollback, including the printed rollback command.
