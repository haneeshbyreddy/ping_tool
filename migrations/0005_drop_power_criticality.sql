-- 0005_drop_power_criticality.sql — remove the power-vs-link cause inference and
-- per-device criticality features entirely.
--
-- `devices.power_ref_ip` was the sole input to the engine's automatic power-vs-link
-- cause guess (`outages.inferred_cause`); neither was ever settable from the UI, so
-- both are removed along with `devices.criticality` (which only ever held its default).
-- The operator-entered post-mortem (`outages.root_cause` / `resolution_notes`) is kept.
--
-- None of these columns are indexed or referenced by FKs, so plain DROP COLUMN is safe
-- (SQLite 3.35+). Applied once and recorded in schema_migrations; forward-only.

ALTER TABLE devices DROP COLUMN power_ref_ip;
ALTER TABLE devices DROP COLUMN criticality;
ALTER TABLE outages DROP COLUMN inferred_cause;
