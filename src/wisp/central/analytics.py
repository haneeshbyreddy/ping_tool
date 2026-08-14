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


_DAY_S = 86400
_WEEK_S = 7 * _DAY_S
# Monday 1970-01-05 00:00 UTC — anchoring week buckets here makes every bucket
# an ISO week without any calendar arithmetic.
_MONDAY_EPOCH = 4 * _DAY_S


def _epoch(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _pctl(sorted_vals: list[float], q: float) -> float | None:
    # Nearest-rank, the historian's rule (history.rx_stats) — deterministic,
    # no interpolation to explain in a tooltip.
    if not sorted_vals:
        return None
    return sorted_vals[round(q * (len(sorted_vals) - 1))]


def day_availability(store, org_id: str, device_id: int, since: str,
                     until: str) -> dict:
    # One device's outage record folded to UTC days: downtime seconds + the
    # spans themselves. Downtime counts final_state == DOWN only — the same
    # rule device_reliability keeps (an UNREACHABLE span is a parent's outage
    # restated on its victims), so this strip and the analytics table cannot
    # disagree about one device. The spans list still carries every outage,
    # labeled, because "my parent was down" is worth seeing on a timeline.
    win_start, win_end = _parse(since), _parse(until)
    days: dict[int, dict] = {}
    spans: list[dict] = []
    for o in store.outages_in_window(org_id, since, until):
        if o["device_id"] != device_id:
            continue
        s = max(_parse(o["started_at"]), win_start)
        e = min(_parse(o["resolved_at"]) if o["resolved_at"] else win_end, win_end)
        if e <= s:
            continue
        spans.append({
            "id": o["id"], "started_at": o["started_at"],
            "resolved_at": o["resolved_at"], "final_state": o["final_state"],
            "root_cause": o["root_cause"],
            "duration_s": round((
                (_parse(o["resolved_at"]) if o["resolved_at"] else win_end)
                - _parse(o["started_at"])).total_seconds()),
        })
        if o["final_state"] != DOWN:
            continue
        cur_s, end_s = _epoch(s), _epoch(e)
        d = (cur_s // _DAY_S) * _DAY_S
        while d < end_s:
            lo, hi = max(cur_s, d), min(end_s, d + _DAY_S)
            if hi > lo:
                row = days.setdefault(d, {"day": d, "down_s": 0, "outages": 0})
                row["down_s"] += hi - lo
                row["outages"] += 1
            d += _DAY_S
    return {"days": sorted(days.values(), key=lambda r: r["day"]),
            "spans": spans}


def weekly_outage_stats(store, org_id: str, since: str, until: str) -> list[dict]:
    # Org-level story in ISO-week buckets: how many outages opened, and the
    # median / p90 of time-to-resolve and time-to-acknowledge. DOWN only, the
    # device_reliability rule. Buckets are keyed by the Monday 00:00 UTC epoch.
    weeks: dict[int, dict] = {}
    for o in store.outages_in_window(org_id, since, until):
        if o["final_state"] != DOWN:
            continue
        started = _parse(o["started_at"])
        if not (_parse(since) <= started <= _parse(until)):
            continue
        wk = ((_epoch(started) - _MONDAY_EPOCH) // _WEEK_S) * _WEEK_S + _MONDAY_EPOCH
        row = weeks.setdefault(wk, {"week": wk, "outages": 0, "resolved": 0,
                                    "_ttr": [], "_tta": []})
        row["outages"] += 1
        if o["resolved_at"]:
            row["resolved"] += 1
            row["_ttr"].append((_parse(o["resolved_at"]) - started).total_seconds())
        if o["acknowledged_at"]:
            row["_tta"].append((_parse(o["acknowledged_at"]) - started).total_seconds())
    out = []
    for wk in sorted(weeks):
        row = weeks[wk]
        ttr = sorted(row.pop("_ttr"))
        tta = sorted(row.pop("_tta"))
        row["ttr_p50_s"] = _pctl(ttr, 0.5)
        row["ttr_p90_s"] = _pctl(ttr, 0.9)
        row["tta_p50_s"] = _pctl(tta, 0.5)
        row["tta_p90_s"] = _pctl(tta, 0.9)
        out.append(row)
    return out
