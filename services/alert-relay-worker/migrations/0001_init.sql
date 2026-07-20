PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS alert_incidents (
  id TEXT PRIMARY KEY,
  environment TEXT NOT NULL CHECK (environment IN ('staging', 'production', 'local')),
  fingerprint TEXT NOT NULL,
  active_key TEXT UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('opening', 'firing', 'resolved')),
  alert_json TEXT NOT NULL,
  resolution_alert_json TEXT,
  occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  slack_channel_id TEXT NOT NULL,
  slack_thread_ts TEXT,
  codex_status TEXT NOT NULL DEFAULT 'investigating'
    CHECK (codex_status IN ('investigating', 'analysis_ready', 'unavailable', 'resolved')),
  analysis_ref TEXT,
  parent_dirty INTEGER NOT NULL DEFAULT 1 CHECK (parent_dirty IN (0, 1)),
  parent_version INTEGER NOT NULL DEFAULT 1 CHECK (parent_version > 0),
  parent_sync_token TEXT,
  parent_sync_expires_at INTEGER,
  recovery_pending INTEGER NOT NULL DEFAULT 0 CHECK (recovery_pending IN (0, 1)),
  recovery_message_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS alert_incidents_lookup
  ON alert_incidents(environment, fingerprint, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS alert_incidents_one_active
  ON alert_incidents(environment, fingerprint)
  WHERE status IN ('opening', 'firing');

CREATE TABLE IF NOT EXISTS alert_jobs (
  id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL REFERENCES alert_incidents(id) ON DELETE CASCADE,
  status TEXT NOT NULL
    CHECK (status IN (
      'waiting', 'queued', 'dispatching', 'dispatched', 'completing', 'completed', 'failed'
    )),
  analysis_ref TEXT,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error TEXT,
  completion_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS alert_jobs_ready ON alert_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS alert_jobs_incident ON alert_jobs(incident_id);

CREATE TABLE IF NOT EXISTS alert_receipts (
  alert_id TEXT PRIMARY KEY,
  incident_id TEXT REFERENCES alert_incidents(id) ON DELETE SET NULL,
  action TEXT NOT NULL
    CHECK (action IN ('opened', 'repeated', 'resolved', 'orphan_resolution')),
  received_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS alert_receipts_incident ON alert_receipts(incident_id);
