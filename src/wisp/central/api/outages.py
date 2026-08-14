from __future__ import annotations

import re
from datetime import datetime, timezone

from wisp.central import analytics as central_analytics
from wisp.central import incidents, onuroster, ponfault
from wisp.central import issues as central_issues
from wisp.central import pdf as central_pdf
from wisp.central import rollup as central_rollup
from wisp.central import xlsx as central_xlsx
from wisp.central.api.common import (DENIED, can_triage, in_scope, keep_visible,
                                     now_iso, olt_liveness, org_or_400,
                                     q_int_or, reader_or_401,
                                     triage_outage_org, visible_device_ids)


def summary(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    scope = visible_device_ids(h, user, org)
    h._reply(200, {"uplink_down": h.store.uplink_active(org),
                   "low_bandwidth": keep_visible(
                       h.store.low_bandwidth_alarms(org), scope),
                   "high_bandwidth": keep_visible(
                       h.store.high_bandwidth_alarms(org), scope)})


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
    rows = h.store.triage_outages(org)
    scope = visible_device_ids(h, user, org)
    if scope is not None:
        me = user["username"]
        rows = [r for r in rows if r.get("device_id") in scope
                or me in (r.get("assigned_to") or [])]
    h._reply(200, {"outages": rows})


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
    h._reply(200, {"events": keep_visible(
        h.store.list_events(org, limit, before_id),
        visible_device_ids(h, user, org))})


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
                   "devices": keep_visible(
                       central_analytics.device_reliability(
                           h.store, org, since, until),
                       visible_device_ids(h, user, org))})


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
    if not in_scope(visible_device_ids(h, user, org), did):
        h._reply(403, {"error": "forbidden"})
        return
    days = q_int_or(qs, "days", 7)
    days = min(days, central_rollup.RETENTION_DAYS)
    since, until = central_analytics.window(days)
    h._reply(200, {"since": since, "until": until,
                   "buckets": h.store.device_rollup_series(org, did, since, until)})


def pon_faults(h, qs):
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
        scope = visible_device_ids(h, user, org)
        if not in_scope(scope, did):
            h._reply(403, {"error": "forbidden"})
            return
        rows = h.store.org_onu_rows(org, did)
    else:
        org = h._scope_org(user, qs)
        if not org:
            h._reply(400, {"error": "org required"})
            return
        scope = visible_device_ids(h, user, org)
        rows = keep_visible(h.store.org_onu_rows(org), scope)
    devs = keep_visible(h.store.list_org_devices(org), scope, "id")
    now = datetime.now(timezone.utc)
    down_olts, stale_olts = olt_liveness(devs, now, h.cfg.central_node_stale_s)
    skip = down_olts | stale_olts
    rows = [r for r in rows if r["device_id"] not in skip]
    dists = ponfault.passive_distances(devs, h.store.list_link_routes(org))
    faults = ponfault.evaluate_org(rows, now, passive_dists=dists,
                                   witness_macs=h.store.onu_place_macs(org))
    h._reply(200, {"faults": [f.as_dict() for f in faults]})


def pon_summary(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    now = datetime.now(timezone.utc)
    scope = visible_device_ids(h, user, org)
    rows = keep_visible(h.store.org_onu_rows(org), scope)
    devs = keep_visible(h.store.list_org_devices(org), scope, "id")
    down_olts, stale_olts = olt_liveness(devs, now, h.cfg.central_node_stale_s)
    seen_rows = [r for r in rows if r["device_id"] not in stale_olts]
    live_rows = [r for r in seen_rows if r["device_id"] not in down_olts]
    dists = ponfault.passive_distances(devs, h.store.list_link_routes(org))
    faults = ponfault.evaluate_org(live_rows, now, passive_dists=dists,
                                   witness_macs=h.store.onu_place_macs(org))
    dups = onuroster.duplicate_macs(live_rows, now)
    roster = onuroster.current_roster(seen_rows, now)
    online = sum(1 for r in roster
                 if r.get("state") == "online" and r["device_id"] not in down_olts)
    default_cap = h.cfg.onu_pon_limit
    limits = {d["id"]: (int(d["onu_pon_limit"]) if d.get("onu_pon_limit") is not None
                        else default_cap) for d in devs}
    caps = onuroster.capacity_faults(
        live_rows, now, lambda dev_id: limits.get(dev_id, default_cap))
    graded = [r for r in roster
              if str(r.get("state") or "") == "online"
              and r["device_id"] not in down_olts]
    onus_crit = sum(1 for r in graded if r.get("severity") == "crit")
    onus_warn = sum(1 for r in graded if r.get("severity") == "warn")
    onus_rx = sum(1 for r in roster if r.get("rx_dbm") is not None)
    olts_rx = len({r["device_id"] for r in roster if r.get("rx_dbm") is not None})
    h._reply(200, {
        "olts": len({r["device_id"] for r in roster}),
        "onus_total": len(roster),
        "onus_online": online,
        "onus_offline": len(roster) - online,
        "onus_crit": onus_crit,
        "onus_warn": onus_warn,
        "onus_rx": onus_rx,
        "olts_rx": olts_rx,
        "fiber_cuts": sum(1 for f in faults if f.kind == "fiber"),
        "pons_over_cap": len(caps),
        "over_cap_device_ids": sorted({c.device_id for c in caps}),
        "pon_cap": default_cap,
        "pon_cap_worst": max((c.onus for c in caps), default=0),
        "dup_macs_live": sum(1 for d in dups if d.online_members >= 2),
        "dup_macs_total": len(dups),
    })


def incident_shape(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    found = incidents.evaluate(
        keep_visible(h.store.list_org_devices(org),
                     visible_device_ids(h, user, org), "id"),
        datetime.now(timezone.utc))
    h._reply(200, {"incidents": [i.as_dict() for i in found]})


def _kinds_arg(qs) -> list[str] | None:
    raw: list[str] = []
    for key in ("kind", "kinds"):
        for val in qs.get(key) or []:
            raw += [part.strip() for part in str(val).split(",")]
    picked = [k for k in dict.fromkeys(raw) if k in central_issues.KINDS]
    return picked or None


def _visible_issues(h, user, org, rows):

    scope = visible_device_ids(h, user, org)
    if scope is None:
        return rows
    out = []
    for r in rows:
        did = r.get("device_id")
        if did is not None:
            if did in scope:
                out.append(r)
        elif r.get("kind") == "probe_stale" and scope.intersection(
                h.store.node_device_ids(org, r.get("subject") or "")):
            out.append(r)
    return out


def issues(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    rows = _visible_issues(h, user, org, central_issues.collect(h.store, h.cfg, org))
    kinds = _kinds_arg(qs)
    shown = ([r for r in rows if r["kind"] in set(kinds)] if kinds else rows)
    h._reply(200, {"issues": shown, "counts": central_issues.counts(rows),
                   "total": len(rows), "generated_at": now_iso(),
                   "kinds": central_issues.KINDS,
                   "kind_labels": central_issues.KIND_LABELS})


_PDF_COLUMNS = (
    ("kind_label", "Issue", 1.3, False),
    ("device_name", "Device", 1.6, True),
    ("subject", "Item", 2.0, True),
    ("region", "Region", 1.0, False),
    ("detail", "Detail", 3.2, False),
    ("since", "Since", 1.5, False),
)


def issues_pdf(h, qs):

    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    rows = _visible_issues(h, user, org, central_issues.collect(h.store, h.cfg, org))
    kinds = _kinds_arg(qs)
    if kinds:
        want = set(kinds)
        rows = [r for r in rows if r["kind"] in want]
    stamp = now_iso()
    labels = central_issues.KIND_LABELS
    which = (", ".join(labels.get(k, k) for k in kinds) if kinds
             else "all issue types")
    columns = [central_pdf.Column(key, title, weight, mono=mono)
               for key, title, weight, mono in _PDF_COLUMNS]
    from wisp.egress.notifiers import _wa_time
    printed = [{**r, "since": _wa_time(r["since"]) if r["since"] else None}
               for r in rows]
    body = central_pdf.table_pdf(
        title=f"Open issues · {org}",
        subtitle=(f"{len(rows)} issue(s) · {which} · generated "
                  f"{_wa_time(stamp)}"),
        columns=columns, rows=printed,
        footer=f"WISP Central · {org}")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", org)[:40]
    h._send_binary(200, "application/pdf", body,
                   filename=f"issues-{safe}-{stamp[:10]}.pdf")


_XLSX_COLUMNS = (
    ("severity", "Severity", 12.0),
    ("kind_label", "Issue", 22.0),
    ("device_name", "Device", 24.0),
    ("subject", "Item", 34.0),
    ("region", "Region", 22.0),
    ("detail", "Detail", 70.0),
    ("since", "Since", 24.0),
)


def issues_xlsx(h, qs):

    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    rows = _visible_issues(h, user, org, central_issues.collect(h.store, h.cfg, org))
    kinds = _kinds_arg(qs)
    if kinds:
        want = set(kinds)
        rows = [r for r in rows if r["kind"] in want]
    from wisp.egress.notifiers import _wa_local
    sheet_rows = [{**r, "since": _wa_local(r["since"])} for r in rows]
    body = central_xlsx.table_xlsx(
        sheet_name=f"Issues {org}",
        columns=[central_xlsx.Column(key, title, width_cap=cap)
                 for key, title, cap in _XLSX_COLUMNS],
        rows=sheet_rows)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", org)[:40]
    h._send_binary(
        200,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        body, filename=f"issues-{safe}-{now_iso()[:10]}.xlsx")


def acknowledge(h, user, body):
    oid = int(body.get("outage_id") or 0)
    org = triage_outage_org(h, user, oid)
    if org is DENIED:
        return
    ok = h.store.acknowledge_outage(org, oid, user["username"])
    h._reply(200 if ok else 404, {"ok": ok})


def assign(h, user, body):


    oid = int(body.get("outage_id") or 0)
    org = h.store.outage_org(oid)
    if org is None:
        h._reply(404, {"error": "no such outage"})
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    raw = body.get("usernames")
    wanted = [str(u).strip() for u in raw if str(u or "").strip()] \
        if isinstance(raw, list) else []
    if not wanted:
        h._reply(422, {"error": "name at least one account to assign to"})
        return
    live = {u["username"] for u in h.store.list_users(org)
            if u["org_id"] == org and u["is_active"]}
    names = [u for u in dict.fromkeys(wanted) if u in live]
    if not names:
        h._reply(422, {"error": "no active account in this org matches those names"})
        return
    if not h.store.assign_outage(org, oid, names, user["username"]):
        h._reply(409, {"error": "outage is already resolved"})
        return

    numbers = h.store.named_whatsapp(org, names)
    detail = f"assigned to {', '.join(names)} by {user['username']}"
    row = next((o for o in h.store.triage_outages(org) if o["id"] == oid), None)
    device = (row or {}).get("device_name") or f"outage #{oid}"
    reached = _page_assignees(h, org, oid, device, detail, numbers, row)
    h._reply(200, {"ok": True, "assigned_to": names, "notified": reached})


def _page_assignees(h, org, oid, device, detail, numbers, row) -> int:


    if not numbers:
        return 0
    from wisp.egress.notifiers import WhatsAppFacts
    body = (f"🔧 You have been assigned to the outage on *{device}*.\n"
            f"{detail}.\n\nTap *I'm on it* so the team knows you're going.")
    buttons = [(f"acc:{oid}", "✅ I'm on it")]
    if (row or {}).get("device_id"):
        buttons.append((f"map:{row['device_id']}", "📍 On map"))
    cold: list[str] = []
    reached = 0
    for number in numbers:
        if h.notifier.send_buttons(number, body, buttons).ok:
            reached += 1
        else:
            cold.append(number)
    if cold:
        res = h.notifier.send(
            f"🔧 Assigned: {device}",
            f"You have been assigned to the outage on {device}. {detail}.",
            4, whatsapp=cold,
            facts=WhatsAppFacts(subject=device, status="ASSIGNED",
                                detail=detail,
                                timestamp=(row or {}).get("started_at") or now_iso()))
        if res.ok:
            reached += len(cold)
    return reached


def accept(h, user, body):


    oid = int(body.get("outage_id") or 0)
    if h.store.outage_org(oid) is None:
        h._reply(404, {"error": "no such outage"})
        return
    org = triage_outage_org(h, user, oid)
    if org is DENIED:
        return
    outcome = h.store.accept_outage(org, oid, user["username"])
    if outcome in ("missing", "closed"):
        h._reply(409 if outcome == "closed" else 404,
                 {"error": "this outage is already resolved" if outcome == "closed"
                           else "no such outage"})
        return
    if outcome == "not_assigned":
        h._reply(403, {"error": "you are not assigned to this outage"})
        return
    row = next((o for o in h.store.triage_outages(org) if o["id"] == oid), None)
    if outcome == "ok":
        _tell_assigner(h, org, row, user["username"])
    h._reply(200, {"ok": True, "already": outcome == "already",
                   "accepted_by": (row or {}).get("accepted_by", [])})


def _tell_assigner(h, org, row, who: str) -> None:
    by = (row or {}).get("assigned_by")
    if not by:
        return
    numbers = h.store.named_whatsapp(org, [by])
    if not numbers:
        return
    device = (row or {}).get("device_name") or "the outage"
    detail = f"{who} accepted the assignment on {device}"
    from wisp.egress.notifiers import WhatsAppFacts
    text = f"✅ {detail}."
    cold = [n for n in numbers if not h.notifier.send_text(n, text).ok]
    if cold:
        h.notifier.send(
            f"✅ Accepted: {device}", text, 3, whatsapp=cold,
            facts=WhatsAppFacts(subject=device, status="ACCEPTED", detail=detail,
                                timestamp=(row or {}).get("accepted_at") or now_iso()))


def postmortem(h, user, body):
    oid = int(body.get("outage_id") or 0)
    org = triage_outage_org(h, user, oid)
    if org is DENIED:
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
             or "Bulk cleared, no post-mortem recorded")
    n = h.store.clear_pending_postmortems(org, cause)
    h._reply(200, {"ok": True, "cleared": n})
