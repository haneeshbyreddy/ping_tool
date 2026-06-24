-- Hourly rollups: compact per-device/hour aggregates folded from raw poll_results
-- by the daemon (core/rollup.roll_up). Raw poll samples are hot scratch — read only
-- for the *latest* state per device + a short forensic window — so their long
-- retention is pure cost. Trends (latency/loss/uptime over weeks) come from here
-- instead: one row per device per hour rather than one per poll. The `outages` table
-- stays the source of truth for incidents; this is only for charts/analytics.
CREATE TABLE IF NOT EXISTS poll_rollups (
    device_id      INTEGER NOT NULL REFERENCES devices(id),
    bucket         TEXT NOT NULL,             -- ISO8601 UTC hour, e.g. 2026-06-24T14:00:00+00:00
    samples        INTEGER NOT NULL,          -- raw polls folded into this hour
    latency_avg    REAL,                      -- NULL when every sample in the hour was 100% loss
    latency_min    REAL,
    latency_max    REAL,
    loss_avg       REAL NOT NULL,
    down_polls     INTEGER NOT NULL,          -- polls in DOWN/UNREACHABLE
    degraded_polls INTEGER NOT NULL,
    up_polls       INTEGER NOT NULL,
    PRIMARY KEY (device_id, bucket)           -- one row per device-hour; makes the fold idempotent
);
-- Time-range scans across devices, plus MAX(bucket) for the rollup watermark.
CREATE INDEX IF NOT EXISTS idx_rollup_bucket ON poll_rollups(bucket);
