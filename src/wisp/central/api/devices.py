"""Device inventory routes: CRUD, placement, cable routes, regions, backup
links, switch ports, ONU/OLT optics, SNMP config/walks/profiles."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from wisp.central import (assignment, billing, drops, inventory, onuroster,
                          ponfault, weboptics_profiles)
from wisp.central.api.common import (DENIED, body_org_write, can_survey,
                                     device_read_scope, device_write_org,
                                     olt_liveness, org_or_400, q_int_or,
                                     q_int_required, reader_or_401,
                                     survey_write_org)

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
    for f in ponfault.evaluate_org(rows, now,
                                   witness_macs=h.store.onu_place_macs(org)):
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
    # Which of these are placed reference points. Folded in here rather than
    # fetched separately so the row's pin state can never lag its own roster —
    # and by MAC, the key the placement is stored under.
    placed = {p["mac"]: p for p in h.store.list_onu_places(org)}
    # …and which passive each one's drop comes off. Folded in here for the same
    # reason: the roster row and its recorded splitter must arrive together, or
    # the tab could show a subscriber the plant record has already moved. Only
    # the id ships — the SPA already holds the device list and resolving the
    # name there keeps this reply from growing a second copy of it.
    attached = h.store.onu_drop_map(org)
    for o in onus:
        mac = onuroster._norm_mac(o.get("serial"))
        p = placed.get(mac)
        o["place"] = ({"lat": p["lat"], "lng": p["lng"], "label": p["label"]}
                      if p else None)
        o["drop_passive_id"] = attached.get(mac)
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
        # The OPERATOR's name (`onu_places.label`, joined onto every roster row)
        # is searched beside the walked one. It is often the ONLY name a
        # subscriber has — the C-Data fleet walks a blank `name` column — so
        # matching just the OLT's string answered "no such subscriber" about a
        # drop a tech had stood at, named, and was now looking up.
        hits = [o for o in roster
                if needle in onuroster.search_key(o.get("serial"))
                or needle in onuroster.search_key(o.get("name"))
                or needle in onuroster.search_key(o.get("label"))]
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
                # the operator's own name, so a result row is titled the way the
                # Optical tab titles the same ONU one click later
                "label": o.get("label"),
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

def _gpon_vendor_names(h, org: str) -> set[str]:
    """Every vendor name this org's OLTs may carry, from `gpon_profiles`. The
    edge built-ins are added by the validator itself.

    DISABLED rows count. A profile switched off is a tombstone, not an absence
    (same rule the web-optics profiles keep) — dropping it here would make every
    device stamped with that vendor unsavable the moment somebody unticked it,
    and the fix for a wrong vendor is to change the vendor, never to be locked
    out of the form that changes it."""
    return {p["name"] for p in h.store.list_gpon_profiles(org) if p.get("name")}


def create(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    clean = inventory.clean_device_payload(
        body, parents=h.store.org_device_parent_map(org), device_id=None,
        registered_nodes=h.store.registered_node_ids(org),
        passive_ids=h.store.org_passive_ids(org),
        gpon_vendors=_gpon_vendor_names(h, org))
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
        passive_ids=h.store.org_passive_ids(org),
        gpon_vendors=_gpon_vendor_names(h, org))
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


def field_location(h, user, body):
    """A placement taken standing at the device — the worker-reachable one.

    Split from `location` rather than widened into it because the two differ in
    what they may DO, not just who may call them: this one cannot clear a pin
    (see `clean_field_location_payload`) and always stamps provenance, while the
    desktop route can clear and deliberately wipes it. One route with a role
    branch inside would put "may a worker delete plant from the map" one `if`
    away from being wrong."""
    did = int(body.get("id") or 0)
    org = survey_write_org(h, user, did)
    if org is DENIED:
        return
    loc = inventory.clean_field_location_payload(body)
    ok = h.store.place_org_device(
        org, did, loc["lat"], loc["lng"], accuracy_m=loc["accuracy_m"],
        source=loc["source"], placed_by=user["username"])
    h._reply(200 if ok else 404, {"ok": ok})


def field_passive(h, user, body):
    """Passive plant discovered in the field: created AT a fix, with no parent.

    The device cap is not consulted — passives never count against it (same rule
    `create` applies), so a survey can record the plant it finds without a plan
    limit turning into a half-mapped network."""
    org = body.get("org_id") or user["org_id"]
    if not can_survey(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    clean = inventory.clean_field_passive_payload(body)
    did = h.store.create_org_device(org, clean)
    h.store.place_org_device(
        org, did, clean["lat"], clean["lng"], accuracy_m=clean["accuracy_m"],
        source=clean["source"], placed_by=user["username"])
    h._reply(200, {"id": did})


def onu_coverage(h, qs):
    """How much of the subscriber base has a location yet, per OLT.

    The survey's headline number. It exists because "unplaced" for equipment and
    "unplaced" for subscribers are different sizes of problem: a fleet has tens
    of boxes and thousands of drops, so a survey counter that only knew about
    `org_devices` read "0 left" the moment the gear was done — while 2,155 of
    2,156 subscribers had no pin. A coverage figure nobody can see is a survey
    nobody finishes.

    Per-OLT because that is how a field walk is actually organised: a tech works
    one PON area at a time. `?device_id=` adds that OLT's UNPLACED rows, so the
    list a worker pulls up is only ever one OLT deep — the full fleet's unplaced
    set is thousands of rows and has no business crossing a handset's connection.

    Counted over the FRESHEST-walk roster view (`current_roster`, staleness-blind)
    for the same reason the Optical tab and ONU search use it: `onu_optics` never
    deletes a vacated slot, so a raw count would set the denominator to include
    zombies the tech can never find. Read-only."""
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    now = datetime.now(timezone.utc)
    roster = onuroster.current_roster(h.store.org_onu_rows(org), now, stale_s=None)
    placed = h.store.onu_place_macs(org, witness_only=False)

    olts: dict[int, dict] = {}
    for r in roster:
        did = r.get("device_id")
        if did is None:
            continue
        o = olts.setdefault(did, {"device_id": did,
                                  "device_name": r.get("device_name"),
                                  "total": 0, "placed": 0})
        o["total"] += 1
        if onuroster._norm_mac(r.get("serial")) in placed:
            o["placed"] += 1

    want = q_int_or(qs, "device_id", 0)
    unplaced = []
    if want:
        for r in roster:
            if r.get("device_id") != want:
                continue
            mac = onuroster._norm_mac(r.get("serial"))
            if not mac or mac in placed:
                continue
            unplaced.append({"mac": mac, "name": r.get("name"),
                             "pon_port": r.get("pon_port"),
                             "onu_id": r.get("onu_id"),
                             "state": r.get("state"),
                             "device_id": want,
                             "device_name": r.get("device_name")})
        # Slot order, not whichever ONUs happen to be up — a tech reads down a
        # stable list and a shuffled one loses their place between visits.
        unplaced.sort(key=lambda x: (x["pon_port"] or "", x["onu_id"] or 0))

    h._reply(200, {
        "total": sum(o["total"] for o in olts.values()),
        "placed": sum(o["placed"] for o in olts.values()),
        "olts": sorted(olts.values(), key=lambda o: (o["device_name"] or "")),
        "unplaced": unplaced,
    })


def field_onu(h, user, body):
    """Locate a subscriber's ONU from the field. Worker-reachable.

    Two refusals carry this route:

    It never CREATES a witness — `clean_field_onu_payload` has no key for it, so
    a survey pin is a location and nothing more. And it never DESTROYS one: if
    the operator had already vouched for this subscriber's power, the flag is
    preserved rather than reset, because that claim is invisible on a handset and
    silently cancelling it would flip a PON verdict from "area power cut" to
    "fibre cut" — rolling a splicing crew for the DISCOM, the exact failure the
    reference-ONU feature exists to prevent.

    The ONU must be in the roster. A scrape can never add an ONU and neither can
    this: a pin on a MAC no walk has ever seen would render at a coordinate with
    nothing behind it, and typo'd stickers are common enough that "we'll show it
    anyway" means showing fiction."""
    org = body.get("org_id") or user["org_id"]
    if not can_survey(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    if not org:
        h._reply(400, {"error": "org_id is required to locate an ONU"})
        return
    clean = inventory.clean_field_onu_payload(body)
    known = {onuroster._norm_mac(r.get("serial"))
             for r in h.store.org_onu_rows(org)}
    if clean["mac"] not in known:
        h._reply(404, {"error": "no ONU with that MAC is in this org's roster"})
        return
    ok = h.store.place_onu_in_field(
        org, clean["mac"], clean["lat"], clean["lng"],
        witness=h.store.onu_place_witness(org, clean["mac"]) is True,
        accuracy_m=clean["accuracy_m"], source=clean["source"],
        placed_by=user["username"], label=clean["label"])
    h._reply(200, {"ok": ok})


def field_onu_name(h, user, body):
    """Name a located subscriber, from the field. Worker-reachable.

    The name goes to `onu_places.label`, NOT `onu_optics.name`. The roster's name
    is whatever the OLT reports and the SNMP upsert rewrites it (`name=
    excluded.name`) on every sweep — so a name typed here would vanish inside
    ~300s, which is worse than not offering the field at all. The label is
    operator-owned and no walk touches it.

    Separate from `field_onu` so a rename cannot restamp the pin's provenance:
    correcting a spelling must not downgrade a 6 m GPS fix to a hand-placed
    point, nor reattribute the placement to whoever fixed the typo."""
    org = body.get("org_id") or user["org_id"]
    if not can_survey(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    if not org:
        h._reply(400, {"error": "org_id is required"})
        return
    clean = inventory.clean_field_onu_name_payload(body)
    ok = h.store.set_onu_place_label(org, clean["mac"], clean["label"])
    # 404 rather than creating a row: a name with no location is not a placement,
    # and inventing a pin-less one would put a subscriber in the coverage count
    # that nobody has actually been to.
    h._reply(200 if ok else 404,
             {"ok": ok} if ok else {"error": "that subscriber has no location yet"})


def onu_places(h, qs):
    """Every reference ONU this org has placed, joined to the CURRENT roster.

    The join is what makes the map honest about a placement whose ONU no longer
    exists: a swapped (RMA'd) box changes MAC, so its row survives pointing at
    nothing, and the operator has to see that rather than a pin that quietly
    stopped being a witness. `matched` false says exactly that.

    `ambiguous` is the other half. C-Data reg tables hand one MAC to more than
    one live slot, and a reference point standing on two of them cannot be said
    to be at one OLT — so the row reports the count instead of picking a winner.
    The VERDICT is unaffected: ponfault matches by MAC inside each PON, where two
    live slots really are two witnesses. Read-only."""
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    now = datetime.now(timezone.utc)
    # Same staleness-blind roster view the Optical tab and ONU search use, so a
    # reference point can't be listed here and missing there (or the reverse).
    roster = onuroster.current_roster(h.store.org_onu_rows(org), now, stale_s=None)
    by_mac: dict[str, list[dict]] = {}
    for r in roster:
        mac = onuroster._norm_mac(r.get("serial"))
        if mac:
            by_mac.setdefault(mac, []).append(r)
    resolved = []
    for p in h.store.list_onu_places(org):
        hits = by_mac.get(p["mac"], [])
        r = hits[0] if len(hits) == 1 else {}
        resolved.append((p, hits, r))
    # A C-Data EPON OLT gives every ONU its own ifTable row, so a reference point
    # can carry a REAL per-subscriber bit rate — not the PON aggregate, which
    # would print one number across every ONU on the PON. Looked up only for the
    # OLTs these few places resolved to. Absent on other vendors by construction
    # (see onuroster.onu_if_token); the reply then simply carries no rate.
    ifaces = h.store.onu_interfaces(
        org, {r["device_id"] for _, _, r in resolved if r.get("device_id")})
    # Which passive this reference point's drop comes off, so the map can draw
    # the line to its SPLITTER rather than straight to the OLT — the straight
    # line skipped every splitter between them, which is the plant a crew works
    # on. Null = nobody recorded one, and the map renders that difference.
    attached = h.store.onu_drop_map(org)
    out = []
    for p, hits, r in resolved:
        token = onuroster.onu_if_token(r.get("pon_port"), r.get("onu_id"))
        port = ifaces.get((r.get("device_id"), token)) if token else None
        out.append({**p,
                    "matched": bool(hits),
                    # SQLite hands `witness` back as 0/1 and `**p` shipped that
                    # raw, so the SPA's declared boolean was a lie — and the two
                    # ways JS reads an int are both wrong: `w === true` is never
                    # true (the survey list's "reference" chip could not render,
                    # the one warning that stops a witness being re-pinned), and
                    # `{w && <Chip/>}` renders a literal "0" beside the name.
                    # Cast at the edge, where the type is declared.
                    "witness": bool(p.get("witness")),
                    "ambiguous": len(hits) > 1,
                    "slots": len(hits),
                    "drop_passive_id": attached.get(p["mac"]),
                    "device_id": r.get("device_id"),
                    "device_name": r.get("device_name"),
                    "onu_id": r.get("onu_id"),
                    "pon_port": r.get("pon_port"),
                    "name": r.get("name"),
                    "state": r.get("state"),
                    "rx_dbm": r.get("rx_dbm"),
                    # the ONU's OWN interface. `port_state` is a SECOND opinion
                    # on a different clock from `state` above (the optical
                    # roster) — they agreed on 1542 of 1557 live rows, and where
                    # they don't, the roster still owns whether the ONU is up.
                    "if_name": port["if_name"] if port else None,
                    "port_state": port["oper_status"] if port else None,
                    "in_bps": port["in_bps"] if port else None,
                    "out_bps": port["out_bps"] if port else None,
                    "port_updated_at": port["updated_at"] if port else None})
    h._reply(200, {"places": out})


def _resolved_drops(h, org: str, now):
    """Recorded drops joined to the current roster, plus the parent map.

    One helper because the two drop reads must never disagree about what is
    attached where — a splitter's own panel listing a subscriber the map's
    rollup didn't count is the drill-down-disagrees-with-the-tile failure, one
    level down."""
    roster = onuroster.current_roster(h.store.org_onu_rows(org), now,
                                      stale_s=None)
    resolved = drops.resolve_drops(roster, h.store.onu_drop_map(org),
                                   witness_macs=h.store.onu_place_macs(org))
    return resolved, roster


def onu_drops(h, qs):
    """Per-splitter subscriber load and branch-fault verdicts, org-wide.

    Map-only, like `/api/inventory/routes` — every page lists devices, only the
    map (and a splitter's own panel) needs to know what hangs off each box.
    Read-side: nothing here writes, and nothing pages.
    """
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    now = datetime.now(timezone.utc)
    resolved, roster = _resolved_drops(h, org, now)
    loads = drops.splitter_loads(resolved)
    # The fault verdict is gated on the SAME liveness split the optical KPI strip
    # and the fiber-cut chips use: a down OLT's ICMP outage owns its page, and a
    # probe-silent one is unknown rather than dark. Without this a dead edge
    # would paint every branch behind it as a cut.
    devices = h.store.list_org_devices(org)
    down_olts, stale_olts = olt_liveness(devices, now, h.cfg.central_node_stale_s)
    fresh = {d["id"] for d in devices} - down_olts - stale_olts
    faults = drops.branch_faults(
        resolved, h.store.org_device_parent_map(org), fresh_olt_ids=fresh,
        # only PASSIVE plant may be named: ancestors are walked to tally
        # subtrees, and without this an OLT that lost every ONU would qualify
        # against its parent switch and paint its BACKHAUL as a fibre break
        passive_ids={d["id"] for d in devices
                     if d.get("device_type") in inventory.PASSIVE_TYPES})
    # How many live subscribers nobody has recorded a splitter for. The map
    # states this rather than letting an operator read a thin plant record as a
    # complete one — the same reason the paging roster reports its unassigned
    # count instead of leaving it to be inferred from an absence.
    recorded = {d.mac for d in resolved}
    unrecorded = sum(1 for r in roster
                     if (mac := onuroster._norm_mac(r.get("serial")))
                     and mac not in recorded)
    h._reply(200, {
        "splitters": [ld.as_dict() for ld in loads.values()],
        "faults": [f.as_dict() for f in faults],
        "recorded": len(resolved),
        "unrecorded": unrecorded,
        "outlier_db": drops.OUTLIER_DB,
    })


def onu_drop_subscribers(h, qs):
    """The subscribers recorded on ONE passive — the splitter panel's list."""
    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    now = datetime.now(timezone.utc)
    resolved, _ = _resolved_drops(h, org, now)
    mine = [d for d in resolved if d.passive_id == did]
    load = drops.splitter_loads(mine).get(did)
    h._reply(200, {
        "drops": [{"mac": d.mac, "olt_id": d.olt_id, "pon_port": d.pon_port,
                   "onu_id": d.onu_id, "name": d.name, "state": d.state,
                   "rx_dbm": d.rx_dbm, "severity": d.severity,
                   "matched": d.matched, "witness": d.witness}
                  for d in mine],
        "load": load.as_dict() if load else None,
        "outlier_db": drops.OUTLIER_DB,
    })


def set_onu_drops(h, user, body):
    """Record (or clear) which passive box these subscribers' drops come off.

    A BULK write, because the question is "which customers hang off this
    splitter" — asked once per box while standing at it, not once per subscriber
    from eight separate rows. Owner-only: this is plant documentation, and the
    branch-fault verdict is computed from it.

    The passive is re-derived from its OWN row, never trusted from the body (the
    org rule every device write here keeps), and it must actually be passive
    plant: a drop comes out of a splitter, and letting it point at a switch would
    put subscribers on a box that has an FSM and an outage of its own.
    """
    clean = inventory.clean_onu_drops_payload(body)
    if clean["passive_id"] is None:
        # detach: the MACs are the only thing being named, so the org comes from
        # the caller's own scope the way the reference-point write resolves it
        org = body_org_write(h, user, body)
        if org is DENIED:
            return
        if not org:
            h._reply(400, {"error": "org_id is required"})
            return
        n = h.store.clear_onu_drops(org, clean["macs"])
        h._reply(200, {"ok": True, "detached": n})
        return
    org = device_write_org(h, user, clean["passive_id"])
    if org is DENIED:
        return
    dev = h.store.get_org_device(org, clean["passive_id"]) or {}
    if dev.get("device_type") not in inventory.PASSIVE_TYPES:
        raise inventory.InventoryError(
            "a drop comes off passive plant — pick a splitter, FDB or closure")
    n = h.store.set_onu_drops(org, clean["macs"], clean["passive_id"])
    h._reply(200, {"ok": True, "attached": n})


def onu_place(h, user, body):
    """Place, move or clear a reference ONU. Owner-only.

    Placing IS the operator's claim that this subscriber's power is reliable —
    there is no power column and nothing detects it — so the UI must state that
    contract at the click. A placement changes PON-fault verdicts (ponfault's
    witness rule), which is why this is a write right and not triage."""
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    # A SUPERADMIN is org_id IS NULL, so `body_org_write` legitimately yields
    # None and the org can only come from the body — the platform admin is the
    # operator on this deployment, so that is the COMMON path, not an edge case.
    # Without this the None reached the store and raised a NOT NULL
    # IntegrityError, which the operator saw as a bare "internal error".
    # There is no org-less reference point to store, so refuse and say why.
    if not org:
        h._reply(400, {"error": "org_id is required to place a reference ONU"})
        return
    clean = inventory.clean_onu_place_payload(body)
    if clean["lat"] is None:
        ok = h.store.delete_onu_place(org, clean["mac"])
    else:
        ok = h.store.set_onu_place(org, clean["mac"], clean["lat"], clean["lng"],
                                   clean["label"], clean["notes"])
    h._reply(200, {"ok": ok})


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


# ----- paging responsibility (device → field accounts) -----------------------
#
# A NOTIFICATION rule, not a permission: nothing here changes what a session may
# read, and every account keeps seeing the whole fleet (operator choice
# 2026-07-26). The rules live in central/assignment.py; storage in
# store_assign.py.

def assignments(h, qs):
    """The assignment screen's whole payload: the org's field accounts, how many
    devices each is responsible for, and the raw rows.

    OWNER-ONLY (not in `_WORKER_GET`) for the same reason `/api/users` is: it
    enumerates accounts. It ships `has_whatsapp` as a BOOLEAN and never the
    number itself — the screen only needs to warn that an assignee can't actually
    be reached, and a roster of the team's phone numbers is not something an
    assignment UI has any use for.
    """
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    rows = h.store.list_device_assignments(org)
    parents = h.store.device_parent_map(org)
    amap = h.store.device_assignment_map(org)
    accounts = []
    for u in h.store.list_users(org):
        if u["org_id"] != org or not u["is_active"]:
            continue
        if u["role"] not in ("owner", "worker"):
            continue
        # `devices` is the INHERITED reach (assigned rows plus everything under
        # them) while `assigned` counts the rows themselves — the screen has to
        # say "3 devices ticked, 47 covered", or one click on a region head looks
        # like it did almost nothing.
        scope = assignment.scope_of(u["id"], parents, amap)
        accounts.append({
            "user_id": u["id"], "username": u["username"], "role": u["role"],
            "has_whatsapp": bool(u.get("whatsapp_number")),
            "assigned": sum(1 for r in rows if r["user_id"] == u["id"]),
            "devices": len(scope),
        })
    # Devices no row covers, directly or by inheritance: these still page every
    # worker, and an operator who thinks assignment is complete needs to see that
    # number rather than infer it.
    unassigned = sum(1 for did in parents
                     if not assignment.responsible_users(did, parents, amap))
    h._reply(200, {"assignments": rows, "accounts": accounts,
                   "unassigned": unassigned})


def assign(h, user, body):
    """Set who is paged about a device, or add/remove accounts across many.

    Two shapes, deliberately different:

      * ``{device_id, user_ids}`` REPLACES that device's set. An empty list is
        valid and means "back to paging every worker" — unlike outage assignment,
        where "assigned to nobody" has no meaning and is refused 422.
      * ``{device_ids, user_ids, mode: add|remove}`` is the bulk path and is
        ADDITIVE, so handing a region to one worker never strips whoever else was
        already responsible for those devices (see store_assign).

    Owner-only, org re-derived from each device's own DB row — the body's
    ``org_id`` is never trusted, so a device id from another org resolves to that
    org and fails ``_can_write`` instead of being written.
    """
    raw_users = body.get("user_ids")
    user_ids = [int(u) for u in raw_users if str(u).strip().isdigit()] \
        if isinstance(raw_users, list) else []
    legal = h.store.assignable_user_ids(user["org_id"]) if user["org_id"] else None

    if body.get("device_ids") is not None:
        raw = body.get("device_ids")
        device_ids = [int(d) for d in raw if str(d).strip().lstrip('-').isdigit()] \
            if isinstance(raw, list) else []
        if not device_ids or not user_ids:
            h._reply(422, {"error": "device_ids and user_ids are both required"})
            return
        # Every device must belong to an org this caller may write. Resolved per
        # id off the DB row, so a mixed-org list is refused rather than partly
        # applied.
        orgs = {h.store.device_org(d) for d in device_ids}
        if None in orgs or len(orgs) != 1:
            h._reply(422, {"error": "devices must all belong to one org"})
            return
        org = orgs.pop()
        if not h._can_write(user, org):
            h._reply(403, {"error": "forbidden"})
            return
        remove = str(body.get("mode") or "add") == "remove"
        n = h.store.bulk_assign_devices(org, device_ids, user_ids,
                                        user["username"], remove=remove)
        h._reply(200, {"ok": True, "changed": n,
                       "unreachable": _unreachable(h, org, user_ids)})
        return

    did = int(body.get("device_id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    if legal is not None and any(u not in legal for u in user_ids):
        # A worker id from another org: refuse loudly rather than silently
        # dropping it, or the UI would show a saved assignment that isn't there.
        h._reply(422, {"error": "unknown account for this org"})
        return
    ok = h.store.set_device_assignees(org, did, user_ids, user["username"])
    if not ok:
        h._reply(404, {"ok": False, "error": "no such device"})
        return
    # Reported, never enforced: an assignee with no WhatsApp number has been made
    # responsible for a device nothing will tell them about. That's a fact for the
    # operator to fix, not a reason to reject the assignment or to widen the page
    # back to the whole team.
    h._reply(200, {"ok": True, "assignee_ids": user_ids,
                   "unreachable": _unreachable(h, org, user_ids)})


def _unreachable(h, org: str, user_ids: list[int]) -> list[str]:
    """Usernames among the assignees that have no WhatsApp number set — the
    people a page would have gone to and won't."""
    wanted = set(user_ids)
    return [u["username"] for u in h.store.list_users(org)
            if u["id"] in wanted and u["org_id"] == org
            and not u.get("whatsapp_number")]
