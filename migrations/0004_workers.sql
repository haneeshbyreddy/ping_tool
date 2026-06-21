-- 0004_workers.sql — workers as first-class entities (the Team directory).
--
-- The engine/notifier routing key stays `devices.technician_phone` (string match to
-- workers.phone), so the alerting paths are untouched; the worker row *enriches* that
-- key with identity, channels, and role. A later migration can add a hard worker_id FK
-- to devices once the directory is the established source of truth.
--
-- Roles drive *routing*, not permissions (auth is the shared PIN, §8.2):
--   owner    → escalations (T+20) + daily digest
--   tech     → their region's device pages (existing technician_phone routing)
--   operator → dashboard user, no distinct routing yet
--
-- Idempotent: CREATE IF NOT EXISTS; the backfill INSERT...SELECT is guarded by a NOT
-- EXISTS check so re-running (or running on an already-populated table) is a no-op.

CREATE TABLE IF NOT EXISTS workers (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    role             TEXT NOT NULL DEFAULT 'tech',    -- 'owner' | 'operator' | 'tech'
    phone            TEXT,                            -- doubles as the device routing key
    ntfy_topic       TEXT,                            -- real ntfy routing
    region           TEXT,                            -- village/area covered
    is_active        INTEGER NOT NULL DEFAULT 1,
    notes            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_workers_role ON workers(role, is_active);

-- Backfill one 'tech' worker per distinct technician_phone already on devices, so the
-- Team page is populated on first run and technician_phone keeps resolving to a person.
INSERT INTO workers (name, role, phone, region)
SELECT COALESCE(d.region, 'Region') || ' tech', 'tech', d.technician_phone,
       MIN(d.region)
FROM devices d
WHERE d.technician_phone IS NOT NULL AND d.technician_phone <> ''
  AND NOT EXISTS (SELECT 1 FROM workers w WHERE w.phone = d.technician_phone)
GROUP BY d.technician_phone;
