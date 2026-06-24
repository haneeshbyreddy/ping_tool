-- 0008_device_perf.sql — current per-link performance-baseline state.
-- One row per device: is it currently degraded vs its OWN rolling baseline, and by
-- how much. Written each cycle by the daemon's perf sweep (core/baseline.py decides),
-- read by the dashboard for the "degraded performance" badge.
-- This is a SOFT signal, deliberately separate from outages/escalation: a slow-but-up
-- link pages the operator once as a heads-up and never enters the all-hands ladder.
-- `since` lets the dashboard show how long the link has been degraded.

CREATE TABLE IF NOT EXISTS device_perf (
    device_id   INTEGER PRIMARY KEY REFERENCES devices(id),
    degraded    INTEGER NOT NULL DEFAULT 0,
    metric      TEXT,            -- 'latency' | 'jitter' | NULL
    baseline_ms REAL,
    current_ms  REAL,
    since       TEXT,            -- when the current degraded episode began (ISO8601)
    updated_at  TEXT NOT NULL
);
