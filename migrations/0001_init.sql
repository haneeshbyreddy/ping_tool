-- 0001_init.sql — base schema for the Village WISP Monitor.
-- Idempotent: every object uses IF NOT EXISTS so re-running is safe.
-- See plan.md §"Database" for the rationale behind each table.

PRAGMA foreign_keys = ON;

-- Shared infrastructure: towers, relays, backhaul links, core gateway.
-- A node's parent_device_id encodes topology so a dead parent suppresses
-- alerts for everything behind it.
CREATE TABLE IF NOT EXISTS devices (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL,          -- human friendly, e.g. 'Rampur Main Tower'
    ip_address           TEXT NOT NULL,
    device_type          TEXT,                   -- 'core'|'tower'|'relay'|'sector'|'backhaul'
    criticality          INTEGER NOT NULL DEFAULT 3,  -- 1..5; 5 = core gateway
    region               TEXT,                   -- village / area name
    is_active            INTEGER NOT NULL DEFAULT 1,
    parent_device_id     INTEGER REFERENCES devices(id),
    power_ref_ip         TEXT,                   -- node on the same MAINS power (power-vs-link)
    technician_phone     TEXT,                   -- region tech; ntfy/Telegram routing key
    customer_count       INTEGER NOT NULL DEFAULT 0,  -- customers behind this site (blast radius)
    base_revenue_impact  REAL NOT NULL DEFAULT 0       -- est. currency/hour while down
);

-- Raw poll samples. One row per device per 60s cycle.
CREATE TABLE IF NOT EXISTS poll_results (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES devices(id),
    timestamp   TEXT NOT NULL,               -- ISO8601 UTC
    latency_ms  REAL,                        -- NULL on 100% loss
    packet_loss REAL NOT NULL,               -- 0..100
    state       TEXT NOT NULL                -- state AFTER this poll was evaluated
);
CREATE INDEX IF NOT EXISTS idx_poll_device_ts ON poll_results(device_id, timestamp);

-- Confirmed outages (operational memory). resolved_at NULL == ongoing.
CREATE TABLE IF NOT EXISTS outages (
    id              INTEGER PRIMARY KEY,
    device_id       INTEGER NOT NULL REFERENCES devices(id),
    started_at      TEXT NOT NULL,
    resolved_at     TEXT,
    final_state     TEXT,                    -- 'DOWN' | 'UNREACHABLE'
    inferred_cause  TEXT,                    -- 'Likely Power Outage'|'Link/Equipment Fault'|NULL
    acknowledged_at TEXT,
    acknowledged_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_outage_device_start ON outages(device_id, started_at);

-- Every alert we attempt: audit trail + anti-spam window + restart safety.
CREATE TABLE IF NOT EXISTS alert_log (
    id         INTEGER PRIMARY KEY,
    outage_id  INTEGER REFERENCES outages(id),
    device_id  INTEGER REFERENCES devices(id),
    channel    TEXT NOT NULL,               -- 'ntfy'|'telegram'|'mock'
    recipient  TEXT NOT NULL,
    sent_at    TEXT NOT NULL,
    status     TEXT NOT NULL,               -- 'sent'|'failed'|'suppressed'
    payload    TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_recipient ON alert_log(recipient, device_id, sent_at);

-- DB-derived escalation timers so a restart never drops an escalation.
CREATE TABLE IF NOT EXISTS escalations (
    id          INTEGER PRIMARY KEY,
    outage_id   INTEGER NOT NULL REFERENCES outages(id),
    kind        TEXT NOT NULL,              -- 'realert' | 'escalate_to_owner'
    due_at      TEXT NOT NULL,
    executed_at TEXT,
    UNIQUE(outage_id, kind)
);

-- Reserved for the future customer-comms layer. Unused in v1, present so that
-- layer needs no migration later.
CREATE TABLE IF NOT EXISTS customer_mappings (
    id             INTEGER PRIMARY KEY,
    customer_phone TEXT NOT NULL,
    device_id      INTEGER REFERENCES devices(id),
    region         TEXT NOT NULL
);
