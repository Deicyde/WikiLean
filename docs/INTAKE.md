# Contributed record sets: the submission endpoint

**Status: design only, with its checks built.** No endpoint exists, no table exists, no key
has been issued, and nothing here is deployed. What *is* built is
`scripts/articles/verify_submission.py`, the offline statement of section 3's checks, so the
contract can be tested from this side before anything is written on either.

The formalization reader ([ARTICLES.md](ARTICLES.md)) takes sealed locate record sets from the
formal-companion skill's locate pipeline. Today an **operator copies** one from the machine
that produced it and runs `scripts/articles/build_store.py` by hand (ARTICLES.md section 2). That
is right while one person runs every locate run. It is a bottleneck as soon as several
machines produce sets a reviewer would accept as they stand — which is what formal-companion's
*contribute mode* makes possible: locate work on a machine no ssh reaches, run by somebody
who is not the maintainer.

This document specifies the **contract** for taking such a set over the wire: what a
contributor machine posts, what WikiLean must check before it accepts it, where an accepted
set goes, and what a promotion step would need from Jack before any of it reaches the Brain.
The two sides can then be built independently, and either can be tested against the contract
alone.

It is the WikiLean half only. Everything about *producing* a set — the planner, the work
repo, the sandbox, the gate, the challenge, the export — belongs to formal-companion and is
documented there; a contributor installs that skill, and WikiLean vendors no part of it.

## 1. What a machine posts

**One sealed record set, whole, in one request** — exactly what the producer's
`cf export literature-links` writes, and exactly what `scripts/articles/build_store.py` already
reads (ARTICLES.md section 3):

```
SEAL.json                     cf-locate-recordset/2: the pins and every file's sha256
universe.json                 the universe: libraries, repos, commits
sources.jsonl                 one row per source: identity, revision and shas
records/<source-id>.jsonl     Literature Links 2.0 records
receipts/<sha256>.json        the search receipts absence records name
```

- **`cf-locate-recordset/2`, either status rule.** `/2` is the record shape (`passage_key`,
  `pair_key`, a coverage claim as a bundle); the seal's `status_rule` (2 or 3) says which
  derivation its statuses came from. Intake accepts either rule it knows and **records it per
  set**: a status is only meaningful beside the rule that derived it, and a staging row that
  forgets the rule cannot be promoted later. A `/1` set is **refused** — it carries no
  `passage_key`, so nothing downstream can key a vote on it. (The store builder still takes
  `/1`; the operator path is unchanged.)
- **One set, one transaction.** Not a record at a time: a seal is a statement about a *set*,
  and a partial set is a set whose seal is false. A set too large for one request is a set
  that should have been exported per shard — the seal's `campaign` token already names shards
  (`run:<run>[+<challenge run>]`).
- **No article text beyond the cap.** The export holds every quote to the cap and quotes
  nothing from a closed licence class. Intake re-checks it rather than trusting it (section 3).

### 1.1 The envelope

One `POST` of a `multipart/form-data` or `application/zip` body (open question 1), plus these
headers, which are the only things outside the set:

| header | meaning |
|---|---|
| `Authorization` | the machine's scoped key (section 5) |
| `Idempotency-Key` | the seal's own sha256, so a retry is the same submission |
| `Content-Digest` | the sha256 of the body, checked before it is unpacked |

The idempotency key being the seal's sha is what makes a retried upload harmless: the set is
content-addressed, so "the same submission" is a decidable question and not a guess about
timing.

## 2. The verifier

`scripts/articles/verify_submission.py` is this document's section 3 as code, offline, with no
network, no D1 and no Worker binding. It is the normative statement of the checks: an endpoint
written in TypeScript re-implements them and is tested against this module's answers.

Every check that is about the **record set** rather than about the **submission** is delegated
to `scripts/articles/build_store.py`, so the two cannot drift — a set the store builder refuses
is a set intake refuses, by construction. What the verifier adds is the envelope, the `/1`
refusal and the key's scope.

It is also the command an operator or a contributor runs by hand until an endpoint exists:

```sh
python3 scripts/articles/verify_submission.py --record-set <dir> --machine-id <id>
```

Exit 0 and the staging rows on stdout, or exit 2 and the refusal's code, HTTP status and
reason on stderr. `--out <file>` writes the rows; `--campaign`, `--universe`,
`--max-submissions-per-day`, `--max-bytes-per-day` and `--max-body-bytes` give it a key's
scope; `--body`, `--content-digest` and `--idempotency-key` give it the envelope. Tests:
`scripts/articles/test_verify_submission.py`, in `scripts/ci-python.sh`.

## 3. What intake verifies before it accepts

Every check below is a **refusal with a reason**, never a warning, and none of them trusts the
producer. The code in brackets is the verifier's own, stable, and what an endpoint should
return in the body.

1. **The body's digest** matches `Content-Digest`, and the body is within the endpoint's
   maximum size. `[body_digest, 422]`, `[body_too_large, 413]`
2. **The seal is a seal.** `SEAL.json` parses, its `schema` is a record set WikiLean knows,
   and its keys are **exactly** the agreed set and no other. `[seal, 422]`
3. **Every file the seal names is there with its sha256, and nothing else is.** A file the
   seal does not name is a refusal, not an extra; so is a symlink. `[seal, 422]`
4. **The schema is `/2`**, and `Idempotency-Key` is the seal's own sha256.
   `[schema_not_accepted, 422]`, `[idempotency_key, 422]`
5. **The key's scope admits this set** (section 5): its campaign, its universe, and the
   machine's own quota. `[scope_campaign, 403]`, `[scope_universe, 403]`,
   `[quota_submissions, 429]`, `[quota_bytes, 429]`
6. **Every record passes the vendored Literature Links checker**
   (`wiki/src/vendor/literature-links/`, version 1.0.1), and its version is recorded per set.
   `[record_set, 422]`
7. **The identity pins resolve**: `universe.json`'s `universe_sha256`, the sourceset's
   `manifest_sha256`, and each source row's `revision` and `layer_sha256`. Intake stores them;
   it does **not** refetch the page. `[record_set, 422]`
8. **The licence and the quote cap.** Every quote is within the cap, carries its citation, and
   comes from a source whose licence class admits quoting. A set that quotes a closed source
   is refused whole — it is the one defect that cannot be repaired after acceptance.
   `[record_set, 422]`
9. **The skill commit is a commit**, 40 hex. It is not checked against a release: intake
   records what produced the set and lets promotion decide what it will accept. `[seal, 422]`

Checks 6, 7 and 8 are the store builder's, verbatim, which is also why its *whole* contract
applies: a record whose `span_sha256` disagrees with its passage's, a receipt no absence record
names, a universe whose libraries are not keyed by id — each is refused under `record_set`
with the builder's own message, which names the rule it breaks.

**Back-pressure.** A refusal for quota is a 429 and may be retried after the quota resets; a
refusal for a defect in the submission is a 422 or a 413 and **must never** be retried; a key
that may not post this set is a 403. The distinction belongs in the contract, because a
producer-side `submit` command would otherwise retry on the wrong one.

### What intake does not check

**Whether a location is right.** That is the challenge's job and the Brain's gate's, and an
intake that re-judged records would be a second judgment nobody reviewed. The verifier returns
no verdict field, and a staging record row carries five indexed columns and the payload — no
score, no decision.

## 4. Staging, never the live tables

An accepted set is written to a **staging table** and nowhere else:

```
submission(id, seal_sha256, set_id, campaign, universe_sha256, sourceset,
           status_rule, skill_commit, machine_id, received_at, state,
           checker_version, byte_budget, bytes)
submission_record(submission_id, source_id, passage_key, pair_key, relation,
                  status, payload)        -- the record as posted, verbatim
submission_file(submission_id, path, sha256, bytes)
```

`verify_submission.verify()` returns exactly these rows, minus the ids the database mints.

- **`state`** is `accepted` (section 3 passed), `rejected` (with the reason) or `promoted`.
  Nothing else writes it.
- **The payload is kept verbatim.** A staging row that re-encoded a record could not be shown
  to be the bytes the seal signed.
- **The live tables are untouched.** No annotation write, no item identity matching, no
  interaction with the nightly moderation pipeline — the same boundary the atlas plane already
  draws (ARTICLES.md section 1), and for the same reason: `articles.revid` advances only
  atomically with a re-anchored annotations payload, and `findLostHuman` 422 is the floor
  (CLAUDE.md, "Hard invariants"). An intake that wrote into the annotation schema would be a
  migration nobody asked for.
- **A superseding submission does not delete its predecessor.** Two sets of one passage from
  two machines are two rows, and which one a promotion takes is promotion's decision
  (section 6), recorded.

Whether these are D1 tables or something else is open (question 4). They are new tables, so
their migration would be a new numbered file in `wiki/migrations/`, idempotent
(`CREATE TABLE IF NOT EXISTS`), applied as an operator action after a backup, like 0015.
Nothing here proposes writing it yet.

## 5. One scoped key per machine

Each contributor machine gets **its own** key, scoped to this endpoint and nothing else:

- **Scope**: submit only. Not read of the live tables, not the atlas store, not the moderation
  API, not the Brain agent API.
- **Bound to** a machine id, and optionally a campaign token and a universe. A key that may
  submit only `run:loc-7*` sets for universe `u-2026-10` cannot be used to post anything else,
  so a leaked laptop key is a bounded loss.
- **Quota**: submissions per day and bytes per day, per key. The refusal names the quota; it
  does not silently drop.
- **Rotation**: issued by the maintainer, listed with its last use, revocable alone. Revoking
  a key leaves its accepted sets in staging — the sets are content-addressed, so revocation is
  a statement about the machine and not about the bytes.
- **Never in this repository, never in a package, never in a receipt.** A row goes in
  [SECRETS.md](SECRETS.md)'s register with the name, holder, scope, rotation and expiry, and
  **never a value** — that file's standing rule. The key lives in the contributor's own
  keychain and is read from their environment at the moment of the post.

This is the same promise CONTRIBUTING.md already makes about donated compute: WikiLean never
collects or stores a contributor's credentials, and the key WikiLean *issues* is scoped so
narrowly that losing it costs a quota and nothing else.

## 6. Promotion into the Brain, and what it needs from Jack

A set in staging is not in the Brain. **Promotion** would move accepted sets into the Brain
*through the nightly build and its reproducibility gate* — never around it:

1. A promotion run selects accepted submissions by (universe, campaign, status rule),
   deduplicates them by `pair_key` and `passage_key`, and writes `brain/proposals/*.jsonl` —
   the one path ARTICLES.md section 13 already admits without a gate-mode decision.
2. The nightly build folds those proposals offline (`brain/fold_proposals.py`, offline-only).
3. The reproducibility gate re-derives the fold's output and refuses a build that does not
   reproduce.

**Step 1 is where this stops, because the rest is Jack's open "Brain v4 release path"
decision** (ROADMAP.md, "Waiting on Jack"). What it needs from him, specifically:

- **Which gate mode admits a content change.** ARTICLES.md section 13 records that the release
  gate accepts only *provenance* changes today. A locate location is a content edge, not a
  provenance one.
- **Whether a contributed set may be a first-class input at all**, or only ever a proposal a
  human promotes. The difference decides whether promotion is a scheduled job or a review
  queue.
- **The reproducibility gate's input set.** If the gate re-derives from the staged bytes, the
  staging table is part of the build's reproducible input and must be snapshotted per build;
  if it re-derives from the record sets as files, the sets must be archived immutably. One of
  the two, named.
- **What supersedes what.** Two universes, two revisions of one page, and a re-cut of one
  passage all produce sets about the same mathematics. ARTICLES.md section 12's import rule
  ("supersede older-universe locate links on AI items only; never set legacy items
  `rejected`") is written for the D1 importer; promotion needs the same rule stated for the
  Brain, or a statement that it is the same.
- **Who is accountable for an accepted set.** A machine's key names a machine, not a person.
  If a promoted edge is wrong, the seal names the campaign, the universe and the skill commit,
  and the submission names the machine — but not an owner. Either that is enough, or a key
  must be bound to a person.
- **The acceptance bar.** Whether promotion requires a challenge run (`run:<run>+<challenge
  run>` in the campaign token). A set exported from an unchallenged run is a legitimate `/2`
  set, and whether it may be promoted is a policy question, not a format one.

Until those are answered, promotion stops at `brain/proposals/` and the offline fold, which
needs no decision, and that is the shape this document assumes.

### 6.1 Promotion into the articles overlay, which needs none of the above

**None of those six questions is a question for the articles overlay**, and that is the
one useful thing to say here. The overlay is not the Brain: it is mutable D1 under a
reader, it has history and revert, and it never enters a frozen release. So an accepted
set can become ordinary community edits without touching the Brain's release path, and
without pre-empting any of Jack's six answers.

`scripts/articles/promote_submission.py` is that step, and it is **offline and
release-neutral**. It verifies a set through section 3's contract again, turns its
located pairs into overlay rows, and writes them to a `.sql` file, a `.json` file, or
both — **for review**. It writes nothing to D1, holds no binding and touches no network;
applying the file is a separate, deliberate operator action after a backup.

```sh
python3 scripts/articles/verify_submission.py --record-set <dir> --machine-id <id>
python3 scripts/articles/promote_submission.py --record-set <dir> \
        --added-by <user-id> --created-at <ISO-Z> [--trusted] \
        --sql rows.sql --json rows.json
```

Three rules it enforces rather than documents:

- **one row per `pair_key`.** The key is revision- and commit-free, so a contributed
  location collides by construction with the pipeline's own on the same (passage,
  repository, declaration). Two sets about one passage produce one row and a second
  attribution, never two rows that can disagree — which is what question 4 above is
  shaped to avoid. A single set that states two different judgements for one pair is
  refused, and both are named.
- **a human's edit is never overwritten.** The generated statement is an
  `ON CONFLICT(pair_key) DO UPDATE … WHERE origin IN ('contributed','pipeline')`, so a
  contributed set may supersede a contributed or a pipeline row and nothing else. That
  is `findLostHuman`'s principle restated for this plane, and it is in the artefact
  rather than in a convention.
- **what it cannot know, it leaves NULL.** What a community row overrides (`base_relation`)
  is read from the live store at write time and needs D1; the removal columns are for a
  removal, which this is not.

A checked absence is not an overlay row — there is no declaration to key one on — so it
is counted and named in the report rather than dropped silently. The overlay's table and
column names are the public-editing plan's section 2.9 (migration 0019, release R1),
which is **not written yet**: the promoter's `COLUMNS` list is the assumption, and
`scripts/articles/test_promote_submission.py` makes it executable by running the
generated SQL against a table built from that same list.

## 7. The contributor's side

A contributor runs formal-companion; WikiLean's side of their work is one posted set.
**formal-companion is a dependency they install, never code this repository vendors.**

1. Install the skill and its substrate on their own machine, per its own README and its
   `docs/contribute.md` (contribute mode: a Mac or any box `cf run` cannot reach, logged in
   to Claude Code, with its sandbox enabled).
2. Take a planned package from the coordinator and run it. The package is a plan; the
   coordinator still decides what is planned.
3. Export the result as a sealed `/2` record set.
4. Check it before posting it: `python3 scripts/articles/verify_submission.py --record-set <dir>
   --machine-id <id>` against a WikiLean checkout. This is the point of shipping the verifier
   rather than only the prose — a contributor sees a refusal before the endpoint is the first
   place a defect is found.
5. Post it, once the endpoint exists. Until then the set reaches WikiLean the way every set
   does today: the operator copies it and builds the store (ARTICLES.md section 6).

The producer side needs two things it does not have, and they are formal-companion's to build,
not WikiLean's: a `submit` command that posts a set and records the answer, and a local
`--verify` on its export so a defect is caught before the upload. Both are listed for that
repository, not here.

## 8. What this does not do

- It does not write to any live table, ever (section 4).
- It does not judge a location (section 3).
- It does not accept a record at a time, a `/1` set, or a set whose seal it cannot verify
  (sections 1 and 3).
- It does not refetch a page (section 3, check 7).
- It does not decide the Brain's release path (section 6).
- It does not give a contributor machine any read of WikiLean (section 5).
- It does not vendor, fork or pin any part of formal-companion (section 7).

## 9. Open questions

1. **Transport shape.** One zip, or a multipart body with the files named? The seal already
   fixes the names, so either works; WikiLean's own stack decides. The verifier takes an
   unpacked directory and is indifferent.
2. **Size.** The endpoint needs a maximum body size, and a statement of what a producer should
   shard at. The verifier takes it as `--max-body-bytes`; nothing picks the number.
3. **Where staging lives.** D1, R2 with an index in D1, or somewhere else. A sealed set is
   mostly immutable bytes, which is R2's shape, and the indexed columns are a query, which is
   D1's. The row budget matters: at full scale a batch is tens of thousands of records.
4. **The atlas plane and the staging table.** The store (ARTICLES.md section 4) is keyed by page
   id and revision and built from the same bytes. Is the staging table the store's source, or
   a second copy? One of the two — two copies that can disagree is the defect this whole
   design is shaped to avoid.
5. **Whether to build it at all yet.** ROADMAP.md's P3 gates the
   "jobs/contributions/api_tokens queue + validation gauntlet + trust ladder" on a written
   trigger: *first real compute donor asks*. This endpoint is the record-set-shaped version of
   that item and should probably wait on the same trigger. The verifier is useful either way:
   it is the operator's own check on a set before it is built into the store.
