-- 0015_outbox.sql — store-and-forward outbox for the central shipper (Phase 10 Part A).
--
-- The edge is, and stays, today's standalone monitor. When WISP_CENTRAL_URL is set, the
-- daemon ALSO enqueues shippable records here — inside the SAME transaction that writes
-- poll_results / applies outage events — and a background shipper thread drains them to the
-- central server over HTTPS, deleting each row only once central acks it. A WAN blip just
-- grows the queue; we never lose an outage record to a dropped socket (the same
-- DB-derived-durability discipline as the escalations table).
--
--   kind     — 'event' (outage open/recategorize/resolve, uplink up/down) or 'rollup'
--              (an hourly poll_rollups row). Heartbeats are LIVE (sent direct by the
--              shipper, never queued — a stale "I'm alive" is worthless), so they are
--              deliberately NOT an outbox kind.
--   payload  — the JSON body of the record (the wire envelope is wrapped at ship time).
--   attempts — bumped each failed ship, for backoff/observability; never blocks a record.
--   sent_at  — set on ack just before the row is deleted (belt-and-braces; the drain
--              deletes acked rows, so a lingering sent_at row is only a crash artifact).
--
-- Eviction (shipper, past a high-water mark) drops the OLDEST 'rollup' rows only — an
-- unsent 'event' is an outage record and is sacred, never evicted. `kind` is what lets the
-- eviction pick rollups; the partial index keeps the hot "next unsent batch" scan cheap as
-- the queue drains. WISP_CENTRAL_URL empty ⇒ nothing is ever written here (back-compat).
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,                     -- 'event' | 'rollup'
    payload    TEXT NOT NULL,                     -- JSON record body
    created_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    sent_at    TEXT
);

-- The shipper always scans "oldest unsent first"; a partial index on the unsent set keeps
-- that O(batch) even when a backlog of sent-but-not-yet-pruned rows would otherwise bloat it.
CREATE INDEX IF NOT EXISTS idx_outbox_unsent ON outbox(id) WHERE sent_at IS NULL;
