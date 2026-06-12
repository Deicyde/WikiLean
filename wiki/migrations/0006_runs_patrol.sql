-- P2a instrumentation: pipeline-run registry + revision patrol marks. Two
-- parts in one migration:
--   1. pipeline_runs: one row per moderation-pipeline invocation, reported by
--      the runner via POST /api/runs (bot bearer; RUNS-API contract). run_id
--      is the runner's 8-hex id — also stamped into revisions.meta, which is
--      how export/stats join revisions back to their run. Feeds the /stats
--      cost/token summaries (RQ7) and the nightly research export.
--   2. revisions: patrolled_by/patrolled_at — the human patrol mark for
--      kind='edit' revisions (POST /api/revision/:id/patrol, patroller/admin;
--      NULL patrolled_by = awaiting patrol).
-- App-table timestamps are integer milliseconds (0001 convention).
-- D1/SQLite: one ADD COLUMN per ALTER; NOT NULL adds require a DEFAULT.

-- 1. pipeline_runs -------------------------------------------------------------
-- kind mirrors the runner's subcommands (moderate.py new|review|wp-update|all).
-- started_at/finished_at are runner-reported ms; created_at is server receipt.
-- cost_usd_equiv NULL = unknown (subscription-auth runs have no $ figure).
CREATE TABLE pipeline_runs (
  run_id             TEXT PRIMARY KEY,
  kind               TEXT NOT NULL CHECK(kind IN ('review','wp-update','new','all')),
  model              TEXT,
  prompt_sha         TEXT,
  started_at         INTEGER NOT NULL,
  finished_at        INTEGER NOT NULL,
  articles_processed INTEGER NOT NULL DEFAULT 0,
  errors             INTEGER NOT NULL DEFAULT 0,
  tokens             INTEGER NOT NULL DEFAULT 0,
  cost_usd_equiv     REAL,
  notes              TEXT,
  created_at         INTEGER NOT NULL
);

-- 2. revisions: patrol columns -------------------------------------------------
ALTER TABLE revisions ADD COLUMN patrolled_by TEXT;
ALTER TABLE revisions ADD COLUMN patrolled_at INTEGER;
