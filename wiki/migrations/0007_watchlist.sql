-- Per-user article watchlist: drives the "Watch" toggle in the editor bar and
-- the "watchlist" filter on /recent-changes. (user_id, slug) is the natural
-- key — no autoincrement id needed; deletes happen by composite key.

CREATE TABLE watchlist (
  user_id    TEXT NOT NULL,
  slug       TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, slug)
);

-- Index for "show me my watchlist, newest first" and the recent-changes filter.
CREATE INDEX idx_watchlist_user ON watchlist (user_id, created_at);
