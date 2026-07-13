from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from wisp.central.inventory import PASSIVE_TYPES
from wisp.core.analytics import _parse
from wisp.core.state_machine import DOWN

def window(days: int, *, until: str | None = None) -> tuple[str, str]:
    end = _parse(until) if until else datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=max(0, days))
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")

def device_reliability(store, org_id: str, since: str, until: str) -> list[dict]:
    win_start, win_end = _parse(since), _parse(until)
    span = (win_end - win_start).total_seconds()
    devices = {d["id"]: d for d in store.list_org_devices(org_id)}

    downtime: dict[int, float] = defaultdict(float)
    outage_counts: dict[int, int] = defaultdict(int)
    for o in store.outages_in_window(org_id, since, until):
        if o["final_state"] != DOWN:
            continue
        s = max(_parse(o["started_at"]), win_start)
        e = min(_parse(o["resolved_at"]) if o["resolved_at"] else win_end, win_end)
        if e > s:
            downtime[o["device_id"]] += (e - s).total_seconds()
            outage_counts[o["device_id"]] += 1

    report = []
    for did, dev in devices.items():
        # passive plant never pings — 100%-uptime rows for splitters would only
        # pad the averages
        if dev.get("device_type") in PASSIVE_TYPES:
            continue
        down_s = downtime.get(did, 0.0)
        uptime_pct = 100.0 if span <= 0 else max(0.0, 100.0 * (1 - down_s / span))
        report.append({
            "device_id": did, "name": dev["name"], "region": dev["region"],
            "downtime_seconds": round(down_s, 1),
            "uptime_pct": round(uptime_pct, 3),
            "outage_count": outage_counts.get(did, 0),
        })
    report.sort(key=lambda r: r["downtime_seconds"], reverse=True)
    return report
