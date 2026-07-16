"""Outage triage, logs, analytics, PON fault verdicts, incident shape, SSE."""
from __future__ import annotations

from datetime import datetime, timezone

from wisp.central import analytics as central_analytics
from wisp.central import incidents, onuroster, ponfault
from wisp.central import rollup as central_rollup
from wisp.central.api.common import (olt_liveness, org_or_400, q_int_or,
                                     reader_or_401)


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
    devs = h.store.list_org_devices(org)
    # A down OLT's ICMP outage owns it, and a probe-silent OLT is unknown — either
    # way don't let its still-fresh optics walk tell a second (fiber/power) story
    # while we can't see it (same liveness gate as pon_summary).
    now = datetime.now(timezone.utc)
    down_olts, stale_olts = olt_liveness(devs, now, h.cfg.central_node_stale_s)
    skip = down_olts | stale_olts
    rows = [r for r in rows if r["device_id"] not in skip]
    dists = ponfault.passive_distances(devs, h.store.list_link_routes(org))
    faults = ponfault.evaluate_org(rows, now, passive_dists=dists)
    h._reply(200, {"faults": [f.as_dict() for f in faults]})


def pon_summary(h, qs):
    # Org-wide optical/PON rollup for the dashboard KPI strip: live duplicate
    # MACs, suspected fiber cuts, PONs at/over their ONU cap, and ONU online
    # counts across every OLT with a fresh walk. Pure read-side — never pages.
    # Duplicates, capacity, and roster ride the freshest-walk-per-OLT view
    # (stale OLTs dropped), matching the per-panel numbers so the strip and the
    # drill-down never disagree.
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    now = datetime.now(timezone.utc)
    rows = h.store.org_onu_rows(org)
    devs = h.store.list_org_devices(org)
    # Gate the rollup on ICMP liveness, in the same hierarchy the device-count KPI
    # already uses: a confirmed-down OLT's ONUs go offline (kept in the total as
    # blast radius); a probe-silent OLT is unknown and drops out entirely. Both
    # matter because the last SNMP walk stays "fresh" for up to STALE_S after the
    # OLT (or its edge) goes away — without this it keeps counting ONUs online.
    down_olts, stale_olts = olt_liveness(devs, now, h.cfg.central_node_stale_s)
    seen_rows = [r for r in rows if r["device_id"] not in stale_olts]
    live_rows = [r for r in seen_rows if r["device_id"] not in down_olts]
    dists = ponfault.passive_distances(devs, h.store.list_link_routes(org))
    faults = ponfault.evaluate_org(live_rows, now, passive_dists=dists)
    dups = onuroster.duplicate_macs(live_rows, now)
    roster = onuroster.current_roster(seen_rows, now)
    online = sum(1 for r in roster
                 if r.get("state") == "online" and r["device_id"] not in down_olts)
    # per-OLT cap override → cfg.onu_pon_limit, same resolution the paging
    # sweep uses so a 1:128 GPON box isn't counted as over a 1:64 default
    default_cap = h.cfg.onu_pon_limit
    limits = {d["id"]: (int(d["onu_pon_limit"]) if d.get("onu_pon_limit") is not None
                        else default_cap) for d in devs}
    caps = onuroster.capacity_faults(
        live_rows, now, lambda dev_id: limits.get(dev_id, default_cap))
    h._reply(200, {
        "olts": len({r["device_id"] for r in roster}),
        "onus_total": len(roster),
        "onus_online": online,
        "onus_offline": len(roster) - online,
        "fiber_cuts": sum(1 for f in faults if f.kind == "fiber"),
        "pons_over_cap": len(caps),
        "pon_cap": default_cap,
        "pon_cap_worst": max((c.onus for c in caps), default=0),
        # a MAC on ≥2 slots is "live" only when ≥2 are ONLINE at once — the
        # paging rule; dead-member dups are C-Data reg-table history, not clones
        "dup_macs_live": sum(1 for d in dups if d.online_members >= 2),
        "dup_macs_total": len(dups),
    })


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
