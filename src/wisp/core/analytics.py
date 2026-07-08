from __future__ import annotations

from datetime import datetime, timezone

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace(" ", "T"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
