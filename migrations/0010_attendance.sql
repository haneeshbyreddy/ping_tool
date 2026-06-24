-- 0010_attendance.sql — daily operator attendance (who showed up, by date).
--
-- A row means "this operator was present on this UTC calendar day". Absence is the
-- absence of a row: the daily present-toggle DELETEs the row to mark not-present, so
-- the table only ever holds positive presence marks (no present=0 tombstones to skip
-- when counting). UNIQUE(worker_id, day) makes marking idempotent (INSERT OR IGNORE).
--
-- worker_id REFERENCES workers(id): with foreign_keys=ON, delete_worker() must clear a
-- worker's attendance rows BEFORE deleting the worker row — same rule the devices-FK
-- tables follow in delete_device(). Attendance is tracked for role='operator' only
-- (enforced in services.set_attendance), but no DB-level role check: a role change
-- shouldn't orphan-fail an INSERT.
--
-- Idempotent via the runner: applied once, tracked in schema_migrations.

CREATE TABLE IF NOT EXISTS attendance (
    id          INTEGER PRIMARY KEY,
    worker_id   INTEGER NOT NULL REFERENCES workers(id),
    day         TEXT NOT NULL,                          -- 'YYYY-MM-DD' (UTC)
    marked_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(worker_id, day)
);
CREATE INDEX IF NOT EXISTS idx_attendance_day ON attendance(day);
