"""Outage triage, logs, analytics, PON fault verdicts, incident shape, SSE."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from wisp.central import analytics as central_analytics
from wisp.central import incidents, onuroster, ponfault
from wisp.central import issues as central_issues
from wisp.central import pdf as central_pdf
from wisp.central import rollup as central_rollup
from wisp.central import xlsx as central_xlsx
from wisp.central.api.common import (can_triage, now_iso, olt_liveness,
                                     org_or_400, q_int_or, reader_or_401)


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
    faults = ponfault.evaluate_org(rows, now, passive_dists=dists,
                                   witness_macs=h.store.onu_place_macs(org))
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
    faults = ponfault.evaluate_org(live_rows, now, passive_dists=dists,
                                   witness_macs=h.store.onu_place_macs(org))
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
    # Optical severity, counted over the SAME roster view as everything else on
    # this strip. Only an ONLINE ONU is graded: a dark one has no light to
    # measure, so its last reading is a fact about the past, and a down OLT's
    # whole roster is already excluded above. This is the org-wide sum of the
    # per-OLT badge (olt_optics.crit_count/warn_count), recomputed here rather
    # than summed from that table because the badge freezes with its walk while
    # this strip must not count an OLT central can no longer see.
    graded = [r for r in roster
              if str(r.get("state") or "") == "online"
              and r["device_id"] not in down_olts]
    onus_crit = sum(1 for r in graded if r.get("severity") == "crit")
    onus_warn = sum(1 for r in graded if r.get("severity") == "warn")
    # Rx COVERAGE — how much of the fleet actually reports optical power. A
    # C-Data/DBC EPON OLT walks a complete roster with every rx_dbm NULL, so
    # "0 critical ONUs" there means "nothing is measured", not "all healthy".
    # The strip has to be able to say which, or the two look identical.
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
        # which OLTs to jump to from the KPI tile — a PON is over cap, not a
        # whole device, but the tile can only drill into the Network tree
        "over_cap_device_ids": sorted({c.device_id for c in caps}),
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


def _kinds_arg(qs) -> list[str] | None:
    """`?kind=port_down&kind=onu_crit` or one comma-joined value, narrowed to the
    known vocabulary. Unknown names are DROPPED and a request left with none
    reads as "no filter": a filter is a view, so a link written against an older
    vocabulary must show the whole list rather than an empty one that looks like
    an all-clear."""
    raw: list[str] = []
    for key in ("kind", "kinds"):
        for val in qs.get(key) or []:
            raw += [part.strip() for part in str(val).split(",")]
    picked = [k for k in dict.fromkeys(raw) if k in central_issues.KINDS]
    return picked or None


def issues(h, qs):
    """Every open issue in the org as a FLAT list — one row per port, ONU, PON or
    probe, not one row per device. The Home tiles drill into the device tree;
    this is the same trouble counted the way it is actually worked."""
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    rows = central_issues.collect(h.store, h.cfg, org)
    kinds = _kinds_arg(qs)
    shown = ([r for r in rows if r["kind"] in set(kinds)] if kinds else rows)
    # counts ride the UNFILTERED list so a filter chip can say how many rows it
    # would show before it is clicked
    h._reply(200, {"issues": shown, "counts": central_issues.counts(rows),
                   "total": len(rows), "generated_at": now_iso(),
                   "kinds": central_issues.KINDS,
                   "kind_labels": central_issues.KIND_LABELS})


_PDF_COLUMNS = (
    # No severity column: the rows are already ordered most-severe-first, and on
    # paper a tone word can't be coloured, so it only spends width the Detail
    # column has better uses for.
    ("kind_label", "Issue", 1.3, False),
    ("device_name", "Device", 1.6, True),
    ("subject", "Item", 2.0, True),
    ("region", "Region", 1.0, False),
    # Detail carries the free text, so it is the one column with an unbounded
    # appetite — the highest weight means it absorbs the shortfall when the page
    # can't satisfy everyone, rather than starving the identifier columns.
    ("detail", "Detail", 3.2, False),
    # NOT mono: a rendered date is prose, not an identifier to align, and Courier
    # spends ~20pt more on it than Helvetica does — width Detail can use.
    ("since", "Since", 1.5, False),
)


def issues_pdf(h, qs):
    """The same list as `issues`, as a PDF the operator can file or hand over.

    Server-rendered (central/pdf.py, pure stdlib) rather than printed from the
    browser: what gets filed after a shift should be the rows the server actually
    holds, not whatever a print stylesheet made of the screen — and it has to work
    from a phone, where "print to PDF" is not a thing."""
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    rows = central_issues.collect(h.store, h.cfg, org)
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
    # Times are rendered in the OPERATOR's zone through the SAME choke point the
    # WhatsApp pages use. Central stores UTC and the dashboard localises in the
    # browser, so a page and a PDF are the only two places a stored timestamp
    # reaches a human with nothing to convert — and shipping raw is exactly how
    # every alert read 5h30m behind the Indian wall clock (see CLAUDE.md). A filed
    # report is read beside a wall clock, never beside a timezone.
    from wisp.egress.notifiers import _wa_time
    printed = [{**r, "since": _wa_time(r["since"]) if r["since"] else None}
               for r in rows]
    body = central_pdf.table_pdf(
        title=f"Open issues · {org}",
        subtitle=(f"{len(rows)} issue(s) · {which} · generated "
                  f"{_wa_time(stamp)}"),
        columns=columns, rows=printed,
        footer=f"WISP Central · {org}")
    # the org id is a validated token, but a filename lands in a response header —
    # so it is rebuilt from a safe alphabet here rather than trusted
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", org)[:40]
    h._send_binary(200, "application/pdf", body,
                   filename=f"issues-{safe}-{stamp[:10]}.pdf")


_XLSX_COLUMNS = (
    # Severity IS a column here, unlike the PDF: a spreadsheet is filtered and
    # sorted, and on paper the word couldn't be coloured but here it's the first
    # thing someone autofilters on.
    ("severity", "Severity", 12.0),
    ("kind_label", "Issue", 22.0),
    ("device_name", "Device", 24.0),
    ("subject", "Item", 34.0),
    ("region", "Region", 22.0),
    ("detail", "Detail", 70.0),
    ("since", "Since", 24.0),
)


def issues_xlsx(h, qs):
    """The same list as `issues`, as a real .xlsx (central/xlsx.py, pure stdlib).

    Not a CSV wearing the name: an operator asking for Excel wants to sort and
    filter, so the header freezes, the table carries an autofilter, and `since` is
    a real DATE cell — sorted by time rather than alphabetically, which text
    stamps get wrong the moment the month rolls over."""
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    rows = central_issues.collect(h.store, h.cfg, org)
    kinds = _kinds_arg(qs)
    if kinds:
        want = set(kinds)
        rows = [r for r in rows if r["kind"] in want]
    # `since` becomes a datetime in the operator's zone through the SAME
    # conversion the WhatsApp pages and the PDF use — one notion of "local".
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
    org = h.store.outage_org(oid)
    if not can_triage(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    ok = h.store.acknowledge_outage(org, oid, user["username"])
    h._reply(200 if ok else 404, {"ok": ok})


def assign(h, user, body):
    """Hand an open outage to one or more of the org's field accounts.

    OWNER-ONLY (`_can_write`), unlike acknowledge: deciding who goes out is
    running the org, and a worker re-pointing its own jobs is a different feature
    with its own conversation behind it. Assignment is an ASK, not an answer — it
    does NOT stamp the ack, so the outage keeps rendering as down until an
    assignee accepts (`accept`).

    The page goes to exactly the assignees (`named_whatsapp`), NOT the org
    audience: the point of naming two workers is that those two hear about it.
    The send is best-effort like every other — the assignment is already
    committed, so a WhatsApp failure reports `notified: 0` and never undoes it."""
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
        # No "assigned to nobody" state: re-assigning replaces the set, so an
        # empty list would be an ambiguous half-clear.
        h._reply(422, {"error": "name at least one account to assign to"})
        return
    # Only ACTIVE accounts of this org, resolved from the DB — never the body's
    # spelling, so an outage can't be handed to a username from another org.
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
    """Tell each assignee, with an [I'm on it] button where WhatsApp allows one.

    Two shapes for the same page, per recipient, because Meta only permits a
    free-form (and therefore buttoned) message inside the 24h window opened by
    that person's last inbound message. So: try the interactive one FIRST — a
    worker who can accept from the notification never has to open the dashboard,
    which is the whole point of naming them — and fall back to the approved
    `wisp_alert1` template for anyone whose window is shut. One message each
    either way; the template's own body already says what to do.

    Best-effort throughout: the assignment is committed before this runs, so
    every failure only lowers the `notified` count the owner is shown."""
    if not numbers:
        return 0
    from wisp.egress.notifiers import WhatsAppFacts
    body = (f"🔧 You have been assigned to the outage on *{device}*.\n"
            f"{detail}.\n\nTap *I'm on it* so the team knows you're going.")
    buttons = [(f"acc:{oid}", "✅ I'm on it")]
    if (row or {}).get("device_id"):
        # "where is it" is the assignee's next question — but only offer the
        # button when there is a device id behind it; a button that answers
        # "that isn't in your network" is worse than no button.
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
    """An assignee answering yes — the other half of `assign`.

    `can_triage`, not `_can_write`: accepting is exactly the triage right a
    worker already has (the store refuses anyone not named on the outage, which
    is the real gate). This is what moves the card to "in progress"; until it
    happens the outage renders as down and waiting, however many people were
    named.

    The owner who assigned it is told, on the same one-message-each discipline —
    they asked a question and the answer is the thing they are waiting for."""
    oid = int(body.get("outage_id") or 0)
    org = h.store.outage_org(oid)
    if org is None:
        h._reply(404, {"error": "no such outage"})
        return
    if not can_triage(user, org):
        h._reply(403, {"error": "forbidden"})
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
    """Best-effort "X accepted" back to whoever assigned it. Never the org
    audience: this answers one person's question."""
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
    org = h.store.outage_org(oid)
    if not can_triage(user, org):
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
             or "Bulk cleared, no post-mortem recorded")
    n = h.store.clear_pending_postmortems(org, cause)
    h._reply(200, {"ok": True, "cleared": n})
