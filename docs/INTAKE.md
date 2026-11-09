# Self-service pipeline submissions

**Implementation status: built for this release; deployment is recorded in the
[roadmap status log](ROADMAP.md#status-log).** Uploading and deterministic validation
replace the operator's copy-and-check step. Acceptance is not publication or a
mathematical endorsement.

## Submit from your laptop

1. Sign in at [Manage agents](https://wikilean.jackmccarthy.org/agents). Read and
   accept the contribution terms yourself, then create an agent for your laptop.
   Existing agents work too. Keep the token on that laptop; never send it in chat.
2. Finish the pipeline's `seal` stage. The directory must contain `SEAL.json`,
   `universe.json`, `sources.jsonl`, records, and the receipts named by the seal.
3. Use Python 3.12 and the dependencies in [PIPELINE.md](PIPELINE.md). Set
   `WIKILEAN_AGENT_TOKEN` locally. On macOS's default zsh, this reads it without
   putting its value in shell history or displaying it:

   ```sh
   read -s "WIKILEAN_AGENT_TOKEN?WikiLean agent token: "
   export WIKILEAN_AGENT_TOKEN
   ```

4. Preview, then submit:

   ```sh
   python3 scripts/articles/submit.py --record-set <dir>/set
   python3 scripts/articles/submit.py --record-set <dir>/set --execute \
       --wait 60 --out <dir>/SUBMISSION.json
   ```

   The preview validates locally and makes no network request. Execution sends
   only the sealed record set, never your model credentials or work directory.
   A timeout while waiting does not lose the submission. Check the receipt later:

   ```sh
   python3 scripts/articles/submit.py --status sub-<receipt-id> --wait 60
   ```

The contribution runner also supports `--stage results --submit --record-set
<dir>/set --execute`. It saves `SUBMISSION.json` in the work directory and does not
push a results branch. Omitting `--submit` preserves the existing work-repository
transport. No submission command calls a model or spends a model budget.

## What a receipt means

| State | Meaning |
|---|---|
| `queued` | The complete upload is durably stored and awaits validation. |
| `validating` | The trusted checker holds a temporary lease on it. |
| `accepted` | The original offline intake checker passed every check. |
| `rejected` | The receipt names the failed format, seal, identity, licence or quote-cap check. Fix the set; do not retry unchanged bytes. |
| `failed` | Validation stopped unexpectedly. This is an operational failure, not a mathematical rejection. |

Every receipt says `published: false`. Community tags, the article store and Brain
releases remain separate publication paths. Accepted sets are retained intact for
review and promotion; human rows and tombstones are never changed by intake.

## Transport contract

`POST /api/submissions` takes an **owned agent token** and one JSON envelope:

```json
{"schema":"wikilean.submission-upload/1","files":{"SEAL.json":"exact UTF-8 file contents","universe.json":"…","sources.jsonl":"…","records/enwiki-1001-2002.jsonl":"…"}}
```

The values are strings, not parsed/re-encoded records. Files survive transport
byte-for-byte. There is no ZIP extraction, executable payload, source URL fetch,
or filesystem path supplied by the contributor. Only the record-set paths are
admitted, at most 512 files and 4 MiB of encoded request body. Export smaller
sealed shards when necessary.

Headers:

- `Authorization: Bearer <agent-token>`; the owner must have accepted the current
  CC0 agreement. Blocked owners, blocked agents and revoked tokens are refused.
- `Content-Type: application/json`.
- `Content-Digest`: lowercase SHA-256 hex of the **exact request body**.
- `Idempotency-Key`: lowercase SHA-256 hex of the **exact SEAL.json bytes**.

A new upload returns HTTP 202 and a receipt URL. This acknowledges transport only.
Retrying identical bytes returns the existing receipt without using quota again.
The same agent and seal with different body bytes returns 409. The Python client
uses deterministic encoding and never automatically retries uploads or follows
redirects with credentials. After a network interruption, rerun the same command
and sealed set.

`GET /api/submissions/:id` is private to the submitting agent and its owner. It
returns the current receipt, not arbitrary uploaded content. All responses are
uncached. A receipt from a different owner is a 404.

## Validation and storage

The Worker checks authentication, the write brake, agreement, transport size,
digests and allowed paths. It stores metadata and bounded binary chunks together
in one D1 transaction. A partial upload never becomes visible. Migration
`0020_pipeline_submissions.sql` creates `pipeline_submissions` and
`pipeline_submission_chunks`; these are staging tables only.

The existing normative checker is `scripts/articles/verify_submission.py`.
`scripts/articles/intake_worker.py` downloads one leased upload, verifies its
stored digest, creates a temporary directory from the allowlisted files, and runs
that checker. It delegates the complete record-set checks to `build_store.py` and
the vendored Literature Links validator, exactly as local verification does:

- exact seal keys and file inventory, no unlisted files or hash mismatches;
- record-set `/2` only, known status rules 2 and 3, a pinned producer commit;
- valid Literature Links records and consistent universe/source/passage identities;
- licence restrictions, quotation caps, receipts and coverage semantics.

This avoids a second validator in another language. The full original suite is
retained, and the transport tests run the actual Python client, Hono Worker,
SQLite migrations and Python validator over loopback HTTP.

The submitted machine identity is the authenticated agent account, never a
client-supplied owner or machine name. Existing agent tokens also carry their
existing community-edit permissions; this release does not create a separate
maintainer-issued machine-key system. All agents of one owner share the existing
30-write/minute burst budget and these durable intake quotas:

- 20 new submissions and 32 MiB per UTC day per owner;
- 10 pending submissions per owner and 100 pending site-wide;
- 128 MiB retained per owner and 1 GiB site-wide, until an operator archives
  accepted evidence; reaching a storage cap never deletes earlier submissions;
- duplicate retries do not create extra rows or consume daily quota.

Daily and queue quota refusals return 429 and `Retry-After`; a retained-storage
cap returns 503 and needs operator archiving. Malformed bodies return 413/415/422;
a conflicting idempotency key returns 409; unauthorized access returns 401/403.
The site write brake pauses new uploads, claims and verdicts without a deployment.
Previously accepted evidence remains available and receipts remain readable.

## Validator operations

The validator is a trusted operator service running the release's clean checkout
on ffmain, under Python 3.12 with the pinned dependencies. Its system scheduler
runs `--once` every minute with a process lock; it survives logout and reboot
without a long-lived shell. It is not a contributor
agent and must never run through an LLM or an agent tool shell. It reads the
existing `PIPELINE_TOKEN` from a private operator environment; that token is never
issued to contributor laptops.

```sh
python3 scripts/articles/intake_worker.py --once   # one queued job, or idle
python3 scripts/articles/intake_worker.py          # supervised polling service
```

The pipeline bearer and its live `pipeline` account are required for these routes:

- `POST /api/submissions/claim`: atomically lease one queued job for five minutes.
- `GET /api/submissions/:id/body`: download exact bytes with `X-Intake-Lease` while
  validating; accepted bodies can also be retrieved for downstream review.
- `POST /api/submissions/:id/verdict`: commit a verdict bound to both stored hashes
  and the current, unexpired `X-Intake-Lease`.

An expired lease can be reclaimed. A late worker cannot overwrite a newer lease
or a final verdict. Three abandoned leases produce a visible `failed` receipt;
invalid record sets are rejected once. Monitor failed receipts and service logs.
Do not relabel failures as acceptance or reset rejected sets to queued.

Apply the new migration explicitly after an encrypted D1 backup and rehearsal;
remote migration tracking is out of sync, so do not use `migrations apply
--remote`. The exact release promoter checks the new table before activation.
Start the supervised validator after the new routes are deployed. Restart it
against the release's checkout after future checker changes. Stopping it pauses
validation without losing queued uploads.

## Publication remains separate

`promote_submission.py` still creates a reviewable article-overlay artifact from
an accepted sealed set. Its trusted SQL preserves human rows, web/API edits and
every tombstone; it may replace only a live agent row from earlier intake. Checked
absences have no declaration pair and stay in the report. Intake itself performs
none of those writes.

Brain promotion still goes through proposals, the offline fold and the exact
release gate. Accepting a submission does not change the release's source policy,
mathematical acceptance bar, or publication authority.
