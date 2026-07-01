"""Shared timestamp normalisation, reused by several of central's modules
(`central/watchdog.py`, `central/rollup.py`, `central/rollout.py`, `central/ports.py`,
`central/analytics.py`) so every stamp comparison across the codebase agrees on the
same naive-UTC representation, whether it came from an ISO8601 poll/outage stamp or
SQLite's `datetime('now')` ack stamps.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse(ts: str) -> datetime:
    """Tolerant parse → naive UTC. Handles 'T'/space separators and ±offset."""
    dt = datetime.fromisoformat(ts.replace(" ", "T"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
