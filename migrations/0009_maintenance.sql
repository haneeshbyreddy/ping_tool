-- 0009_maintenance.sql — per-node maintenance flag.
-- Putting a node in maintenance fully pauses its monitoring: the daemon stops
-- pinging it (load_device_meta filters maintenance=1 out of the active set, so the
-- in-process engine rebuild drops it) and therefore pages no one for it while the
-- flag is set. It still exists in the inventory; flip the flag back to resume.
-- Idempotent via the runner: it applies each file once (tracked in schema_migrations).
-- SQLite has no "ADD COLUMN IF NOT EXISTS", but 0009 never re-runs, so the bare ALTER
-- is safe (same pattern as 0007_jitter.sql).

ALTER TABLE devices ADD COLUMN maintenance INTEGER NOT NULL DEFAULT 0;
