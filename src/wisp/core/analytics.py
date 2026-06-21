"""Layer 3 — Business Intelligence query helpers (read-only).

Shared functions that turn `poll_results` + `outages` rows into the numbers the
dashboard renders: latest per-device state, outages overlapping a time window,
downtime per device, and repeat-offender counts. `server/services.py` builds the
JSON dashboard views on top of these.

All timestamps are normalised to naive-UTC on read so the ISO8601 poll/outage
stamps and SQLite's `datetime('now')` ack stamps compare cleanly.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from wisp.core.state_machine import DOWN


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse(ts: str) -> datetime:
    """Tolerant parse → naive UTC. Handles 'T'/space separators and ±offset."""
    dt = datetime.fromisoformat(ts.replace(" ", "T"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# --- shared queries ---------------------------------------------------------
def latest_states(conn) -> dict[int, str]:
    rows = conn.execute(
        "SELECT pr.device_id AS did, pr.state AS state FROM poll_results pr"
        " JOIN (SELECT device_id, MAX(id) mid FROM poll_results GROUP BY device_id) m"
        " ON pr.id = m.mid"
    ).fetchall()
    return {r["did"]: r["state"] for r in rows}


def _outages_in_window(conn, win_start: datetime, win_end: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT o.*, d.name, d.region, d.criticality"
        " FROM outages o JOIN devices d ON d.id = o.device_id"
    ).fetchall()
    out = []
    for r in rows:
        start = _parse(r["started_at"])
        end = _parse(r["resolved_at"]) if r["resolved_at"] else win_end
        if end >= win_start and start <= win_end:  # overlaps the window
            d = dict(r)
            d["_start"], d["_end"] = start, end
            out.append(d)
    return out


def _downtime_by_device(outages: list[dict], win_start, win_end,
                        only_down: bool = True) -> dict[int, float]:
    per: dict[int, float] = defaultdict(float)
    for o in outages:
        if only_down and o["final_state"] != DOWN:
            continue
        s = max(o["_start"], win_start)
        e = min(o["_end"], win_end)
        if e > s:
            per[o["device_id"]] += (e - s).total_seconds()
    return per


def _offender_counts(down_outages: list[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = defaultdict(int)
    for o in down_outages:
        counts[o["name"]] += 1
    return sorted(counts.items(), key=lambda kv: -kv[1])
