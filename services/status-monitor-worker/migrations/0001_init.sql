-- Probe results, one row per (model, cron cycle).
CREATE TABLE IF NOT EXISTS probe_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id TEXT NOT NULL,
  ok INTEGER NOT NULL,            -- 0/1
  latency_ms REAL,
  ttft_ms REAL,
  completion_tokens INTEGER,
  throughput_tps REAL,
  error TEXT,
  checked_at TEXT NOT NULL        -- ISO-8601 UTC
);

-- Fast "latest + recent history per model" lookups.
CREATE INDEX IF NOT EXISTS idx_probe_results_model_id ON probe_results (model_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_probe_results_checked_at ON probe_results (checked_at);
