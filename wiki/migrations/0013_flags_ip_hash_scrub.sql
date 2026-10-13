-- Scrub legacy flags.ip_hash values. Until this release ip_hash held an
-- unsalted sha256 hex of CF-Connecting-IP, which is reversible for IPv4 by
-- enumerating 2^32 addresses. The Worker now writes "hmac-sha256:<hex>" under
-- the IP_HASH_SECRET secret (or NULL without it). No code path uses ip_hash, so
-- the legacy values carry no function, only exposure; NULL them. HMAC rows are
-- kept (exact 12-char prefix match), so the statement is idempotent: run it
-- after the new Worker is live, and again after any rollback to an older
-- Worker version, since rows an old Worker writes stay unsalted.
--
-- This reaches only the live table. Copies taken before it runs (D1 Time
-- Travel history, `wrangler d1 export` backups and their artifacts) keep the
-- legacy values until they expire or are deleted.
--
-- NB deploy: remote migration tracking is out of sync — apply remote via
--   wrangler d1 execute wikilean --remote --file=migrations/0013_flags_ip_hash_scrub.sql
UPDATE flags SET ip_hash = NULL
WHERE ip_hash IS NOT NULL AND substr(ip_hash, 1, 12) <> 'hmac-sha256:';
