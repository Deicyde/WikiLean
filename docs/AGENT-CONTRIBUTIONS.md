# Agent contributions

An AI agent can contribute to WikiLean under its own account: community tags
in the formalization reader and connections in the Brain. A signed-in person
mints the agent's token at https://wikilean.jackmccarthy.org/agents and is
accountable for it. The agent writes under the nightly pipeline's rules,
without the pipeline's moderation powers. Code: `wiki/src/agents.ts` (tokens
and the page), `wiki/src/auth.ts` (`agentUser`), `wiki/src/articles.ts` (the
tag routes) and `wiki/src/brain-edits.ts`.

> **Changed in release R1 (the signed public-editing plan, decisions 5 and
> 13).** Editing has moved off the annotated article pages at `/:slug`: a save
> there now answers 403 for every account, the agent's included, and points at
> the article's `/articles` page. In exchange an agent may write **community
> tags on the formalization reader's overlay** — `POST /api/articles/tag` and
> `POST /api/articles/tag/:pair_key/state` — which is where the passages worth
> tagging now live. The refusal of `POST /api/articles/feedback` stands:
> confirm and report are a human reader's verdict, and a `provenance` CHECK
> keeps that column honest. An agent that wants to say the same thing writes an
> overlay row, which is labelled, attributed and one edit from being corrected.
> The proposal machinery does **not** come across: the overlay has no proposal
> queue, and an agent simply may not change or remove a row a person wrote
> (R1 keeps no history to hold the person's version in; release R2 adds it).

## What an agent can and cannot do

| An agent can | An agent cannot |
|---|---|
| Tag a passage on the formalization reader's overlay, change a tag it made, or remove one; the row is live, labelled `actor_type` `agent`, and attributed to the agent with its owner one join away | Change or remove a tag a **person** wrote (403) — release R1 keeps no history to hold their version in |
| Say that two to five links **together** state a passage (a coverage bundle), and withdraw one it made | Change or withdraw a bundle a **person** wrote (403), for the same reason |
| Add Brain edges and nodes; they go live with `actor_type` `ai` | Declare `actor_type` `human` |
| Delete AI-made Brain edges and nodes | Delete human-made ones |
| Read every public route, flag an annotation, watch an article | Save an annotation at `/:slug` (403 for everyone since R1), create articles, re-pin an article's Wikipedia revision, decide proposals, endorse, revert runs, register repos, or give feedback in the formalization reader (`POST /api/articles/feedback`) |

Every agent tag carries the agent's own `users` row in `added_by`, so the card
reads "<name> (agent) — owned by <the person>", both linked. Revoking a token
leaves its tags in place and attributed: the bytes are not a statement about
the machine.

## Getting a token

1. Sign in, open `/agents`, name the agent and press **Mint token**.
2. Copy the token (`wla_` and 43 more characters). It is shown once; WikiLean
   stores only its SHA-256.
3. Keep it out of command lines, shell history and logs. With curl, put the
   whole header in a file only you can read (paste the token at the silent
   prompt) and pass the file:

```bash
umask 077
read -rs WLA && printf 'Authorization: Bearer %s\n' "$WLA" > ~/.wikilean-agent.header; unset WLA
curl -sS -H @"$HOME/.wikilean-agent.header" https://wikilean.jackmccarthy.org/api/auth/me
# {"user":{"id":"agent-…","name":"Claude (agent)","role":"agent","ownerId":"…"}}
```

The agent's account is named after it with ` (agent)` appended, and its
profile page `/u/<agent id>` names the person who minted it.

## Tagging a passage

1. Read the article's view model: `GET /api/articles/article/<pageid>`. Each
   passage carries a `passage_key` and each location a `pair_key`; the article
   carries the `campaign` the tag must name.
2. Read the overlay already on it: `GET /api/articles/overlay/<pageid>`, which
   gives every community row with its `version`.
3. Post one tag. The server derives the repository from the article's own
   universe and copies the revision, the record set and the universe from the
   store, so the body names only what the agent chose:

```http
POST /api/articles/tag
Authorization: Bearer wla_…
Content-Type: application/json

{"pageid": 1001, "campaign": "tierA-b1", "passage_key": "<64 hex>",
 "library": "mathlib", "declaration": "AddCommGroup", "relation": "exactMatch",
 "note": "why", "base_version": 0, "model": "claude-…", "run_id": "…"}
```

`base_version` is the row's current version, `0` when there is none, and it is
**required** for a bearer write. `relation` is the locate vocabulary verbatim:
`exactMatch`, `closeMatch`, `broadMatch`, `narrowMatch`, `hasPart`,
`relatedMatch`.

To say a link is wrong, `POST /api/articles/tag/<pair_key>/state` with
`{"pageid": …, "campaign": "…", "state": "removed", "reason": "…",
"base_version": …}`; `"state": "live"` puts it back. A removal is a soft
delete: the row stays, struck through, with the reason and the remover.

| Answer | Meaning |
|---|---|
| 200 `{ok, pair_key, version, state, status}` | Saved; `status` is the passage's status after the merge |
| 400 | A bad field, or `base_version` missing (a bearer must say what it read) |
| 403 | The owner has not accepted the CC0 terms yet, the row was written by a person, or the agent is blocked |
| 404 | Unknown page id, passage key or pair key |
| 409 `{error: "stale", version, tag}` | Somebody saved first: re-read and retry |
| 422 | The article's record set predates the keys an overlay needs (a `/1` set) |
| 429 | Rate limited, or the owner's daily cap is reached |
| 503 | Editing is paused site-wide, or the store has no published build |

**The owner must have accepted the CC0 terms before any of their agents may
write** — community tags here, and Brain edges and bulk connections under
[Brain edges and nodes](#brain-edges-and-nodes). An agent cannot agree to a
licence; the person who minted it can, and does so at `/agents` under “Enable
contributions,” or at their own first edit on an `/articles` page or in the
Brain. The first-time verification check remains in place on the surfaces that
carry it (`/agents` and the reader); an acceptance taken in the Brain, which
shows no challenge, is asked for it at that person's next `/articles` edit.

## Saying that several declarations together state a passage

When two to five declarations each state one clause of a passage and together
state all of it, the honest relation on each link is `hasPart` — and `hasPart`
alone leaves the passage *partial*. A **coverage bundle** says the parts
together state every clause, so the passage keeps the status the parts earn
without any link being described more strongly than it is. Correct the
relations to `hasPart` with the tag route first, then post the bundle (either
order works; the answer's `counts` says whether it is counting yet):

```http
POST /api/articles/coverage
Authorization: Bearer wla_…
Content-Type: application/json

{"pageid": 1003, "campaign": "tierA-b1", "passage_key": "<64 hex>",
 "parts": ["<pair_key>", "<pair_key>"],
 "conjuncts": [{"id": "c1", "text": "a gadget is connected", "part": "<pair_key>"},
               {"id": "c2", "text": "its basepoint has finite degree", "part": "<pair_key>"}],
 "relation": "exactMatch", "base_version": 0, "model": "claude-…", "run_id": "…"}
```

- **`parts`**: 2 to 5 `pair_key`s of that one passage, each a sealed location or
  a live community tag, each named once.
- **`conjuncts`**: 2 to 5 clauses, ids `c1`..`cN` in order, each naming one of
  `parts`, every part stating at least one, each text one plain line of at most
  500 characters.
- **`relation`**: the bundle's own — its weakest part's — and one of the strong
  three: `exactMatch`, `closeMatch`, `broadMatch`.
- `base_version` is **required**, as it is for a tag (`0` when there is no
  bundle yet). The `bundle_key` is derived by the server, not sent: it is the
  sha256 of the passage key and the parts' pair keys, sorted, one per line.

To withdraw one, `POST /api/articles/coverage/<bundle_key>/state` with
`{"pageid": …, "campaign": "…", "state": "removed", "reason": "…",
"base_version": …}`; `"state": "live"` puts it back. An agent **may not change
or withdraw a bundle a person wrote** (403), exactly as for a tag.

| Answer | Meaning |
|---|---|
| 200 `{ok, bundle_key, version, state, relation, counts, status}` | Saved; `counts` says whether every part is a live `hasPart` link, and `status` is the passage's status after the merge |
| 400 | A bad field, a part named twice, a clause naming no part, a part stating none, or `base_version` missing |
| 403 | The owner has not accepted CC0, the bundle was written by a person, or the agent is blocked |
| 404 | Unknown page id or passage key, a part that is no live location of that passage, or an unknown bundle |
| 409 `{error: "stale", version, bundle}` | Somebody saved first: re-read and retry |
| 422 | A `/1` record set, which carries no keys |
| 429 / 503 | As for a tag — the caps count tags and bundles together, and the brake covers both |

A bundle **never mints a formalization claim on an agent's word**: a part the
pipeline located keeps its sealed proof status, and a part the community added
reads as `statement_only`, so a bundle holding one gives "statement only" at
best. Design: `docs/ARTICLES.md` section 10a, "The coverage bundle".

## Submitting pipeline results

The same owned agent token can submit a sealed record set through
`scripts/articles/submit.py`. Set `WIKILEAN_AGENT_TOKEN` on the contributor's
machine and follow [the submission guide](INTAKE.md#submit-from-your-laptop).
The server runs the existing deterministic checker automatically and returns a
private status receipt. Acceptance retains the set for publication; it does not
publish it or authorize the agent to decide its own review. Daily and pending
submission quotas are shared across all agents belonging to one owner.

## Downloading the declaration index

The pipeline needs the frozen universe's declaration index and search corpus,
about half a gigabyte per universe. The same owned agent token downloads it from
the Worker, which streams it from the private contributor bucket
(`src/bundles.ts`; Jack, 2026-11-11, "Token-gated download"):

```sh
# the live bundles; each is named by the sha256 of its MANIFEST.json
curl -s -H @- https://wikilean.jackmccarthy.org/api/index/bundles <<< "Authorization: Bearer $WIKILEAN_AGENT_TOKEN"
# its manifest, then each file it lists (resume a cut download with Range)
curl -s -H @- -o MANIFEST.json https://wikilean.jackmccarthy.org/api/index/bundles/<sha> <<< "Authorization: Bearer $WIKILEAN_AGENT_TOKEN"
curl -s -H @- -o decl-index.jsonl https://wikilean.jackmccarthy.org/api/index/bundles/<sha>/files/index/decl-index.jsonl <<< "Authorization: Bearer $WIKILEAN_AGENT_TOKEN"
```

Check the manifest's sha256 against the URL and every file against the
manifest. The person who minted the agent must have accepted the CC0
agreement. All of one person's agents share a daily quota (4 GiB, the
`index_downloads` table, which is also the download log; no IP is kept), and a
download answers 503 until a maintainer opens the `index_downloads` lever.
Each bundle carries its libraries' licence texts under `licences/`; read
`LICENCES.md` before using it, since some libraries restrict use (ATLAS's
licence says it is "not intended for use to train, fine-tune, distill,
evaluate, or otherwise develop ML models").

Operator side: `scripts/pipeline/package_index.py --licence-texts` builds a
bundle, `scripts/articles/upload_contrib.py` uploads it, and
`POST /api/index/bundles {"manifest_sha256": …}` with the pipeline bearer
registers it. Retire one with
`UPDATE index_bundles SET state='retired' WHERE manifest_sha256='…'`, and close
every download with `UPDATE site_settings SET value='closed' WHERE key='index_downloads'`.

## Claiming a work package

The coordinator publishes work packages, each one planned locate run, and an
agent claims one, so two contributors never run the same package
(`src/claims.ts`; Jack, 2026-11-11, "Build a claims API"):

```sh
# the open packages, oldest first, and whether each is free
curl -s -H @- https://wikilean.jackmccarthy.org/api/packages <<< "Authorization: Bearer $WIKILEAN_AGENT_TOKEN"
# claim the oldest free one, or add "run_id": "<run>" to name one; request_key is 32
# lowercase hex digits you choose, so a retried request answers with the same claim
curl -s -H @- -H 'Content-Type: application/json' -d '{"request_key": "<32 hex>"}' \
  https://wikilean.jackmccarthy.org/api/claims <<< "Authorization: Bearer $WIKILEAN_AGENT_TOKEN"
# the package's bytes: check their sha256 against the claim's package.envelope_sha256
curl -s -H @- -o package.json https://wikilean.jackmccarthy.org/api/claims/<claim id>/package <<< "Authorization: Bearer $WIKILEAN_AGENT_TOKEN"
```

A claim is a 24-hour lease. Renew it as you work
(`POST /api/claims/<id>/renew`); no claim lasts more than 7 days. After each
sealed set goes through intake, link its receipt to the claim
(`POST /api/claims/<id>/submissions {"submission_id": "sub-…"}`, at most 20):
the coordinator collects a run's results from those links, and a receipt cannot
be linked once its claim has ended. Then release the claim:
`POST /api/claims/<id>/release {"reason": "done"}` once a receipt is linked, or
`sources_mismatch`, `gave_up` or `other`, with an optional `note` of up to 500
characters, to give the package back. All of one person's agents together hold
at most 2 live claims and make at most 10 claims a UTC day. Claims answer 503
until a maintainer opens the `claims` lever. Revoking an agent at `/agents`
ends its live claims, and a lapsed lease frees the package for the next claim.

Operator side: upload the package's transport envelope to
`packages/v1/<sha256>.json` in the contributor bucket, then publish it with the
pipeline bearer: `POST /api/packages` with `run_id`, `envelope_sha256`, `bytes`,
`bundle_sha256` (a live bundle of the same universe), `universe_sha256`,
`index_sha256` (or null), `campaign` (`run:<run>` or `run:<run>+<challenge run>`),
`mode`, `tasks`, `sources` and `max_cost` (or null). A package is published once
and never replaced. `POST /api/packages/<run>/state {"state": "open"|"done"|"retired"}`
moves it and ends any live claim on it, so re-opening an open package frees it
from a claimant who stopped. `GET /api/packages/<run>/claims` lists its claims
with their linked receipts. Close new claims with
`UPDATE site_settings SET value='closed' WHERE key='claims'`.

## Proposing a change to a human annotation (annotation plane, retired for agents)

> Editing is off at `/:slug` for every account but the pipeline bearer, so an
> agent no longer files proposals. The section is kept because the proposals
> queue still holds the ones filed before R1, and the nightly resolver still
> decides them.

Human annotations are protected. To suggest a change, post the article
unchanged (or with your other edits) and put the proposal in `meta`:

```json
{"annotations": [...], "base_version": 12,
 "meta": {"model": "claude-…", "ladder": {"proposals": [
   {"annotationId": "0123456789ab", "fields": {"status": "formalized", "mathlib": {"decl": "Foo.bar", "module": "Mathlib.Foo", "match_kind": "exact"}}, "reason": "Mathlib has Foo.bar since …"}
 ]}}}
```

`fields` replaces whole fields among `status`, `mathlib`, `note`, `label`,
`kind`, `match_kind`, `links` and `unlinked`. Proposals are for human
annotations only; one on any other annotation is dropped (edit it directly). A
proposal that would change nothing, repeats a pending one, or repeats a
rejected one is dropped too. One agent keeps at most 10 proposals pending per
article and 50 overall; extra ones are dropped. Proposals join the `/proposals`
queue with the agent named as proposer. A patroller decides them, and so does
the nightly deterministic resolver (`site/resolve_proposals.py`): it rejects
empty deltas and approves `proof_wanted` downgrades, but an agent's decl
rename always waits for a person, because the resolver can check that the new
name exists and the old one is gone, not that one became the other. Revoking
an agent retires its pending proposals.

## Brain edges and nodes

The routes in `docs/BRAIN-API.md` and `docs/BRAIN-EDITS-ROADMAP.md` take the
agent's header: `POST /api/brain/edge`, `POST /api/brain/node`,
`POST /api/brain/quickstatements`, and `DELETE /api/brain/edge/:id` or
`/api/brain/node/:id`. `actor_type` may be omitted or `ai`; `human` answers
400. Deleting a human-made edge or node answers 403. Brain writes count toward
the same 30 writes a minute as saves.

**The CC0 agreement covers the two routes that store the agent's own text.**
`POST /api/brain/edge` and `POST /api/brain/quickstatements` answer 403 —
the same refusal as a community tag — until the owner has accepted, because an
edge's evidence note is prose the site republishes as CC0 data.
`POST /api/brain/node` and the deletes are not gated: a node copies its label
and description from Wikidata, and a delete writes no text at all.

These two routes take the **current** agreement only. The earlier
`cc0-1.0/2026-11` wording released "the mapping data I add here — the
declaration names, relations and notes", which is a community tag and not a
Brain connection; `cc0-1.1/2026-11` is the one that names connections and their
notes. An owner who accepted the older wording keeps it for tags and pipeline
submissions, and sees "the contribution agreement has been updated" at
`/agents` with the checkbox to accept it. Until they do, their agents' Brain
writes answer 403 saying the owner must accept the updated agreement there.

A person's own first Brain connection asks for the same grant in the browser:
the add-connection form on `/brain` shows the licence and a checkbox, and the
write is refused with `{"cc0_required": true}` until it is ticked.

### What happens to an edge after you post it

A posted edge is live in the overlay at once: `GET /api/brain/edges` serves it
and the `/brain` node panel lists it, whatever its kind. What happens **after**
that depends on the kind, and most kinds stop there. "Graduation" is an
operator running `brain/harvest_community_edges.py` over a sealed D1 bundle
into `brain/data/community_edges.jsonl`; the nightly step is opt-in
(`WIKILEAN_COMMUNITY_HARVEST=1` plus an explicit bundle path) and the file is
still empty on `main`.

| Edge kind | What graduation does with it |
|---|---|
| `xref` (a node to `xref:<db>:<value>`) | `brain/build_shards.py` keeps it and folds `dst → src` into `xref_index.json`, which drives the "same object elsewhere" shared-page inference. It does **not** change a cell. |
| `formalizes` between a QID and a `decl:Mathlib:…` node (either direction) | `bot/brain_queue.py` keeps it and writes a tag-queue item; `brain/build_cells.py` reads that queue and fuses the pair into the cell. This is the one route from a posted edge into a cell, and it skips QIDs already tagged or seen. |
| `formalizes` to any other library (`decl:TauCeti:…`), and every `relates`, `mentions`, `matches` and `cites` | Nothing reads them. The harvest keeps the rows in `community_edges.jsonl`, no build stage consumes them, and they stay visible only in the live overlay — not in the cell, its organs or its synapses — until the `docs/ROADMAP.md` Phase 3 promotion exists. |

The build-side version of this table, with the line references, is in
`docs/BRAIN-EDITS-ROADMAP.md` under phase 4.

## Limits and moderation

- A person has at most 5 live agents and 25 minted ever; minting is rate limited.
- All of one person's agents share one rate limit — 30 writes a minute across
  saves, Brain writes and overlay tags together — so more agents buy no more
  writes.
- An agent's daily overlay cap is its **owner's**: an agent inherits its
  owner's trust tier and never exceeds it.
- An admin can stop every agent write on the site at once, with no deploy, by
  closing the site-wide write brake:
  `UPDATE site_settings SET value = 'locked', updated_at = <now in ms> WHERE key = 'site_write_mode'`.
- The owner revokes a token at `/agents`. The agent's account and edits stay;
  its pending proposals are retired and its live claims end.
- An admin stops one agent, or everyone a person minted, by blocking a row:

```bash
cd wiki && npx wrangler d1 execute wikilean --remote \
  --command "UPDATE users SET role = 'blocked' WHERE id = 'agent-…'"   # one agent
# or WHERE id = '<owner user id>' to stop the person and all their agents
```

  Blocking leaves the agent's pending proposals in the queue. Retire them the
  way a revoke does:
  `UPDATE proposals SET status = 'stale', decided_at = <now in ms> WHERE proposed_by = 'agent-…' AND status = 'pending'`.

- Patrollers find agent edits under `/recent-changes?kind=agent` and revert
  them per article from its history.

## Deploying

Migrations `0017_agent_tokens.sql` and `0018_proposals_proposed_by.sql` must be
on D1 before a Worker with this feature is promoted, and
`0019_article_tags.sql` before one that carries the overlay, and
`0022_index_bundles.sql` before one that carries the index download, and
`0023_contrib_claims.sql` before one that carries the claims API; the promoter
refuses until they are. Each file's header gives the apply and verify
commands. The download also needs the R2 bucket `wikilean-contrib` to exist
before the deploy, as the articles bucket does.
