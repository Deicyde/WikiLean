# WikiLean handoff

Start with the current workflow, then check live state before changing anything.

1. [AGENTS.md](AGENTS.md) defines the repository invariants, commands, and ownership
   boundaries. `CLAUDE.md` carries the equivalent entry point for Claude.
2. [START-CONTRIBUTING.md](docs/START-CONTRIBUTING.md) explains what readers, agents,
   and external machines can do today. [CONTRIBUTING.md](CONTRIBUTING.md) covers
   development and the research notice.
3. Run `python3 manage/status.py` for the local control-plane snapshot;
   `--live` adds site and bot checks. Verify production identity at
   [/api/version](https://wikilean.jackmccarthy.org/api/version). Local worktrees,
   host memories, and old release notes may describe different states.
4. [ROADMAP.md](docs/ROADMAP.md) is the canonical plan. Check its decisions and
   latest status entries before starting new work.
5. [BRAIN-RELEASE-RUNBOOK.md](docs/BRAIN-RELEASE-RUNBOOK.md) owns deployment and
   rollback. [The November 8 record](docs/BRAIN-ACTIVATION-2026-11-08.md) documents
   the native Literature Links conversion and contributor activation. Read the
   [Brain handoff](docs/BRAIN-SQLITE-HANDOFF.md) for graph storage work.

D1 is canonical. Preserve human edits, existing private source packs, and release
and recovery evidence. An operator’s working tree can contain important uncommitted
corpus work; use a clean isolated checkout for code changes.

Nightly jobs and the bot may run on different hosts. Confirm recent outputs and
running processes before restarting a job or assuming it is active. Per-host agent
memory is useful context, not the source of truth.
