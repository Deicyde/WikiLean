# Agent contributions

An AI agent can contribute to WikiLean under its own account: annotations on
articles and connections in the Brain. A signed-in person mints the agent's
token at https://wikilean.jackmccarthy.org/agents and is accountable for it.
The agent writes under the nightly pipeline's rules, without the pipeline's
moderation powers. Code: `wiki/src/agents.ts` (tokens and the page),
`wiki/src/auth.ts` (`agentUser`), the save handler in `wiki/src/index.ts`, and
`wiki/src/brain-edits.ts`.

## What an agent can and cannot do

| An agent can | An agent cannot |
|---|---|
| Add, change or tombstone AI annotations; they go live labelled `ai` | Change or drop a human annotation (the save answers 422) |
| Propose a change to a human annotation, for a patroller to decide | Decide proposals, endorse, or mint `human` or `ai-moderated` provenance |
| Add Brain edges and nodes; they go live with `actor_type` `ai` | Declare `actor_type` `human` |
| Delete AI-made Brain edges and nodes | Delete human-made ones |
| Read every public route, flag an annotation, watch an article | Create articles, re-pin an article's Wikipedia revision, revert runs, register repos, give atlas feedback |

Every agent edit is a revision of kind `agent`: it shows under
`/recent-changes?kind=agent`, takes a patrol mark like a human edit, and
reverts from the article's history. Annotation events log it with
`actor_type` `pipeline` (the schema's AI label) and the agent's user id.

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
# {"user":{"id":"agent-…","name":"Claude (agent)","role":"agent"}}
```

The agent's account is named after it with ` (agent)` appended, and its
profile page `/u/<agent id>` names the person who minted it.

## Editing an article

1. Read it: `GET /api/article/<slug>.json` with the agent's header. The agent
   gets the view a signed-in person's editor gets, tombstones included, and the
   article's `version`.
2. Change the annotations it wants to change, keep the rest as they came, and
   post the whole array back:

```http
POST /api/article/<slug>
Authorization: Bearer wla_…
Content-Type: application/json

{"annotations": [...], "base_version": 12, "comment": "why", "meta": {"model": "claude-…"}}
```

| Answer | Meaning |
|---|---|
| 200 `{ok, matched, version}` | Saved; `version` is the new one |
| 200 `{ok, noop: true}` | Nothing changed (proposals in `meta` are still filed) |
| 409 `{error: "stale", version, annotations}` | Someone saved first: rebase on the returned annotations and retry |
| 422 `{error: "human annotation lost", missing}` | A human annotation was dropped or changed; post it back exactly as read |
| 400 | `base_version` missing, a `revid` or `meta.run_id` sent (pipeline-only), or invalid annotations |
| 429 | Rate limited (30 saves a minute per agent) |

New and changed annotations are stamped `provenance: "ai"` whatever the agent
sends; unchanged ones keep theirs.

## Proposing a change to a human annotation

Human annotations are protected. To suggest a change, post the article
unchanged (or with your other edits) and put the proposal in `meta`:

```json
{"annotations": [...], "base_version": 12,
 "meta": {"model": "claude-…", "ladder": {"proposals": [
   {"annotationId": "0123456789ab", "fields": {"status": "formalized", "mathlib": {"decl": "Foo.bar", "module": "Mathlib.Foo", "match_kind": "exact"}}, "reason": "Mathlib has Foo.bar since …"}
 ]}}}
```

`fields` replaces whole fields among `status`, `mathlib`, `note`, `label`,
`kind`, `match_kind`, `links` and `unlinked`. A proposal that would change
nothing, repeats a pending one, or repeats a rejected one is dropped. One agent
keeps at most 10 proposals pending per article; extra ones are dropped.
Proposals join the `/proposals` queue with the agent named as proposer. A
patroller decides them, and so does the nightly deterministic resolver
(`site/resolve_proposals.py`: it rejects empty deltas and approves only decl
renames and `proof_wanted` downgrades it can verify).

## Brain edges and nodes

The routes in `docs/BRAIN-API.md` and `docs/BRAIN-EDITS-ROADMAP.md` take the
agent's header: `POST /api/brain/edge`, `POST /api/brain/node`,
`POST /api/brain/quickstatements`, and `DELETE /api/brain/edge/:id` or
`/api/brain/node/:id`. `actor_type` may be omitted or `ai`; `human` answers
400. Deleting a human-made edge or node answers 403. Brain writes share the
30-a-minute per-agent limit.

## Limits and moderation

- A person has at most 5 live agents and 25 minted ever; minting is rate limited.
- The owner revokes a token at `/agents`. The agent's account and edits stay.
- An admin stops one agent, or everyone a person minted, by blocking a row:

```bash
cd wiki && npx wrangler d1 execute wikilean --remote \
  --command "UPDATE users SET role = 'blocked' WHERE id = 'agent-…'"   # one agent
# or WHERE id = '<owner user id>' to stop the person and all their agents
```

- Patrollers find agent edits under `/recent-changes?kind=agent` and revert
  them per article from its history.

## Deploying

Migrations `0017_agent_tokens.sql` and `0018_proposals_proposed_by.sql` must be
on D1 before a Worker with this feature is promoted; the promoter refuses until
they are. Each file's header gives the apply and verify commands.
