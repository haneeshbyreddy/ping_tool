-- 0002_postmortem.sql — human-confirmed post-mortem fields on outages.
-- `inferred_cause` is what the engine guessed (power vs link); these two columns
-- are what the operator/technician confirms after the fact, surfaced by the
-- dashboard's "Pending post-mortem" triage card.
-- Idempotent: SQLite lacks "ADD COLUMN IF NOT EXISTS", so the runner only applies
-- this file once (tracked in schema_migrations) — adding the columns unconditionally
-- here is safe because 0002 never re-runs.

ALTER TABLE outages ADD COLUMN root_cause TEXT;
ALTER TABLE outages ADD COLUMN resolution_notes TEXT;
