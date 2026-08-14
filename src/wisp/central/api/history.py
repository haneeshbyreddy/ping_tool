from __future__ import annotations

from datetime import datetime, timezone

from wisp.central import analytics as central_analytics
from wisp.central import history as central_history
from wisp.central.api.common import (in_scope, org_or_400, q_int_or,
                                     reader_or_401, visible_device_ids)


def _iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(
        tzinfo=None).isoformat(timespec="seconds")

# Range caps: bounded endpoints are a hard chart constraint. 400 days covers
# "a year, with margin", matching the historian's day-tier retention argument.
MAX_DAYS = 400


def reliability(h, qs):
    # Two views on one route, split by ?device_id=:
    #   device -> the day-availability strip + outage spans + probe coverage,
    #             scoped like every per-device read (a worker sees only
    #             assigned devices);
    #   org    -> weekly outage counts + triage-latency percentiles,
    #             OWNER-ONLY (org-wide numbers leak past assignment scope).
    user = reader_or_401(h)
    if not user:
        return
    days = min(max(1, q_int_or(qs, "days", 90)), MAX_DAYS)
    since, until = central_analytics.window(days)
    did_raw = (qs.get("device_id") or [None])[0]
    if did_raw is not None:
        try:
            did = int(did_raw)
        except (TypeError, ValueError):
            h._reply(400, {"error": "bad device_id"})
            return
        org = h.store.device_org(did)
        if org is None or not (user["is_superadmin"] or user["org_id"] == org):
            h._reply(403, {"error": "forbidden"})
            return
        if not in_scope(visible_device_ids(h, user, org), did):
            h._reply(403, {"error": "forbidden"})
            return
        avail = central_analytics.day_availability(h.store, org, did,
                                                   since, until)
        h._reply(200, {"since": since, "until": until,
                       "days": avail["days"], "spans": avail["spans"],
                       "coverage": _coverage(h.store, org, did, since, until),
                       "recording_since": h.store.history_since()})
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "owner only"})
        return
    # Clamp the window to where this org's record actually begins — a chart
    # whose axis runs months before the first outage row renders "zero
    # outages" over a time the org did not exist, which is the young-historian
    # lie in another shape. First-row clamps, not org creation: for an event
    # table the first row IS where recording starts.
    since = max(since, (h.store.first_outage_at(org) or since)[:19])
    h._reply(200, {"since": since, "until": until,
                   "weeks": central_analytics.weekly_outage_stats(
                       h.store, org, since, until)})


def _coverage(store, org: str, device_id: int, since: str, until: str) -> list[dict]:
    # Was the probe even watching? Day-grain sample counts, merged from
    # hist_device_day (accumulating since the historian shipped) with
    # device_rollups folded to days for anything the historian hasn't folded
    # yet (its last ~30 days of hours). A day in neither source is honestly
    # absent — the chart renders "coverage unknown", never "up".
    days: dict[int, int] = {}
    s_ep = central_history.epoch_s(since)
    u_ep = central_history.epoch_s(until)
    for r in store.device_day_history(org, device_id, s_ep, u_ep + 1):
        days[r["day"]] = r["samples"]
    roll: dict[int, int] = {}
    for b in store.device_rollup_series(org, device_id, since, until):
        d = central_history.day_floor(central_history.epoch_s(b["bucket"]))
        roll[d] = roll.get(d, 0) + b["samples"]
    for d, n in roll.items():
        # hist_device_day is folded FROM these rollups — where both exist the
        # fold wins, or the same samples would count twice.
        days.setdefault(d, n)
    return [{"day": d, "samples": n} for d, n in sorted(days.items())]


def onus(h, qs):
    # The org's crit/warn ONU trend for the dashboard (hist_olt_hour summed
    # per bucket). Owner-only: org-wide counts leak past assignment scope.
    # The window CLAMPS to history_since — the historian is young, and a
    # domain that runs weeks before recording began renders "zero crit" over
    # time nobody was measuring.
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "owner only"})
        return
    days = min(max(1, q_int_or(qs, "days", 14)), MAX_DAYS)
    since, until = central_analytics.window(days)
    recording = h.store.history_since()
    if recording:
        since = max(since, recording[:19])
    # Floor to the bucket grid, or the clamp excludes the partial first hour
    # (a bucket labeled 02:00 whose samples all postdate an 02:03 start is
    # honest — its samples column carries the coverage).
    since_s = (central_history.epoch_s(since) // 3600) * 3600
    h._reply(200, {"since": _iso_utc(since_s), "until": until,
                   "recording_since": recording,
                   "buckets": h.store.org_optics_hours(
                       org, since_s, central_history.epoch_s(until) + 1)})


def paging(h, qs):
    # The governor's ledger: alert_log counts per (UTC day, kind, status).
    # Owner-only — it enumerates the org's whole alerting behaviour.
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "owner only"})
        return
    days = min(max(1, q_int_or(qs, "days", 90)), MAX_DAYS)
    since, until = central_analytics.window(days)
    since = max(since, (h.store.first_alert_at(org) or since)[:19])
    h._reply(200, {"since": since, "until": until,
                   "rows": h.store.alert_counts_by_day(org, since, until)})
