-- 0012_snmp_ports.sql — SNMP port status (Phase 9 Part B).
--
-- A second, more specific ingress alongside ICMP. Instead of inferring "Tower B is down"
-- from ping timeouts, a switch states directly "port Gi0/2 — the Tower B backhaul — is
-- down", sooner and with the physical cause attached. Scope is IF-MIB oper/admin status
-- ONLY (no CPU/mem/temp): a monitored uplink/infra port flipping oper=down while admin=up.
--
-- SNMP is per-device config. NOTE: snmp_community is the FIRST per-device credential in the
-- DB — until now the only DB secret was the dashboard PIN hash. Low sensitivity (read-only
-- v2c on a management VLAN), but a conscious change (see CLAUDE.md §"SNMP port status").
-- v2c only for now; snmp_version keeps room for '3' without implementing v3 auth/priv.
--
-- SQLite has no "ADD COLUMN IF NOT EXISTS", but the runner applies each file once
-- (tracked in schema_migrations), so the bare ALTERs are safe (same pattern as 0009/0007).

ALTER TABLE devices ADD COLUMN snmp_enabled   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE devices ADD COLUMN snmp_version   TEXT;     -- '2c' (room for '3')
ALTER TABLE devices ADD COLUMN snmp_community TEXT;
ALTER TABLE devices ADD COLUMN snmp_port      INTEGER NOT NULL DEFAULT 161;

-- One row per discovered switch port. Discovery walks the ifTable once and lists every
-- port; the operator ticks which to `monitored` (you do NOT want to alarm on every access
-- port a laptop comes and goes on — only operator-flagged uplink/infra ports). A monitored
-- port that drops folds into the outage of the device it `feeds_device_id` (the bridge to
-- the graph), it does not raise a competing alarm.
--
-- `down_streak`/`alarm`/`alarm_since` carry the flap-suppressed detection state in-row so
-- it survives a daemon restart (no in-memory port FSM to lose). FK discipline: BOTH
-- device_id and feeds_device_id REFERENCE devices(id), so delete_device() clears
-- switch_ports where the device is either before the device row.
CREATE TABLE IF NOT EXISTS switch_ports (
    id              INTEGER PRIMARY KEY,
    device_id       INTEGER NOT NULL REFERENCES devices(id),
    if_index        INTEGER NOT NULL,
    if_name         TEXT,
    if_alias        TEXT,                              -- operator's label ("-> Rampur backhaul")
    admin_status    TEXT,                              -- 'up'|'down'|...
    oper_status     TEXT,                              -- 'up'|'down'|'lowerLayerDown'|...
    last_change     TEXT,                              -- ifLastChange (sysUpTime ticks), raw
    monitored       INTEGER NOT NULL DEFAULT 0,        -- only flagged ports can alarm
    feeds_device_id INTEGER REFERENCES devices(id),    -- the downstream node this port feeds
    down_streak     INTEGER NOT NULL DEFAULT 0,        -- consecutive down walks (flap suppression)
    alarm           INTEGER NOT NULL DEFAULT 0,        -- confirmed down (post flap-suppression)
    alarm_since     TEXT,
    updated_at      TEXT,
    UNIQUE(device_id, if_index)
);
CREATE INDEX IF NOT EXISTS idx_switch_ports_device ON switch_ports(device_id);
CREATE INDEX IF NOT EXISTS idx_switch_ports_feeds ON switch_ports(feeds_device_id);
