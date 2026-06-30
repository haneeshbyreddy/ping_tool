"""Hourly rollups — fold raw `poll_results` into compact per-device/hour aggregates.

Raw poll samples are hot scratch: the engine consumes them as they stream in, and
the only durable read is "the latest state per device" (current status + restart
rehydration) plus a short forensic window. Nothing reads their historical *body*, so
keeping a billion raw rows for 90 days is cost without a reader.

Trends instead live here: one `poll_rollups` row per device per hour (latency
min/avg/max, mean loss, and how many polls fell in each state), so a latency/uptime
chart over weeks scans hours, not millions of polls. The `outages` table remains the
source of truth for incidents — this tier is purely for analytics.

The daemon calls `roll_up()` once an hour (alongside the retention prune). It is
pure SQL — a single `GROUP BY` per run — so it never pulls raw rows into Python.
"""
from __future__ import annotations

from datetime import datetime, timezone

from wisp.config import CONFIG, Config
from wisp.database import outbox
from wisp.database.client import connect, transaction, write_with_retry

# The bucket key is derived straight from the ISO8601 'YYYY-MM-DDTHH:MM:SS+00:00'
# stamp: its first 13 chars are 'YYYY-MM-DDTHH', pinned to the top of the hour. The
# offset is always +00:00 (poll stamps are UTC isoformat), so lexicographic order
# equals chronological order and plain string compares work for the watermark/window.
_BUCKET_EXPR = "substr(timestamp, 1, 13) || ':00:00+00:00'"


def _hour_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def roll_up(cfg: Config = CONFIG, *, now: datetime | None = None) -> int:
    """Fold every *closed* hour not yet rolled up into `poll_rollups`.

    Returns the number of (device, hour) rows written. Idempotent and cheap:
    * only buckets strictly before the current hour are folded — the in-progress
      hour is left alone so it isn't rolled half-complete;
    * a watermark (`MAX(bucket)`) skips already-rolled hours, so each run scans just
      the newest closed hour(s), not the whole table;
    * the insert is `OR IGNORE` against the (device_id, bucket) PK, so a double run
      (startup + the hourly timer firing close together) never double-counts.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = _hour_start(now).isoformat(timespec="seconds")  # exclusive upper bound

    def _do() -> int:
        with connect(cfg) as conn:
            with transaction(conn):
                last = conn.execute(
                    "SELECT MAX(bucket) AS b FROM poll_rollups"
                ).fetchone()["b"]
                cur = conn.execute(
                    f"""
                    INSERT OR IGNORE INTO poll_rollups
                        (device_id, bucket, samples, latency_avg, latency_min,
                         latency_max, loss_avg, down_polls, degraded_polls, up_polls)
                    SELECT
                        device_id,
                        {_BUCKET_EXPR} AS bucket,
                        COUNT(*),
                        AVG(latency_ms), MIN(latency_ms), MAX(latency_ms),
                        AVG(packet_loss),
                        SUM(CASE WHEN state IN ('DOWN', 'UNREACHABLE') THEN 1 ELSE 0 END),
                        SUM(CASE WHEN state = 'DEGRADED' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN state = 'UP' THEN 1 ELSE 0 END)
                    FROM poll_results
                    WHERE {_BUCKET_EXPR} < ?
                      AND (? IS NULL OR {_BUCKET_EXPR} > ?)
                    GROUP BY device_id, bucket
                    """,
                    (cutoff, last, last),
                )
                rolled = cur.rowcount
                # Central reporting (Phase 10 Part A): queue the freshly-folded rollup rows
                # for the shipper in this SAME transaction (so the fold and its shippable
                # records commit atomically). The window predicate matches the INSERT above,
                # so it selects exactly the rows just folded. No-op unless central is on.
                if rolled and cfg.central_enabled():
                    new_rows = conn.execute(
                        f"""
                        SELECT device_id, bucket, samples, latency_avg, latency_min,
                               latency_max, loss_avg, down_polls, degraded_polls, up_polls
                        FROM poll_rollups
                        WHERE bucket < ? AND (? IS NULL OR bucket > ?)
                        """,
                        (cutoff, last, last),
                    ).fetchall()
                    outbox.enqueue_rollups(conn, new_rows, now.isoformat(timespec="seconds"))
                return rolled
    return int(write_with_retry(_do) or 0)
