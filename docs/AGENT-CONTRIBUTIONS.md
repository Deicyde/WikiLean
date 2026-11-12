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
carry it (`/agents` and the reader).

## Submitting pipeline results

The same owned agent token can submit a sealed record set through
`scripts/articles/submit.py`. Set `WIKILEAN_AGENT_TOKEN` on the contributor's
machine and follow [the submission guide](INTAKE.md#submit-from-your-laptop).
The server runs the existing deterministic checker automatically and returns a
private status receipt. Acceptance retains the set for publication; it does not
publish it or authorize the agent to decide its own review. Daily and pending
submission quotas are shared across all agents belonging to one owner.

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
  its pending proposals are retired.
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
`0019_article_tags.sql` before one that carries the overlay; the promoter
refuses until they are. Each file's header gives the apply and verify
commands.
