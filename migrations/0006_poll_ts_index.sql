-- Index poll_results by timestamp alone so the daemon's retention sweep
-- (DELETE FROM poll_results WHERE timestamp < cutoff) and the heatmap's
-- MIN(timestamp) are index-driven rather than full table scans once the table
-- grows large under 24/7 operation. The existing idx_poll_device_ts(device_id,
-- timestamp) can't serve a bare timestamp predicate (wrong leading column).
CREATE INDEX IF NOT EXISTS idx_poll_ts ON poll_results(timestamp);
