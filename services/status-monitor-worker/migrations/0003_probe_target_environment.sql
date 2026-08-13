-- Record which gateway each probe measured.
--
-- Until 2026-08-11 this Worker probed staging; #1252 repointed it at production
-- without giving the table anywhere to say so, leaving one column-less history
-- that blends both. Every read that reasons about health — the dashboard's
-- charts and, more importantly, the consecutive-failure count that decides
-- whether to page — was free to mix them.
ALTER TABLE probe_results ADD COLUMN target_environment TEXT;

-- Backfill is deterministic rather than inferred: the cutover left a clean
-- 12-hour hole in the data. The 03:44Z deploy repointed the URL while the
-- prober key still belonged to staging, so the gateway rejected every probe
-- account-wide until the key was replaced at 14:59Z. Nothing was written
-- between these two bounds, so no row has to be guessed at.
UPDATE probe_results
   SET target_environment = 'staging'
 WHERE target_environment IS NULL
   AND checked_at <= '2026-08-11T03:00:28.896Z';

UPDATE probe_results
   SET target_environment = 'production'
 WHERE target_environment IS NULL
   AND checked_at >= '2026-08-11T15:01:07.303Z';

-- Reads filter by (model_id, target_environment) and still want newest-first
-- within a model, so extend the existing access path rather than adding a
-- second one alongside it.
CREATE INDEX IF NOT EXISTS idx_probe_results_model_target
  ON probe_results (model_id, target_environment, id DESC);
