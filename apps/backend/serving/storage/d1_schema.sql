-- Cloudflare D1 schema for operational tables.
--
-- SQLite dialect translation of the 6 PostgreSQL operational tables.
-- Run via D1OperationalStore.initialize() or manually via Cloudflare dashboard.
--
-- Tables: users, api_keys, auth_sessions,
--         email_verification_tokens, password_reset_tokens, admin_audit_log
--
-- Also: api_logs (slim rows for D1LogStore, no prompt/response content)

-- -------------------------------------------------------------------
-- users
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                TEXT PRIMARY KEY,
    email             TEXT NOT NULL UNIQUE,
    password_hash     TEXT NOT NULL,
    user_name         TEXT,
    preferences       TEXT NOT NULL DEFAULT '{}',
    role              TEXT NOT NULL DEFAULT 'free'
                      CHECK (role IN ('free', 'pro', 'internal', 'admin')),
    email_verified    INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'active'
                      CHECK (status IN ('active', 'suspended', 'deleted',
                                        'pending_approval', 'rejected')),
    approval_note     TEXT,
    reviewed_at       TEXT,
    reviewed_by       TEXT,
    created_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_login_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_pending_approval ON users(created_at DESC) WHERE status = 'pending_approval';
CREATE INDEX IF NOT EXISTS idx_users_last_login_at ON users(last_login_at DESC);

-- -------------------------------------------------------------------
-- api_keys
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash                 TEXT NOT NULL UNIQUE,
    key_prefix               TEXT NOT NULL,
    user_id                  TEXT NOT NULL,
    user_name                TEXT,
    status                   TEXT NOT NULL DEFAULT 'active',
    quota_daily_cost_usd     REAL DEFAULT 1000.00,
    quota_monthly_cost_usd   REAL,
    created_at               TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at               TEXT,
    last_used_at             TEXT,
    notes                    TEXT,
    metadata                 TEXT,
    account_id               TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_status ON api_keys(status, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_prefix_unique ON api_keys(key_prefix);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_user_active ON api_keys(user_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_api_keys_account ON api_keys(account_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_account_active ON api_keys(account_id) WHERE status = 'active' AND account_id IS NOT NULL;

-- -------------------------------------------------------------------
-- auth_sessions
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auth_sessions (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    refresh_token_hash  TEXT NOT NULL UNIQUE,
    jti                 TEXT,
    sid                 TEXT NOT NULL,
    created_at          TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_used_at        TEXT,
    expires_at          TEXT NOT NULL,
    revoked             INTEGER DEFAULT 0,
    user_agent          TEXT,
    ip_address          TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_token ON auth_sessions(refresh_token_hash) WHERE NOT revoked;
CREATE INDEX IF NOT EXISTS idx_auth_sessions_jti ON auth_sessions(jti);

-- -------------------------------------------------------------------
-- email_verification_tokens
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS email_verification_tokens (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_email_verification_user ON email_verification_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_email_verification_expires ON email_verification_tokens(expires_at);

-- -------------------------------------------------------------------
-- password_reset_tokens
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_password_reset_user ON password_reset_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_password_reset_expires ON password_reset_tokens(expires_at);

-- -------------------------------------------------------------------
-- admin_audit_log
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    admin_ip         TEXT NOT NULL,
    action           TEXT NOT NULL,
    target_user_id   TEXT,
    details          TEXT,
    success          INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_timestamp ON admin_audit_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_user ON admin_audit_log(target_user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_action ON admin_audit_log(action, timestamp DESC);

-- -------------------------------------------------------------------
-- user_daily_cost (quota enforcement counters)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_daily_cost (
    user_id           TEXT NOT NULL,
    day               TEXT NOT NULL,
    cost_usd          REAL NOT NULL DEFAULT 0.0,
    requests          INTEGER NOT NULL DEFAULT 0,
    last_request_at   TEXT,
    PRIMARY KEY (user_id, day)
);

CREATE INDEX IF NOT EXISTS idx_user_daily_cost_day ON user_daily_cost(day);

-- -------------------------------------------------------------------
-- signup_allowed_domains (admin-editable signup approval policy)
-- Empty table means all signups auto-approve. With rows present,
-- only listed domains (exact or wildcard suffix) auto-approve and
-- non-listed signups go to pending_approval. See
-- serving/auth/signup_policy.py for match rules.
-- -------------------------------------------------------------------
-- ``created_by`` references users(id) but is informational only and is
-- left unconstrained on D1 (the schema rebuild is sufficient, there is
-- no migration runner). On Postgres it carries an ON DELETE SET NULL FK
-- so hard-deleting a user who created an entry does not fail.
CREATE TABLE IF NOT EXISTS signup_allowed_domains (
    domain      TEXT NOT NULL,
    is_wildcard INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by  TEXT,
    PRIMARY KEY (domain, is_wildcard)
);

-- -------------------------------------------------------------------
-- api_logs (slim rows — no prompt/response content)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_logs (
    request_id        TEXT PRIMARY KEY,
    timestamp         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    user_id           TEXT,
    model_id          TEXT NOT NULL,
    provider          TEXT NOT NULL,
    cost_usd          REAL,
    latency_ms        INTEGER,
    status_code       INTEGER,
    ttft_ms           INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    outcome           TEXT NOT NULL DEFAULT 'success'
);

CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp ON api_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_api_logs_user ON api_logs(user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_api_logs_model ON api_logs(model_id, provider, timestamp DESC);
