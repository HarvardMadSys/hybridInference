-- Key/value table for cron-cycle health (so a failed discovery/probe cycle is
-- surfaced instead of leaving stale green rows on the dashboard).
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
