-- 0003_settings.sql — typed key/value store for UI-editable operational config.
--
-- Phase 8 moves the source of truth for tunables out of frozen env-read config and
-- into the DB, so the operator can reconfigure from the browser (the two processes —
-- daemon + dashboard — share only this SQLite file). Values are stored as text and
-- coerced/range-checked on read against the typed schema in `config.py`
-- (SETTING_SCHEMA). The table starts empty, so an empty `settings` table ⇒ today's
-- env/default behavior exactly (fully backward compatible).
--
-- Bootstrap config (db_path, migrations_dir, busy_timeout, session secret) stays
-- env/file-only and is NOT stored here — you can't keep "where the DB is" inside the DB.
--
-- Idempotent (IF NOT EXISTS); the runner records this version in schema_migrations so
-- it is applied once. Never edit in place — add a later migration instead.

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,            -- e.g. 'poll_interval_s', 'ntfy_base_url', 'org_name'
    value      TEXT NOT NULL,               -- text; coerced on read per SETTING_SCHEMA
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by TEXT                         -- worker name / 'system'; light audit
);
