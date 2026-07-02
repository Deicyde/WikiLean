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

3. **The memory system** — `~/.claude/projects/-Users-jack-Desktop-LEAN-WikiLean/memory/`.
   `MEMORY.md` is the index (loaded each session); the individual notes hold the evolving
   operational detail — pipeline internals, deploy setup, the `/review` tool, the Wikidata
   property proposal, the `manage/` control plane, and Jack's working preferences.

4. **`docs/ROADMAP.md`** — the canonical plan (phases, binding decisions, status log). Read
   before proposing what to build next. Companion: `docs/research-plan.md` (RQ1–RQ8).

**Why the split:** live state → `status.py`, evolving facts → memory, durable conventions →
`CLAUDE.md`, the plan → `ROADMAP.md`, and the operational loop runs itself (nightly launchd +
bot Actions). No single conversation or hand-maintained doc is load-bearing anymore.

*The pre-split deep version of this file is in git history (`git log -- HANDOFF.md`) if ever
needed.*
