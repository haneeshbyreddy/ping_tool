"""Device inventory routes: CRUD, placement, cable routes, regions, backup
links, switch ports, ONU/OLT optics, SNMP config/walks/profiles."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from wisp.central import (billing, inventory, onuroster, ponfault,
                          weboptics_profiles)
from wisp.central.api.common import (DENIED, body_org_write, device_read_scope,
                                     device_write_org, olt_liveness, org_or_400,
                                     q_int_required, reader_or_401)

log = logging.getLogger("wisp.central")


# ----- reads ---------------------------------------------------------------

def _stamp_optical_faults(h, org: str, devices: list[dict]) -> None:
    # Row chips for the OLT list: suspected fiber cuts and LIVE duplicate MACs,
    # the same verdicts the Optical tab and the Home KPI strip show — so the
    # Network list flags a troubled OLT without the tech drilling in. Pure
    # read-side; both verdicts ride the freshest-walk view (stale OLTs skipped),
    # so the chip and the drill-down never disagree. Non-fiber orgs pay just one
    # empty query (org_onu_rows short-circuits before any pure math runs).
    for d in devices:
        d["fiber_cuts"] = 0
        d["dup_macs"] = 0
    rows = h.store.org_onu_rows(org)
    if not rows:
        return
    now = datetime.now(timezone.utc)
    # Same liveness gate as the Home KPI strip (pon_summary): a down OLT's ICMP
    # outage owns its row and a probe-silent OLT is unknown — neither stamps a
    # fiber/dup verdict off its frozen last walk, so chip and strip never disagree.
    down_olts, stale_olts = olt_liveness(devices, now, h.cfg.central_node_stale_s)
    skip = down_olts | stale_olts
    rows = [r for r in rows if r["device_id"] not in skip]
    if not rows:
        return
    by_id = {d["id"]: d for d in devices}
    for f in ponfault.evaluate_org(rows, now):
        if f.kind == "fiber" and f.device_id in by_id:
            by_id[f.device_id]["fiber_cuts"] += 1
    # a MAC is a chip only when ≥2 slots are ONLINE at once (the paging rule);
    # dead-member dups are C-Data reg-table history, never a clone/loop. One
    # group can straddle two OLTs — chip every OLT it touches.
    for dm in onuroster.duplicate_macs(rows, now):
        if dm.online_members < 2:
            continue
        for dev_id in {m["device_id"] for m in dm.members}:
            if dev_id in by_id:
                by_id[dev_id]["dup_macs"] += 1


def list_devices(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    devices = h.store.list_org_devices(org)
    _stamp_optical_faults(h, org, devices)
    # tag colours ride the device list rather than a second GET: every consumer
    # of one needs the other in the same render, and they invalidate together
    h._reply(200, {"devices": devices, "tag_colors": h.store.org_colors(org, "tag")})


def regions(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    h._reply(200, {"regions": h.store.list_regions(org)})


def routes(h, qs):
    # map-only geometry, deliberately not folded into /api/inventory —
    # every page lists devices, only the map needs cable paths
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    h._reply(200, {"routes": h.store.list_link_routes(org)})


def ports(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    h._reply(200, {"ports": h.store.list_switch_ports(org, did)})


def link_ports(h, qs):
    # every port bound to a link (parent-side `feeds` + child-side `uplink`),
    # org-wide in one query — the map draws a bandwidth label per link off this
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    h._reply(200, {"ports": h.store.list_link_ports(org)})


def optics(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    dev = h.store.get_org_device(org, did) or {}
    now = datetime.now(timezone.utc)
    # onu_optics NEVER deletes removed-ONU rows (the roster-hygiene design keeps
    # them so last_online_at can freeze), so the raw table over-counts a PON with
    # deleted ONUs — "13/20" when only 13 slots still exist. Show the CURRENT
    # roster: the rows from this OLT's freshest walk, the same view capacity/
    # dup-MAC page from. stale_s=None keeps a stale-but-live OLT's last snapshot
    # visible (the panel flags freshness itself) instead of blanking the tab.
    onus = onuroster.current_roster(h.store.list_onu_optics(org, did), now,
                                    stale_s=None)
    # redundant-MAC groups are org-wide (a MAC cloned onto a second OLT is the
    # dangerous case); surface only the ones that touch THIS OLT in its panel
    dups = onuroster.duplicate_macs(h.store.org_onu_rows(org), now)
    dup_macs = [d.as_dict() for d in dups
                if any(m["device_id"] == did for m in d.members)]
    h._reply(200, {
        "onus": onus,
        "olt": h.store.get_olt_optics(org, did),
        "warn_dbm": dev.get("optical_warn_dbm") if dev.get("optical_warn_dbm") is not None else h.cfg.optical_warn_dbm,
        "crit_dbm": dev.get("optical_crit_dbm") if dev.get("optical_crit_dbm") is not None else h.cfg.optical_crit_dbm,
        "onu_pon_limit": dev.get("onu_pon_limit") if dev.get("onu_pon_limit") is not None else h.cfg.onu_pon_limit,
        "dup_macs": dup_macs,
    })


# A two-character needle matches half a fleet's MACs; the tech types the tail of
# a sticker, which is realistically 3+. Also the floor that keeps a bare "a" from
# scanning every ONU row on every keystroke of an unrelated name search.
ONU_SEARCH_MIN = 3
# Cap what one search ships. Past this the needle is too broad to be a MAC lookup
# anyway, and the answer is "type more", not a thousand-row payload.
ONU_SEARCH_MAX = 50


def onu_search(h, qs):
    """Find ONUs by serial/MAC **or name** substring, org-wide, grouped by OLT.

    Backs the Network page's device search: the identifiers a tech actually
    holds for a subscriber are the MAC off the sticker and the name the ONU was
    provisioned with, and until this endpoint neither reached anything in the
    dashboard — the roster was only visible once you already knew which OLT to
    open, which is the thing being looked up. Both fields go through the same
    punctuation-blind key, so "hc_kiran", "HC KIRAN" and "hckiran" all land, the
    way "a4:f2" and "a4f2" do. Read-only; never pages.
    """
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    needle = onuroster.search_key((qs.get("q") or [""])[0])
    if len(needle) < ONU_SEARCH_MIN:
        return h._reply(200, {"matches": [], "truncated": False})
    now = datetime.now(timezone.utc)
    matches: list[dict] = []
    shipped = 0
    truncated = False
    for did in h.store.onu_search_device_ids(org, needle):
        dev = h.store.get_org_device(org, did)
        if not dev:
            continue
        # Search the CURRENT roster, not the raw table. onu_optics never deletes
        # a removed ONU's row (that's what lets last_online_at freeze), so a raw
        # hit can be a slot that no longer exists — and clicking it would land on
        # an Optical tab that doesn't list it. stale_s=None is what that tab
        # itself renders, so search and drill-down can't disagree.
        roster = onuroster.current_roster(h.store.list_onu_optics(org, did), now,
                                          stale_s=None)
        hits = [o for o in roster
                if needle in onuroster.search_key(o.get("serial"))
                or needle in onuroster.search_key(o.get("name"))]
        if not hits:
            continue
        # Stable slot order the tech reads down — the Optical tab's rule, not a
        # relevance sort that reshuffles as the roster changes underneath.
        hits.sort(key=lambda o: (str(o.get("pon_port") or ""), o.get("onu_id") or 0,
                                 str(o.get("onu_key") or "")))
        room = ONU_SEARCH_MAX - shipped
        if len(hits) > room:
            hits = hits[:room]
            truncated = True
        shipped += len(hits)
        matches.append({
            "device_id": did,
            "device_name": dev.get("name") or "",
            "onus": [{
                "id": o.get("id"),
                "onu_key": o.get("onu_key"),
                "pon_port": o.get("pon_port"),
                "onu_id": o.get("onu_id"),
                "name": o.get("name"),
                "serial": o.get("serial"),
                "state": o.get("state"),
                # severity rides along so a result row colors with the SAME rule
                # the Optical tab uses — a MAC hit that reads "ok" here and
                # "crit" one click later would be its own little lie.
                "severity": o.get("severity"),
                "rx_dbm": o.get("rx_dbm"),
                "distance_m": o.get("distance_m"),
                "last_online_at": o.get("last_online_at"),
                "updated_at": o.get("updated_at"),
            } for o in hits],
        })
        if shipped >= ONU_SEARCH_MAX:
            truncated = True
            break
    matches.sort(key=lambda m: m["device_name"].lower())
    h._reply(200, {"matches": matches, "truncated": truncated})


def snmp_walks(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    h._reply(200, {"walks": h.store.list_snmp_walks(org, did)})


def snmp_walk_result(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    wid = q_int_required(h, qs, "id")
    if wid is None:
        return
    org = h.store.snmp_walk_org(wid)
    if org is None or not (user["is_superadmin"] or user["org_id"] == org):
        h._reply(403, {"error": "forbidden"})
        return
    h._reply(200, {"walk": h.store.get_snmp_walk(org, wid)})


def snmp_profiles(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = h._scope_org(user, qs)
    h._reply(200, {"profiles": h.store.list_snmp_profiles(org),
                   "metrics": list(inventory.PROFILE_METRICS),
                   "decodes": list(inventory.PROFILE_DECODES),
                   "selects": list(inventory.PROFILE_SELECTS)})


def snmp_status(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    h._reply(200, {"status": h.store.device_snmp_status(org, did),
                   "capability": h.store.device_capabilities(org, did)})


def rx_status(h, qs):
    """WHY this OLT has no per-ONU dBm — the optical counterpart of snmp_status.

    A blank Rx column has several completely different causes that look
    identical on screen, and they take opposite actions:

      * the vendor genuinely publishes no per-ONU Rx over SNMP (C-Data/DBC —
        proven exhaustively, twice) and there is no web-UI recipe for it yet;
      * there IS a recipe, but nobody has stored the OLT's web login;
      * everything is configured and the scrape is failing (wrong address,
        refused password, a firmware without that page);
      * or it simply works and this PON's ONUs are dark.

    Before this, all four rendered as an empty column, which is the exact false
    negative the whole web-scrape subsystem exists to kill — "this vendor has no
    Rx" concluded from a login that was never attempted. So the reply carries
    FACTS (does a profile exist, are there credentials, is the tunnel granted,
    what did the last scrape say) and the dashboard turns them into a sentence:
    the same split of duties SnmpDiagnosis already runs on.

    Pure read-side. Never triggers a scrape — a diagnosis page that pokes a weak
    OLT every time it renders is how the "must never look like polling" rule
    gets broken by accident.
    """
    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    dev = h.store.get_org_device(org, did) or {}
    # Which vendor this OLT resolved as, in the SAME precedence the pollers and
    # the sweeper use: the operator's dropdown beats the edge's detection.
    snmp = {s["subsystem"]: s for s in h.store.device_snmp_status(org, did)}
    optics = snmp.get("optics") or {}
    declared = str(dev.get("gpon_vendor") or "").strip().lower()
    detected = str(optics.get("profile") or "").strip().lower()
    # Detection only counts with a sysObjectID behind it — see web_optics_targets.
    if not (detected and str(optics.get("sysobjectid") or "").strip()):
        detected = ""
    vendor = declared or detected
    # Scoped to this org (global rows + its own), not the whole table: another
    # org's local vendor is none of this org's business, and `known_vendors`
    # ships straight to the page.
    profiles = weboptics_profiles.ProfileSet.build(
        h.store.list_web_optics_profiles(org))
    profile = profiles.resolve(org, vendor) if vendor else None
    creds = h.store.get_device_webui_credentials(org, did) or {}
    counts = h.store.onu_rx_counts(org, did)
    sweeper = getattr(h, "weboptics", None)
    h._reply(200, {
        "vendor": vendor or None,
        "vendor_source": "declared" if declared else ("detected" if detected else None),
        # Does a web-UI recipe exist for this vendor at all? The single fact
        # that separates "we can't read this box" from "nobody has told us how".
        "web_profile": profile.name if profile else None,
        "known_vendors": sorted(profiles.names()),
        "has_credentials": bool(creds.get("username") and creds.get("password_enc")),
        "web_proxy": h.store.org_web_proxy(org),
        "has_node": bool(dev.get("assigned_node_id")),
        "onus_total": counts["total"],
        "onus_rx": counts["with_rx"],
        "scrape": h.store.get_web_optics_status(org, did),
        # Is a read even possible here, and is one happening right now? Asked of
        # the SWEEPER, not re-derived from the facts above: the panel's Refresh
        # button and the route that serves it must agree with the sweep about
        # what is readable, and three copies of that rule would not.
        "can_refresh": bool(sweeper and sweeper.target(org, did)),
        "refreshing": bool(sweeper and sweeper.busy(did)),
    })


def rx_refresh(h, user, body):
    """Read this OLT's optical page NOW, instead of at the next sweep.

    The sweep's 15-minute clock is right for the thing it measures — Rx drifts
    over days — but it is wrong for the moment someone is standing at a pole
    with the fibre in their hand. A quarter-hour of "is it better yet?" is how a
    diagnosis turns into a second site visit, so the operator gets a button.
    The RESTRAINT stays where it was: same eligibility query, same per-OLT lock,
    same live-browse and dormant-tunnel gates, same recorded outcome. This
    widens WHO may ask for a read, not what a read is allowed to do.

    Owner-only, because it spends the stored web-UI credential down the tunnel —
    the same grade of action as opening a session (proxy._PROXY_ROLES), and a
    worker has neither.

    It answers immediately and scrapes on a thread: one OLT costs up to
    web_optics_device_budget_s (120s), and a request held that long is a browser
    timeout, a stuck spinner, and a worker thread this server does not have
    spare. The panel watches the recorded status instead — which is the same
    thing it reads when the sweep does the work, so there is one story about
    what happened rather than a special one for the button.
    """
    try:
        device_id = int(body.get("device_id"))
    except (TypeError, ValueError):
        h._reply(400, {"error": "device_id required"})
        return
    org = device_write_org(h, user, device_id)
    if org is DENIED:
        return
    if org is None:
        h._reply(404, {"error": "device not found"})
        return
    sweeper = getattr(h, "weboptics", None)
    if sweeper is None:
        h._reply(503, {"error": "web-UI optical reads are not enabled on this server"})
        return
    if sweeper.busy(device_id):
        # Not an error the operator caused, and not a state worth overwriting
        # the last outcome with — just say it's already happening.
        h._reply(409, {"error": "a read of this OLT is already running"})
        return
    # Refused HERE rather than on the thread, so an ineligible device gets a
    # real answer instead of a 200 followed by a status row that overwrites
    # whatever actually happened last with "you can't read this".
    dev = sweeper.target(org, device_id)
    if dev is None:
        h._reply(400, {"error": "this OLT isn't set up for web-UI optical reads "
                                "— see the Optical tab for what's missing"})
        return

    def _run() -> None:
        try:
            sweeper.scrape_device(dev)
        except Exception:
            log.exception("manual web-optics read failed for device=%d", device_id)

    threading.Thread(target=_run, name=f"wisp-rxrefresh-{device_id}",
                     daemon=True).start()
    log.info("manual web-optics read queued by user=%s for %s/device=%d",
             user["id"], org, device_id)
    h._reply(200, {"started": True})


def redundancy(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    h._reply(200, {"redundancy": h.store.device_redundancy_state(org, did)})


def perf(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    h._reply(200, {"perf": h.store.device_perf_state(org, did)})


def perf_samples(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    h._reply(200, {"samples": h.store.perf_sample_window(org, did)})


# ----- device CRUD -----------------------------------------------------------

def create(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    clean = inventory.clean_device_payload(
        body, parents=h.store.org_device_parent_map(org), device_id=None,
        registered_nodes=h.store.registered_node_ids(org),
        passive_ids=h.store.org_passive_ids(org))
    # Paywall device cap (central/billing.py) — counts probed devices only;
    # passive plant (splitter/FDB/closure) is documentation, never metered.
    # Enforced on CREATE only: a downgrade never breaks existing monitoring.
    if clean.get("device_type") not in inventory.PASSIVE_TYPES:
        plan = h.store.org_plan(org)
        cap = billing.device_cap(plan)
        if cap is not None and h.store.org_monitored_device_count(
                org, inventory.PASSIVE_TYPES) >= cap:
            label = billing.PLANS[plan]["label"]
            upgrade = ("upgrade to Pro or VIP for more"
                       if plan == "free" else "upgrade to VIP for unlimited devices")
            h._reply(422, {"error": f"{label} plan is limited to {cap} monitored "
                                    f"devices — {upgrade} (Settings → Plan & billing)"})
            return
    did = h.store.create_org_device(org, clean)
    h._reply(200, {"id": did})


def update(h, user, body):
    did = int(body.get("id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    parents = h.store.org_device_parent_map(org)
    clean = inventory.clean_device_payload(
        body, parents=parents, device_id=did,
        registered_nodes=h.store.registered_node_ids(org),
        passive_ids=h.store.org_passive_ids(org))
    ok = h.store.update_org_device(org, did, clean)
    h._reply(200 if ok else 404, {"ok": ok})


def delete(h, user, body):
    did = int(body.get("id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    result = h.store.delete_org_device(org, did)
    h._reply(200 if result["ok"] else 409, result)


def maintenance(h, user, body):
    did = int(body.get("id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    ok = h.store.set_org_device_maintenance(org, did, bool(body.get("on")))
    h._reply(200 if ok else 404, {"ok": ok})


def tag_color(h, user, body):
    """Colour-code a tag. Presentation only — a tag has no row of its own, so
    this keys on the text; renaming a tag on every device orphans the colour."""
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    tag = inventory.clean_color_key("tag", body.get("tag"))
    h.store.set_org_color(org, "tag", tag, inventory.clean_color(body.get("color")))
    h._reply(200, {"ok": True})


def tree_detached(h, user, body):
    did = int(body.get("id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    ok = h.store.set_org_device_tree_detached(org, did, bool(body.get("on")))
    h._reply(200 if ok else 404, {"ok": ok})


def location(h, user, body):
    did = int(body.get("id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    loc = inventory.clean_location_payload(body)
    ok = h.store.set_org_device_location(org, did, loc["lat"], loc["lng"])
    h._reply(200 if ok else 404, {"ok": ok})


def _link_write_scope(h, user, clean):
    """Resolve the org for a map-presentation write and prove the link is real.

    Returns the org, or DENIED when a reply has already been sent. Presentation
    (geometry, colour, label position) only ever attaches to a link that exists
    in this org — primary, backup or cross-link.

    A cross-link matches in EITHER order: org_device_links canonicalizes a peer
    to (min, max) but link_routes keys it (child=higher, parent=lower) so the
    waypoints still run parent→child like every other kind. Peers used to be
    rejected outright here, so a drawn route on a cross-link 400'd even though
    the map offered the editor."""
    org = device_write_org(h, user, clean["child_id"])
    if org is DENIED:
        return DENIED
    child = h.store.get_org_device(org, clean["child_id"])
    if not child:
        h._reply(404, {"error": "device not found"})
        return DENIED
    if child.get("parent_device_id") == clean["parent_id"]:
        return org
    linked = {e["parent_id"] for e in h.store.org_device_backup_edges(org)
              if e["child_id"] == clean["child_id"]}
    linked |= h.store.org_device_peer_map(org).get(clean["child_id"], set())
    if clean["parent_id"] not in linked:
        raise inventory.InventoryError(
            "no link between those devices — set the parent first")
    return org


def route(h, user, body):
    clean = inventory.clean_route_payload(body)
    org = _link_write_scope(h, user, clean)
    if org is DENIED:
        return
    h.store.set_link_route(org, clean["child_id"], clean["parent_id"],
                           clean["waypoints"], updated_by=user["username"])
    h._reply(200, {"ok": True})


def link_style(h, user, body):
    """Per-link map styling: colour from the closed palette, label position along
    the line. Owner-gated like every inventory write; purely cartographic — it
    can't reach the engine, an alert or a state row."""
    clean = inventory.clean_link_style_payload(body)
    org = _link_write_scope(h, user, clean)
    if org is DENIED:
        return
    h.store.set_link_style(org, clean["child_id"], clean["parent_id"],
                           clean["fields"], updated_by=user["username"])
    h._reply(200, {"ok": True})


def snmp(h, user, body):
    did = int(body.get("id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    clean = inventory.clean_snmp_payload(body)
    ok = h.store.set_org_device_snmp(org, did, clean)
    h._reply(200 if ok else 404, {"ok": ok})


def web_access(h, user, body):
    """Set/clear a device's web-UI proxy address override (owner-gated, like every
    inventory write). When the device's admin page isn't at ip_address:80/443
    (port-forwarding / a separate mgmt IP), the owner declares where it lives so
    'Open web UI' tunnels there instead."""
    did = int(body.get("id") or body.get("device_id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    dev = h.store.get_org_device(org, did) if org is not None else None
    if not dev:
        h._reply(404, {"ok": False, "error": "device not found"})
        return
    clean = inventory.clean_web_access_payload(body)
    # Drop a redundant override (same IP on 80/443, or a bare scheme) to NULL so
    # it never pins a scheme and steals the http/https fallback — see
    # inventory.normalize_web_access.
    clean = inventory.normalize_web_access(clean, dev.get("ip_address"))
    ok = h.store.set_org_device_web_access(
        org, did, web_ip=clean["web_ip"], web_port=clean["web_port"],
        web_scheme=clean["web_scheme"])
    h._reply(200 if ok else 404, {"ok": ok})


def capability(h, user, body):
    clean = inventory.clean_capability_payload(body)
    org = device_write_org(h, user, clean["device_id"])
    if org is DENIED:
        return
    ok = h.store.set_device_capability(
        org, clean["device_id"], clean["subsystem"], clean["supported"],
        clean["note"], updated_by=user["username"])
    h._reply(200 if ok else 404, {"ok": ok})


# ----- device web-UI credentials -------------------------------------------
# Owner-only, like every other inventory write (the SNMP community string is a
# device credential too and gates the same way). The stored password is never
# returned to the browser — only whether one is set.

def webui_credentials(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    did = q_int_required(h, qs, "device_id")
    if did is None:
        return
    org = device_write_org(h, user, did)   # owner-only; 403 already sent on deny
    if org is DENIED:
        return
    row = h.store.get_device_webui_credentials(org, did) or {}
    h._reply(200, {"credentials": {
        "username": row.get("username") or "",
        "has_password": bool(row.get("password_enc")),
        "auth_mode": row.get("auth_mode") or "form",
        "updated_by": row.get("updated_by"),
        "updated_at": row.get("updated_at"),
    }})


def webui_credentials_set(h, user, body):
    did = int(body.get("device_id") or body.get("id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    username = str(body.get("username") or "").strip()[:128]
    # form-login is the default — most switch/OLT web UIs are a login form; Basic
    # (the HTTP popup) is the opt-in.
    auth_mode = "basic" if str(body.get("auth_mode") or "").lower() == "basic" else "form"
    # Password semantics are explicit so a username-only edit never wipes a
    # stored password: key absent / null -> leave the stored password untouched;
    # "" -> clear it; a non-empty string -> encrypt and store.
    raw = body.get("password", None)
    set_password = raw is not None
    password_enc = None
    if set_password and raw != "":
        password_enc = h.secretbox.encrypt(str(raw)[:512])
    ok = h.store.set_device_webui_credentials(
        org, did, username=username, password_enc=password_enc,
        set_password=set_password, auth_mode=auth_mode, updated_by=user["username"])
    h._reply(200 if ok else 404, {"ok": ok})


def webui_credentials_clear(h, user, body):
    did = int(body.get("device_id") or body.get("id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    ok = h.store.clear_device_webui_credentials(org, did)
    h._reply(200 if ok else 404, {"ok": ok})


def snmp_walk_create(h, user, body):
    did = int(body.get("device_id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    device = h.store.get_org_device(org, did)
    if not device:
        h._reply(404, {"error": "device not found"})
        return
    if not device.get("snmp_enabled") or not device.get("snmp_community"):
        raise inventory.InventoryError(
            "enable SNMP (with a community) on this device first")
    node = device.get("assigned_node_id")
    if not node:
        raise inventory.InventoryError(
            "assign this device to a probe first — the walk runs from "
            "its assigned node")
    clean = inventory.clean_walk_payload(body)
    wid = h.store.create_snmp_walk(org, did, node, clean["root_oid"],
                                   clean["max_varbinds"],
                                   requested_by=user["username"])
    h._reply(200, {"id": wid})


# ----- SNMP profiles ---------------------------------------------------------

def profile_create(h, user, body):
    clean = inventory.clean_profile_payload(body)
    # org_id NULL = a GLOBAL profile every org's edges receive —
    # superadmin only. An org owner creates org-local ones.
    if user["is_superadmin"]:
        org = body.get("org_id") or None
    else:
        org = user["org_id"]
    if org is not None and not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    pid = h.store.create_snmp_profile(org, clean)
    h._reply(200, {"id": pid})


def _profile_mutate(h, user, body, *, delete: bool):
    profile = h.store.get_snmp_profile(int(body.get("id") or 0))
    if not profile:
        h._reply(404, {"error": "profile not found"})
        return
    org = profile["org_id"]
    allowed = (user["is_superadmin"] if org is None
               else h._can_write(user, org))
    if not allowed:
        h._reply(403, {"error": "forbidden"})
        return
    if delete:
        ok = h.store.delete_snmp_profile(profile["id"])
    else:
        clean = inventory.clean_profile_payload(body)
        ok = h.store.update_snmp_profile(profile["id"], clean)
    h._reply(200 if ok else 404, {"ok": ok})


def profile_update(h, user, body):
    _profile_mutate(h, user, body, delete=False)


def profile_delete(h, user, body):
    _profile_mutate(h, user, body, delete=True)


# ----- GPON vendor profiles (optics counterpart, same auth shape) -------------

def gpon_profiles(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = h._scope_org(user, qs)
    h._reply(200, {"profiles": h.store.list_gpon_profiles(org),
                   "oid_fields": list(inventory.GPON_PROFILE_OIDS),
                   "states": list(inventory.GPON_PROFILE_STATES),
                   "pon_index_strategies": list(inventory.GPON_PON_INDEX_STRATEGIES)})


def gpon_profile_create(h, user, body):
    clean = inventory.clean_gpon_profile_payload(body)
    # org_id NULL = a GLOBAL profile every org's edges receive —
    # superadmin only. An org owner creates org-local ones.
    if user["is_superadmin"]:
        org = body.get("org_id") or None
    else:
        org = user["org_id"]
    if org is not None and not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    pid = h.store.create_gpon_profile(org, clean)
    h._reply(200, {"id": pid})


def _gpon_profile_mutate(h, user, body, *, delete: bool):
    profile = h.store.get_gpon_profile(int(body.get("id") or 0))
    if not profile:
        h._reply(404, {"error": "profile not found"})
        return
    org = profile["org_id"]
    allowed = (user["is_superadmin"] if org is None
               else h._can_write(user, org))
    if not allowed:
        h._reply(403, {"error": "forbidden"})
        return
    if delete:
        ok = h.store.delete_gpon_profile(profile["id"])
    else:
        clean = inventory.clean_gpon_profile_payload(body)
        ok = h.store.update_gpon_profile(profile["id"], clean)
    h._reply(200 if ok else 404, {"ok": ok})


def gpon_profile_update(h, user, body):
    _gpon_profile_mutate(h, user, body, delete=False)


def gpon_profile_delete(h, user, body):
    _gpon_profile_mutate(h, user, body, delete=True)


# ----- web-UI optics vendor profiles ----------------------------------------
# The third profile table, and the one that decides whether a vendor whose Rx
# lives ONLY on its web page can be read at all. Same shape as the two above so
# there is one thing to learn: org_id NULL is global (superadmin), an org owner
# adds org-local rows, and the whole payload is refused rather than partially
# applied — see central/weboptics_profiles.py for why a half-understood recipe
# is worse than none.

def web_optics_profiles(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = h._scope_org(user, qs)
    h._reply(200, {
        "profiles": h.store.list_web_optics_profiles(org),
        "builtins": list(weboptics_profiles.builtin_names()),
        # The closed vocabulary, served rather than duplicated in the SPA: a
        # dropdown built from a second hand-kept list is how a value that the
        # validator rejects ends up being offered in the UI.
        "fields": list(weboptics_profiles.FIELDS),
        "sessions": list(weboptics_profiles.SESSION_STRATEGIES),
        "methods": list(weboptics_profiles.OPTICS_METHODS),
        "charsets": list(weboptics_profiles.CHARSETS),
        "onu_id_shapes": list(weboptics_profiles.ONU_ID_SHAPES),
        # One worked example, so "what does a real one look like?" is answerable
        # from inside the dashboard instead of from the source.
        "example": weboptics_profiles.BUILTIN_SPECS.get("dbc", {}),
    })


def web_optics_profile_create(h, user, body):
    clean = weboptics_profiles.clean_web_optics_profile_payload(body)
    if user["is_superadmin"]:
        org = body.get("org_id") or None
    else:
        org = user["org_id"]
    if org is not None and not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    h._reply(200, {"id": h.store.create_web_optics_profile(org, clean)})


def _web_optics_profile_mutate(h, user, body, *, delete: bool):
    profile = h.store.get_web_optics_profile(int(body.get("id") or 0))
    if not profile:
        h._reply(404, {"error": "profile not found"})
        return
    org = profile["org_id"]
    allowed = (user["is_superadmin"] if org is None else h._can_write(user, org))
    if not allowed:
        h._reply(403, {"error": "forbidden"})
        return
    if delete:
        ok = h.store.delete_web_optics_profile(profile["id"])
    else:
        clean = weboptics_profiles.clean_web_optics_profile_payload(body)
        ok = h.store.update_web_optics_profile(profile["id"], clean)
    h._reply(200 if ok else 404, {"ok": ok})


def web_optics_profile_update(h, user, body):
    _web_optics_profile_mutate(h, user, body, delete=False)


def web_optics_profile_delete(h, user, body):
    _web_optics_profile_mutate(h, user, body, delete=True)


# ----- switch ports ----------------------------------------------------------

def port_monitored(h, user, body):
    pid = int(body.get("id") or 0)
    org = h.store.switch_port_org(pid)
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    ok = h.store.set_port_monitored(org, pid, bool(body.get("on")))
    h._reply(200 if ok else 404, {"ok": ok})


def port_feeds(h, user, body):
    pid = int(body.get("id") or 0)
    org = h.store.switch_port_org(pid)
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    feeds_raw = body.get("feeds_device_id")
    feeds = None
    if feeds_raw not in (None, "", "null"):
        try:
            feeds = int(feeds_raw)
        except (TypeError, ValueError):
            h._reply(422, {"error": "feeds_device_id must be a number"})
            return
        if h.store.device_org(feeds) != org:
            h._reply(422, {"error": "feeds device must belong to the same org"})
            return
    ok = h.store.set_port_feeds(org, pid, feeds)
    h._reply(200 if ok else 404, {"ok": ok})


def port_uplink(h, user, body):
    # the child-side mirror of port_feeds: THIS port faces that parent device
    pid = int(body.get("id") or 0)
    org = h.store.switch_port_org(pid)
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    uplink_raw = body.get("uplink_device_id")
    uplink = None
    if uplink_raw not in (None, "", "null"):
        try:
            uplink = int(uplink_raw)
        except (TypeError, ValueError):
            h._reply(422, {"error": "uplink_device_id must be a number"})
            return
        if h.store.device_org(uplink) != org:
            h._reply(422, {"error": "uplink device must belong to the same org"})
            return
    ok = h.store.set_port_uplink(org, pid, uplink)
    h._reply(200 if ok else 404, {"ok": ok})


def port_bandwidth(h, user, body):
    pid = int(body.get("id") or 0)
    org = h.store.switch_port_org(pid)
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    clean = inventory.clean_port_bandwidth_payload(body)
    ok = h.store.set_port_bandwidth_config(
        org, pid, clean["threshold_mbps"], clean["direction"],
        clean["max_mbps"])
    h._reply(200 if ok else 404, {"ok": ok})


# ----- optics ----------------------------------------------------------------

def optics_ack(h, user, body):
    onu_id = int(body.get("id") or 0)
    org = h.store.onu_optics_org(onu_id)
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    until = inventory.clean_ack_until(body)
    ok = h.store.set_onu_ack(org, onu_id, until)
    h._reply(200 if ok else 404, {"ok": ok})


def optics_thresholds(h, user, body):
    did = int(body.get("device_id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    clean = inventory.clean_optical_thresholds(body)
    ok = h.store.set_olt_optical_thresholds(
        org, did, clean["warn_dbm"], clean["crit_dbm"], clean["onu_pon_limit"])
    h._reply(200 if ok else 404, {"ok": ok})


# ----- backup links ----------------------------------------------------------

def link_add(h, user, body):
    child_id = int(body.get("child_id") or 0)
    parent_id = int(body.get("parent_id") or 0)
    org = device_write_org(h, user, child_id)
    if org is DENIED:
        return
    if h.store.device_org(parent_id) != org:
        h._reply(422, {"error": "backup parent must belong to the same org"})
        return
    parents = h.store.org_device_parent_map(org)
    backups = h.store.org_device_backup_map(org)
    inventory.clean_backup_link(child_id, parent_id, parents=parents,
                                backups=backups)
    h.store.create_backup_link(org, child_id, parent_id)
    h._reply(200, {"ok": True})


def link_delete(h, user, body):
    child_id = int(body.get("child_id") or 0)
    parent_id = int(body.get("parent_id") or 0)
    org = device_write_org(h, user, child_id)
    if org is DENIED:
        return
    ok = h.store.delete_backup_link(org, child_id, parent_id)
    h._reply(200 if ok else 404, {"ok": ok})


# ----- peer (cross) links ----------------------------------------------------
# Undirected switch-to-switch cabling. Same table as backup links under
# kind='peer', invisible to the engine by construction (see store_devices.py).

def peer_add(h, user, body):
    a_id = int(body.get("a_id") or 0)
    b_id = int(body.get("b_id") or 0)
    org = device_write_org(h, user, a_id)
    if org is DENIED:
        return
    if h.store.device_org(b_id) != org:
        h._reply(422, {"error": "the other device must belong to the same org"})
        return
    inventory.clean_peer_link(a_id, b_id,
                             parents=h.store.org_device_parent_map(org),
                             backups=h.store.org_device_backup_map(org),
                             peers=h.store.org_device_peer_map(org))
    h.store.create_peer_link(org, a_id, b_id)
    h._reply(200, {"ok": True})


def peer_delete(h, user, body):
    a_id = int(body.get("a_id") or 0)
    b_id = int(body.get("b_id") or 0)
    org = device_write_org(h, user, a_id)
    if org is DENIED:
        return
    ok = h.store.delete_peer_link(org, a_id, b_id)
    h._reply(200 if ok else 404, {"ok": ok})


# ----- regions ---------------------------------------------------------------

def region_add(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    h.store.add_region(org, inventory.clean_region_name(body.get("name")))
    h._reply(200, {"ok": True})


def region_rename(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    old = inventory.clean_region_name(body.get("old"))
    new = inventory.clean_region_name(body.get("new"))
    h.store.rename_region(org, old, new)
    h._reply(200, {"ok": True})


def region_delete(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    result = h.store.delete_region(
        org, inventory.clean_region_name(body.get("name")))
    h._reply(200 if result["ok"] else 409, result)
