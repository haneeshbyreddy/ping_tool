"""Central-side historical analytics (CLAUDE.md item 2, first slice): outage-history-
derived downtime/uptime/SLA reporting.

Deliberately scoped to what the EXISTING `outages` table can already answer — "how
reliable was Tower A last month" is downtime-window math (the old edge's
`core/analytics.py` already had this), ported onto `CentralStore`'s tenant-scoped
schema. No new storage, no retention-policy decision needed: central already keeps the
full outage history (nothing prunes it in central-brain mode).

What this does NOT do: a latency/packet-loss TREND chart. `device_states` only holds
each device's latest sample (overwritten every cycle, not a history), so a trend line
needs its own time-series storage — and CLAUDE.md flags the rollup granularity/retention
call as a still-open product decision. Don't conflate the two; this module answers "was
it down, and for how long", not "how has its latency looked over time".
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from wisp.core.analytics import _parse   # shared naive-UTC timestamp normalisation
from wisp.core.state_machine import DOWN


def window(days: int, *, until: str | None = None) -> tuple[str, str]:
    """(since_iso, until_iso) for the trailing `days` days ending at `until` (default
    now). Both ISO8601, matching the outage table's own timestamp format."""
    end = _parse(until) if until else datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=max(0, days))
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def device_reliability(store, tenant_id: str, since: str, until: str) -> list[dict]:
    """Per-device downtime seconds + uptime % over [since, until], for EVERY active
    device the tenant has configured — not just ones with an outage, since a device
    with zero outages in the window is 100% up and should still appear in an SLA
    report. Only DOWN-final-state outages count against uptime (mirrors the edge's
    `only_down=True` default): an UNREACHABLE outage is a topology-suppressed artifact
    of a dead parent, not this device's own fault, so it doesn't count against IT."""
    win_start, win_end = _parse(since), _parse(until)
    span = (win_end - win_start).total_seconds()
    devices = {d["id"]: d for d in store.list_org_devices(tenant_id)}

    downtime: dict[int, float] = defaultdict(float)
    outage_counts: dict[int, int] = defaultdict(int)
    for o in store.outages_in_window(tenant_id, since, until):
        if o["final_state"] != DOWN:
            continue
        s = max(_parse(o["started_at"]), win_start)
        e = min(_parse(o["resolved_at"]) if o["resolved_at"] else win_end, win_end)
        if e > s:
            downtime[o["device_id"]] += (e - s).total_seconds()
            outage_counts[o["device_id"]] += 1

    report = []
    for did, dev in devices.items():
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
