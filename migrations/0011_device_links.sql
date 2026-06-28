-- 0011_device_links.sql — graph topology (backup lines) + the on-backup badge.
--
-- Phase 9 Part A. The tool still pings every device's management IP directly; topology
-- is pure inference (alert suppression + blast-radius attribution). Until now that
-- inference used a SINGLE parent (devices.parent_device_id). A tower/relay with a backup
-- uplink breaks that: when the primary path dies but a backup carries traffic, the node
-- is still genuinely reachable — declaring its children UNREACHABLE would be a lie, and
-- "running on backup" is itself the most valuable signal the tool couldn't raise.
--
-- Model (see CLAUDE.md §"Graph topology"): keep `devices.parent_device_id` as the
-- denormalized PRIMARY parent (every existing tree/topo query keeps working unchanged);
-- this table carries only the *extra* redundancy edges (kind='backup'). The engine
-- combines the two: primary = parent_device_id, backups = device_links. So there is NO
-- primary backfill here — the primary already lives on the device row, and duplicating it
-- would just create rows nothing reads.
--
-- `kind` keeps room for future edge types; `is_active` lets an edge be parked without a
-- delete. UNIQUE(child_id, parent_id) makes a backup edge idempotent.
--
-- FK discipline (CLAUDE.md): device_links REFERENCES devices(id) in BOTH columns, so
-- delete_device() must clear rows where the device is child_id OR parent_id before the
-- device row, exactly like the other devices-FK tables.
--
-- Idempotent via the runner: applied once, tracked in schema_migrations.

CREATE TABLE IF NOT EXISTS device_links (
    id         INTEGER PRIMARY KEY,
    child_id   INTEGER NOT NULL REFERENCES devices(id),
    parent_id  INTEGER NOT NULL REFERENCES devices(id),
    kind       TEXT NOT NULL DEFAULT 'backup',   -- 'backup' (room for more later)
    is_active  INTEGER NOT NULL DEFAULT 1,
    UNIQUE(child_id, parent_id)
);
CREATE INDEX IF NOT EXISTS idx_device_links_child ON device_links(child_id);
CREATE INDEX IF NOT EXISTS idx_device_links_parent ON device_links(parent_id);

-- On-backup badge state (one row per redundancy-capable device), the soft-signal sidecar
-- mirroring device_perf: written every full cycle by the daemon's redundancy sweep, read
-- by the dashboard for the "on backup" badge. Restart-safe — the sweep reads prior
-- on_backup back from here so a restart mid-failover never re-pages. `primary_down_since`
-- shows how long the node has been running on its backup path.
CREATE TABLE IF NOT EXISTS device_redundancy (
    device_id          INTEGER PRIMARY KEY REFERENCES devices(id),
    on_backup          INTEGER NOT NULL DEFAULT 0,
    primary_down_since TEXT,
    updated_at         TEXT NOT NULL
);
