"""Outage triage, logs, analytics, PON fault verdicts, incident shape, SSE."""
from __future__ import annotations

from datetime import datetime, timezone

from wisp.central import analytics as central_analytics
from wisp.central import incidents, ponfault
from wisp.central import rollup as central_rollup
from wisp.central.api.common import (org_or_400, q_int_or, reader_or_401)


def summary(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    h._reply(200, {"uplink_down": h.store.uplink_active(org),
                   "low_bandwidth": h.store.low_bandwidth_alarms(org),
                   "high_bandwidth": h.store.high_bandwidth_alarms(org)})


def events(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = h._scope_org(user, qs)
    h._serve_events(org)


def list_open(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    h._reply(200, {"outages": h.store.triage_outages(org)})


def logs(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    limit = q_int_or(qs, "limit", 100)
    before_raw = (qs.get("before") or [None])[0]
    try:
        before_id = int(before_raw) if before_raw is not None else None
    except ValueError:
        before_id = None
    h._reply(200, {"events": h.store.list_events(org, limit, before_id)})


def analytics(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    days = q_int_or(qs, "days", 30)
    since, until = central_analytics.window(days)
    h._reply(200, {"since": since, "until": until,
                   "devices": central_analytics.device_reliability(
                       h.store, org, since, until)})


def analytics_trend(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    try:
        did = int((qs.get("device_id") or [None])[0])
    except (TypeError, ValueError):
        h._reply(400, {"error": "device_id required"})
        return
    org = h.store.device_org(did)
    if org is None or not (user["is_superadmin"] or user["org_id"] == org):
        h._reply(403, {"error": "forbidden"})
        return
    days = q_int_or(qs, "days", 7)
    days = min(days, central_rollup.RETENTION_DAYS)
    since, until = central_analytics.window(days)
    h._reply(200, {"since": since, "until": until,
                   "buckets": h.store.device_rollup_series(org, did, since, until)})


def pon_faults(h, qs):
    # PON mass-drop read: dying-gasp (power) vs LOS (fiber) + a cut
    # distance interval off ranging. Pure read-side — never pages.
    user = reader_or_401(h)
    if not user:
        return
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
        rows = h.store.org_onu_rows(org, did)
    else:
        org = h._scope_org(user, qs)
        if not org:
            h._reply(400, {"error": "org required"})
            return
        rows = h.store.org_onu_rows(org)
    dists = ponfault.passive_distances(h.store.list_org_devices(org),
                                       h.store.list_link_routes(org))
    faults = ponfault.evaluate_org(rows, datetime.now(timezone.utc),
                                   passive_dists=dists)
    h._reply(200, {"faults": [f.as_dict() for f in faults]})


def incident_shape(h, qs):
    # power-vs-upstream annotation over the open outage wave —
    # explains alarms, never mutes or reroutes a page
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    found = incidents.evaluate(h.store.list_org_devices(org),
                               datetime.now(timezone.utc))
    h._reply(200, {"incidents": [i.as_dict() for i in found]})


def acknowledge(h, user, body):
    oid = int(body.get("outage_id") or 0)
    org = h.store.outage_org(oid)
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    ok = h.store.acknowledge_outage(org, oid, user["username"])
    h._reply(200 if ok else 404, {"ok": ok})


def postmortem(h, user, body):
    oid = int(body.get("outage_id") or 0)
    org = h.store.outage_org(oid)
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    cause = str(body.get("root_cause") or "").strip()
    if not cause:
        h._reply(422, {"error": "root_cause is required"})
        return
    notes = str(body.get("resolution_notes") or "").strip() or None
    ok = h.store.set_outage_postmortem(org, oid, cause, notes)
    h._reply(200 if ok else 404, {"ok": ok})


def clear_postmortems(h, user, body):
    org = user["org_id"] if not user["is_superadmin"] else (body.get("org") or None)
    if not org:
        h._reply(400, {"error": "org is required"})
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    cause = (str(body.get("root_cause") or "").strip()
             or "Bulk cleared — no post-mortem recorded")
    n = h.store.clear_pending_postmortems(org, cause)
    h._reply(200, {"ok": True, "cleared": n})
