-- 0007_jitter.sql — store per-poll jitter alongside latency/loss.
-- Jitter (mean variation between consecutive RTTs) is the early-warning signal for a
-- wireless link going bad: a normally-8ms backhaul now at 90ms with high jitter is a
-- degraded link even while it still pings "up". This column feeds the per-link
-- baseline-deviation detector (see core/baseline.py).
-- Idempotent via the runner: it applies each file once (tracked in schema_migrations).
-- SQLite has no "ADD COLUMN IF NOT EXISTS", but 0007 never re-runs, so the bare ALTER
-- is safe (same pattern as 0002_postmortem.sql).

ALTER TABLE poll_results ADD COLUMN jitter_ms REAL;
