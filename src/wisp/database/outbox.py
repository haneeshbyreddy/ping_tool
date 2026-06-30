"""Store-and-forward outbox — durable DB glue between the edge and the central shipper.

This is the producer/queue side only: pure SQLite, no network. The daemon enqueues
records *inside its existing write transactions* (so an outage record is committed
atomically with the `poll_results`/`outages` rows that produced it — never half-written),
and the shipper (`egress/shipper.py`) drains them. Keeping this in `database/` lets both
`core/rollup` and the daemon enqueue without a backwards core→egress import.

Records are shaped at enqueue time into the small, stable dicts central stores; the wire
envelope (`v`, tenant, node, …) is wrapped later by the shipper, so a protocol-version
bump never has to migrate already-queued rows.

Two kinds:
  * 'event'  — an outage/uplink transition (the real-time truth). NEVER evicted.
  * 'rollup' — an hourly `poll_rollups` row (trend analytics; reconstructable, so it is
               what eviction sheds first under a long backlog).
Heartbeats are NOT queued — they are live liveness and the shipper sends them direct.
"""
from __future__ import annotations

import json
import sqlite3

from wisp.config import CONFIG, Config


# --- Record shaping (edge dataclasses / rows -> the dicts central stores) ----

def event_record(ev, ts: str, meta=None) -> dict | None:
    """One outage/uplink Event -> a JSON-able record dict, or None if it carries no
    shippable signal. `meta` is the daemon's `engine.meta` (device_id -> DeviceMeta); when
    present we denormalize name/ip/region onto the record so central can show a human row
    ("Tower A 10.0.0.1 DOWN") without yet having an edge device roster (Part B adds that)."""
    name = type(ev).__name__
    rec: dict = {"type": name, "at": ts}
    dev_id = getattr(ev, "device_id", None)
    if dev_id is not None:
        rec["device_id"] = dev_id
        info = (meta or {}).get(dev_id)
        if info is not None:
            rec["device_name"] = getattr(info, "name", None)
            rec["device_ip"] = getattr(info, "ip_address", None)
            rec["device_region"] = getattr(info, "region", None)
    state = getattr(ev, "state", None)
    if state is not None:
        rec["state"] = state
    return rec


def rollup_record(row: sqlite3.Row, ts: str) -> dict:
    """One freshly-folded `poll_rollups` row -> a record dict (the hourly trend point)."""
    return {
        "type": "Rollup",
        "at": ts,
        "device_id": row["device_id"],
        "bucket": row["bucket"],
        "samples": row["samples"],
        "latency_avg": row["latency_avg"],
        "latency_min": row["latency_min"],
        "latency_max": row["latency_max"],
        "loss_avg": row["loss_avg"],
        "down_polls": row["down_polls"],
        "degraded_polls": row["degraded_polls"],
        "up_polls": row["up_polls"],
    }


# --- Enqueue (called inside an existing transaction) -------------------------

def _enqueue(conn: sqlite3.Connection, kind: str, record: dict, ts: str) -> None:
    conn.execute(
        "INSERT INTO outbox (kind, payload, created_at) VALUES (?,?,?)",
        (kind, json.dumps(record, separators=(",", ":")), ts),
    )


def enqueue_events(conn: sqlite3.Connection, events: list, ts: str, meta=None) -> int:
    """Queue outage/uplink events for shipping, inside the caller's open transaction.
    Returns how many rows were enqueued (events with no shippable signal are skipped)."""
    n = 0
    for ev in events:
        rec = event_record(ev, ts, meta)
        if rec is not None:
            _enqueue(conn, "event", rec, ts)
            n += 1
    return n


def enqueue_rollups(conn: sqlite3.Connection, rows: list, ts: str) -> int:
    """Queue freshly-folded rollup rows for shipping, inside the caller's transaction."""
    for row in rows:
        _enqueue(conn, "rollup", rollup_record(row, ts), ts)
    return len(rows)


# --- Drain side (used by the shipper) ---------------------------------------

def pending(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """The oldest unsent records, oldest first — the next batch to ship."""
    return conn.execute(
        "SELECT id, kind, payload, attempts FROM outbox"
        " WHERE sent_at IS NULL ORDER BY id LIMIT ?",
        (max(1, limit),),
    ).fetchall()


def mark_sent(conn: sqlite3.Connection, ids: list[int]) -> None:
    """Delete acked rows. (We delete rather than tombstone — the queue is scratch once
    central holds the record; `sent_at` exists only as a crash-safety marker.)"""
    if not ids:
        return
    conn.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])


def bump_attempts(conn: sqlite3.Connection, ids: list[int]) -> None:
    """Record a failed ship attempt (observability + lets a poison record be spotted)."""
    if not ids:
        return
    conn.executemany("UPDATE outbox SET attempts = attempts + 1 WHERE id = ?",
                     [(i,) for i in ids])


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])


def evict_rollups(conn: sqlite3.Connection, over_by: int) -> int:
    """Shed up to `over_by` of the OLDEST 'rollup' rows to keep the queue under its cap.
    Events are never touched — an unsent outage record is the source of truth and is
    sacred. Returns how many rollup rows were evicted (may be < over_by if the backlog is
    all events, in which case the queue is allowed to grow rather than drop an event)."""
    if over_by <= 0:
        return 0
    cur = conn.execute(
        "DELETE FROM outbox WHERE id IN ("
        "  SELECT id FROM outbox WHERE kind = 'rollup' ORDER BY id LIMIT ?)",
        (over_by,),
    )
    return cur.rowcount or 0
