# WikiLean — Agent Handoff

> This file used to carry the full architecture snapshot, which drifts. That content now
> lives in the four places below (which stay current on their own). This is just the map.

**Start here, in order:**

1. **`CLAUDE.md`** (repo root) — auto-loaded every session: what WikiLean is, where things
   live, the commands, and the **hard invariants** ("don't break"). Read it first; it's the
   durable contract every agent (and subagent) is expected to follow.

2. **`python3 manage/status.py`** — the **live** ground-truth snapshot (coverage, the Agent-2
   backlog, worklist depths, tag-pool runway, the current `@[wikidata]` batch, git state, and
   the decisions waiting on a human). A SessionStart hook runs this automatically, so every
   session opens from real current state instead of a stale doc. Add `--live` for site-200 +
   bot-gate checks.

3. **Evolving facts live in `docs/`**: the `docs/ROADMAP.md` status log and its "Waiting on
   Jack" list, `docs/BRAIN-ACTIVATION-2026-10-04.md` for production Brain state, and the
   Brain checkpoint named by `docs/BRAIN-SQLITE-HANDOFF.md`. Agent memory
   (`~/.claude/projects/<checkout-path>/memory/`) is per-host and may hold few or no notes on
   a given machine, so treat it as a cache, not a source of truth. Deploy setup is in
   `docs/BRAIN-RELEASE-RUNBOOK.md`; the Wikidata property is P14534
   (`docs/wikidata_property_proposal.md`).

4. **`docs/ROADMAP.md`** — the canonical plan (phases, binding decisions, status log). Read
   before proposing what to build next. For production Brain state read
   `docs/BRAIN-ACTIVATION-2026-10-04.md`; for the Brain/SQLite checkpoint history,
   `docs/BRAIN-SQLITE-HANDOFF.md`. Companion: `docs/research-plan.md` (RQ1–RQ8).

**Why the split:** live state → `status.py` and the live site, evolving facts → `docs/`,
durable conventions → `CLAUDE.md`, the plan → `ROADMAP.md`. The operational loop runs on
launchd on one operator Mac plus the bot's GitHub Actions. Confirm it is actually producing
work (the `/stats` pipeline-run rows, recent article history) rather than assuming it is.

*The pre-split deep version of this file is in git history (`git log -- HANDOFF.md`) if ever
needed.*
