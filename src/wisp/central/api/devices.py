from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from wisp.central import (assignment, customers as customers_mod,
                          drops, fiber, inventory, nvr_profiles, onuroster,
                          ponfault, radius_profiles, radius_sync,
                          weboptics_profiles)
from wisp.central.api.common import (DENIED, body_org_write, can_survey,
                                     device_read_scope, device_write_org,
                                     in_scope, keep_visible, olt_liveness,
                                     org_or_400, q_int_or, q_int_required,
                                     reader_or_401, superadmin_or_403,
                                     superadmin_write_or_403, survey_write_org,
                                     visible_device_ids)

log = logging.getLogger("wisp.central")


def _stamp_optical_faults(h, org: str, devices: list[dict]) -> None:
    for d in devices:
        d["fiber_cuts"] = 0
        d["dup_macs"] = 0
    rows = h.store.org_onu_rows(org)
    if not rows:
        return
    now = datetime.now(timezone.utc)
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
    devices = keep_visible(h.store.list_org_devices(org),
                           visible_device_ids(h, user, org), "id")
    _stamp_optical_faults(h, org, devices)
    h._reply(200, {"devices": devices, "tag_colors": h.store.org_colors(org, "tag")})


def regions(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    regions_ = h.store.list_regions(org)
    scope = visible_device_ids(h, user, org)
    if scope is not None:
        seen: dict[str, int] = {}
        for d in h.store.list_org_devices(org):
            if d["id"] in scope and d.get("region"):
                seen[d["region"]] = seen.get(d["region"], 0) + 1
        regions_ = [{**r, "device_count": seen.get(r["name"], 0)} for r in regions_]
    h._reply(200, {"regions": regions_})


def routes(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    routes_ = h.store.list_link_routes(org)
    scope = visible_device_ids(h, user, org)
    if scope is not None:
        routes_ = [r for r in routes_
                   if r.get("child_id") in scope and r.get("parent_id") in scope]
    h._reply(200, {"routes": routes_})


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
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    h._reply(200, {"ports": keep_visible(h.store.list_link_ports(org),
                                         visible_device_ids(h, user, org))})


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
    onus = onuroster.current_roster(h.store.list_onu_optics(org, did), now,
                                    stale_s=None)
    dups = onuroster.duplicate_macs(h.store.org_onu_rows(org), now)
    dup_macs = [d.as_dict() for d in dups
                if any(m["device_id"] == did for m in d.members)]
    placed = {p["mac"]: p for p in h.store.list_onu_places(org)}
    attached = h.store.onu_drop_map(org)
    for o in onus:
        mac = onuroster._norm_mac(o.get("serial"))
        p = placed.get(mac)
        pinned = p if p and p["lat"] is not None else None
        o["place"] = ({"lat": pinned["lat"], "lng": pinned["lng"],
                       "label": pinned["label"],
                       "witness": bool(pinned["witness"]),
                       "phone": pinned["phone"]}
                      if pinned else None)
        o["phone"] = p["phone"] if p else None
        o["drop_passive_id"] = attached.get(mac)
    h._reply(200, {
        "onus": onus,
        "olt": h.store.get_olt_optics(org, did),
        "warn_dbm": dev.get("optical_warn_dbm") if dev.get("optical_warn_dbm") is not None else h.cfg.optical_warn_dbm,
        "crit_dbm": dev.get("optical_crit_dbm") if dev.get("optical_crit_dbm") is not None else h.cfg.optical_crit_dbm,
        "onu_pon_limit": dev.get("onu_pon_limit") if dev.get("onu_pon_limit") is not None else h.cfg.onu_pon_limit,
        "dup_macs": dup_macs,
    })


ONU_SEARCH_MIN = 3
ONU_SEARCH_MAX = 50


def onu_search(h, qs):

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
    scope = visible_device_ids(h, user, org)
    matches: list[dict] = []
    shipped = 0
    truncated = False
    names = h.store.org_device_names(org)
    candidates = [did for did in h.store.onu_search_device_ids(org, needle)
                  if in_scope(scope, did) and did in names]
    rosters = h.store.list_onu_optics_by_device(org, candidates)
    for did in candidates:
        roster = onuroster.current_roster(rosters.get(did, []), now,
                                          stale_s=None)
        hits = [o for o in roster
                if needle in onuroster.search_key(o.get("serial"))
                or needle in onuroster.search_key(o.get("name"))
                or needle in onuroster.search_key(o.get("label"))
                or needle in onuroster.search_key(o.get("radius_name"))
                or needle in onuroster.search_key(o.get("radius_username"))
                or needle in onuroster.search_key(o.get("radius_mobile"))]
        if not hits:
            continue
        hits.sort(key=lambda o: (str(o.get("pon_port") or ""), o.get("onu_id") or 0,
                                 str(o.get("onu_key") or "")))
        room = ONU_SEARCH_MAX - shipped
        if len(hits) > room:
            hits = hits[:room]
            truncated = True
        shipped += len(hits)
        matches.append({
            "device_id": did,
            "device_name": names.get(did) or "",
            "onus": [{
                "id": o.get("id"),
                "onu_key": o.get("onu_key"),
                "pon_port": o.get("pon_port"),
                "onu_id": o.get("onu_id"),
                "name": o.get("name"),
                "label": o.get("label"),
                "serial": o.get("serial"),
                "state": o.get("state"),
                "severity": o.get("severity"),
                "rx_dbm": o.get("rx_dbm"),
                "distance_m": o.get("distance_m"),
                "last_online_at": o.get("last_online_at"),
                "updated_at": o.get("updated_at"),
                "radius_name": o.get("radius_name"),
                "radius_username": o.get("radius_username"),
                "radius_mobile": o.get("radius_mobile"),
                "radius_status": o.get("radius_status"),
            } for o in hits],
        })
        if shipped >= ONU_SEARCH_MAX:
            truncated = True
            break
    matches.sort(key=lambda m: m["device_name"].lower())
    h._reply(200, {"matches": matches, "truncated": truncated})


def snmp_walks(h, qs):
    # Superadmin-only, like the walk that produced them: a result is a raw
    # varbind dump off a customer's gear, so the list and the dump answer to the
    # same gate as the queue. The org scope check stays underneath it.
    # NO dashboard surface calls this any more — the walk dialog and the profile
    # wizard were deleted from the SPA for every role, and vendor onboarding is
    # an ops job now. The routes stay because the queue is how a walk reaches
    # the edge in the next `/report` reply, so this gate is the ONLY thing in
    # front of them: there is no hidden button to fall back on.
    user = superadmin_or_403(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    h._reply(200, {"walks": h.store.list_snmp_walks(org, did)})


def snmp_walk_result(h, qs):
    user = superadmin_or_403(h)
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
    # READABLE by an owner, on purpose — see `gpon_profiles` for the rule that
    # governs every recipe list. Authoring is superadmin-only; PICKING which
    # recipe applies to your own box is the ISP's job, and that is the whole
    # payoff of the profiles going global.
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


    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    dev = h.store.get_org_device(org, did) or {}
    snmp = {s["subsystem"]: s for s in h.store.device_snmp_status(org, did)}
    optics = snmp.get("optics") or {}
    declared = str(dev.get("gpon_vendor") or "").strip().lower()
    detected = str(optics.get("profile") or "").strip().lower()
    if not (detected and str(optics.get("sysobjectid") or "").strip()):
        detected = ""
    vendor = declared or detected
    profiles = weboptics_profiles.ProfileSet.build(
        h.store.list_web_optics_profiles(org))
    profile = profiles.resolve(org, vendor) if vendor else None
    creds = h.store.get_device_webui_credentials(org, did) or {}
    counts = h.store.onu_rx_counts(org, did)
    sweeper = getattr(h, "weboptics", None)
    h._reply(200, {
        "vendor": vendor or None,
        "vendor_source": "declared" if declared else ("detected" if detected else None),
        "web_profile": profile.name if profile else None,
        "known_vendors": sorted(profiles.names()),
        "has_credentials": bool(creds.get("username") and creds.get("password_enc")),
        "web_proxy": h.store.org_web_proxy(org),
        "has_node": bool(dev.get("assigned_node_id")),
        "onus_total": counts["total"],
        "onus_rx": counts["with_rx"],
        "scrape": h.store.get_web_optics_status(org, did),
        "can_refresh": bool(sweeper and sweeper.target(org, did)),
        "refreshing": bool(sweeper and sweeper.busy(did)),
    })


def rx_refresh(h, user, body):


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
        h._reply(409, {"error": "a read of this OLT is already running"})
        return
    dev = sweeper.target(org, device_id)
    if dev is None:
        h._reply(400, {"error": "this OLT isn't set up for web-UI optical reads. "
                                "See the Optical tab for what's missing."})
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


def nvr_profiles_list(h, qs):
    # Owner-readable, the recipe-list rule stated on `gpon_profiles`: the device
    # form's NVR vendor dropdown is built from `names`. There is no NVR profile
    # WRITE route to lock — authoring one is already an ops job, which is now
    # the rule for every recipe table.
    user = reader_or_401(h)
    if not user:
        return
    org = h._scope_org(user, qs)
    pset = nvr_profiles.ProfileSet.build(h.store.list_nvr_profiles(org))
    h._reply(200, {
        "profiles": h.store.list_nvr_profiles(org),
        "builtins": list(nvr_profiles.builtin_names()),
        "names": sorted(pset.names()),
    })


def nvr_channels(h, qs):

    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    dev = h.store.get_org_device(org, did) or {}
    declared = str(dev.get("nvr_vendor") or "").strip().lower()
    profiles = nvr_profiles.ProfileSet.build(h.store.list_nvr_profiles(org))
    profile = profiles.resolve(org, declared) if declared else None
    creds = h.store.get_device_webui_credentials(org, did) or {}
    sweeper = getattr(h, "weboptics", None)
    h._reply(200, {
        "channels": h.store.list_nvr_channels(org, did),
        "scrape": h.store.get_nvr_status(org, did),
        "vendor": declared or None,
        "profile": profile.name if profile else None,
        "known_vendors": sorted(profiles.names()),
        "has_credentials": bool(creds.get("username")
                                and creds.get("password_enc")),
        "web_proxy": h.store.org_web_proxy(org),
        "has_node": bool(dev.get("assigned_node_id")),
        "can_refresh": bool(sweeper and sweeper.nvr_target(org, did)),
        "refreshing": bool(sweeper and sweeper.busy(did)),
    })


def nvr_snapshot(h, qs):

    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    channel_no = q_int_or(qs, "channel_no", None)
    if channel_no is None:
        h._reply(400, {"error": "channel_no required"})
        return
    sweeper = getattr(h, "weboptics", None)
    if sweeper is None:
        h._reply(503, {"error": "web-UI reads are not enabled on this server"})
        return
    frame, err, code = sweeper.snapshot(org, did, channel_no)
    if frame is None:
        h._reply(code, {"error": err or "no frame"})
        return
    h._send_binary(200, "image/jpeg", frame)


def nvr_watch(h, user, body):

    try:
        device_id = int(body.get("device_id"))
        channel_no = int(body.get("channel_no"))
    except (TypeError, ValueError):
        h._reply(400, {"error": "device_id and channel_no required"})
        return
    org = device_write_org(h, user, device_id)
    if org is DENIED:
        return
    if org is None:
        h._reply(404, {"error": "device not found"})
        return
    on = bool(body.get("on"))
    if not h.store.set_nvr_channel_watch(org, device_id, channel_no, on):
        h._reply(404, {"error": "no such camera channel"})
        return
    h._reply(200, {"ok": True})


def nvr_refresh(h, user, body):

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
        h._reply(503, {"error": "web-UI reads are not enabled on this server"})
        return
    if sweeper.busy(device_id):
        h._reply(409, {"error": "a read of this NVR is already running"})
        return
    dev = sweeper.nvr_target(org, device_id)
    if dev is None:
        h._reply(400, {"error": "this NVR isn't set up for camera reads. "
                                "See the Cameras tab for what's missing."})
        return

    def _run() -> None:
        try:
            sweeper.scrape_nvr(dev)
        except Exception:
            log.exception("manual NVR read failed for device=%d", device_id)

    threading.Thread(target=_run, name=f"wisp-nvrrefresh-{device_id}",
                     daemon=True).start()
    log.info("manual NVR read queued by user=%s for %s/device=%d",
             user["id"], org, device_id)
    h._reply(200, {"started": True})


def customers_list(h, qs):

    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    h._reply(200, customers_mod.collect(h.store, h.cfg, org))


def radius_settings(h, qs):

    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    status = {s["account_id"]: s for s in h.store.org_radius_status(org)}
    accounts = []
    for account in h.store.org_radius_accounts(org):
        account_id = int(account["id"])
        accounts.append({
            "id": account_id,
            "label": account.get("label") or "",
            "profile": account.get("profile"),
            "base_url": account.get("base_url"),
            "username": account.get("username"),
            "password_set": bool(account.get("password_enc")),
            "enabled": bool(account.get("enabled")),
            "updated_at": account.get("updated_at"),
            "status": status.get(account_id),
            "customers": h.store.radius_customer_count(org, account_id),
        })
    h._reply(200, {
        "accounts": accounts,
        "customers": h.store.radius_customer_count(org),
        "profiles": list(radius_profiles.ProfileSet.build(
            h.store.list_radius_profiles(org)).names()),
    })


def radius_configure(h, user, body):

    org = body_org_write(h, user, body)
    if org is DENIED:
        return

    account_id = body.get("id")
    account_id = int(account_id) if account_id else None
    if account_id is not None:
        existing = h.store.get_radius_account(account_id)
        if not existing or existing.get("org_id") != org:
            h._reply(404, {"error": "no such billing panel for this org"})
            return

    if body.get("delete"):
        if account_id is None:
            h._reply(422, {"error": "which panel? the request named no id"})
            return
        h.store.delete_radius_account(account_id)
        log.info("radius account %s deleted by user=%s for org=%s", account_id,
                 user["id"], org)
        h._reply(200, {"deleted": True})
        return

    profile = str(body.get("profile") or "").strip().lower()
    if radius_profiles.ProfileSet.build(
            h.store.list_radius_profiles(org)).resolve(org, profile) is None:
        h._reply(422, {"error": f"no billing-panel recipe named {profile!r}"})
        return
    try:
        base_url = radius_sync.clean_base_url(body.get("base_url"))
    except radius_sync.PanelError as e:
        h._reply(422, {"error": str(e)})
        return

    label = str(body.get("label") or "").strip()[:64]
    username = str(body.get("username") or "").strip() or None
    password_enc = None
    if body.get("password"):
        password_enc = h.secretbox.encrypt(str(body["password"]))
    saved = h.store.set_radius_account(
        org, profile=profile, base_url=base_url, username=username,
        password_enc=password_enc, account_id=account_id, label=label,
        enabled=bool(body.get("enabled", True)), updated_by=user["id"])
    log.info("radius account %s set by user=%s for org=%s (%s)", saved, user["id"],
             org, base_url)
    h._reply(200, {"saved": True, "id": saved})


def radius_sync_now(h, user, body):

    org = body_org_write(h, user, body)
    if org is DENIED:
        return

    wanted = body.get("id")
    accounts = h.store.org_radius_accounts(org, enabled_only=True)
    if wanted:
        accounts = [a for a in accounts if int(a["id"]) == int(wanted)]
        if not accounts:
            h._reply(404, {"error": "no such billing panel for this org"})
            return
    if not accounts:
        h._reply(400, {"error": "no billing panel is configured for this org"})
        return

    syncer = radius_sync.build_syncer(h.cfg, h.store, h.secretbox)
    if syncer is None:
        h._reply(503, {"error": "billing-panel sync is not enabled on this server"})
        return

    def _run() -> None:
        for account in accounts:
            try:
                syncer.sync_org(account)
            except Exception:
                log.exception("manual radius sync failed for org=%s account=%s",
                              org, account.get("id"))

    threading.Thread(target=_run, name=f"wisp-radius-{org}", daemon=True).start()
    log.info("manual radius sync queued by user=%s for org=%s (%d panel(s))",
             user["id"], org, len(accounts))
    h._reply(200, {"started": True, "panels": len(accounts)})


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


def _gpon_vendor_names(h, org: str) -> set[str]:

    return {p["name"] for p in h.store.list_gpon_profiles(org) if p.get("name")}


def _nvr_vendor_names(h, org: str) -> set[str]:

    return {p["name"] for p in h.store.list_nvr_profiles(org) if p.get("name")}


def create(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    clean = inventory.clean_device_payload(
        body, parents=h.store.org_device_parent_map(org), device_id=None,
        registered_nodes=h.store.registered_node_ids(org),
        passive_ids=h.store.org_passive_ids(org),
        gpon_vendors=_gpon_vendor_names(h, org),
        nvr_vendors=_nvr_vendor_names(h, org))
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
        gpon_vendors=_gpon_vendor_names(h, org),
        nvr_vendors=_nvr_vendor_names(h, org))
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


    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    now = datetime.now(timezone.utc)
    scope = visible_device_ids(h, user, org)
    roster = [r for r in onuroster.current_roster(h.store.org_onu_rows(org), now,
                                                  stale_s=None)
              if in_scope(scope, r.get("device_id"))]
    placed = h.store.onu_place_macs(org, witness_only=False, located_only=True)

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
    unplaced: list[dict] = []
    located: list[dict] = []
    details = {p["mac"]: p for p in h.store.list_onu_places(org)} if want else {}
    if want:
        for r in roster:
            if r.get("device_id") != want:
                continue
            mac = onuroster._norm_mac(r.get("serial"))
            if not mac:
                continue
            # The survey used to be the one screen with its own naming rule —
            # label, then the OLT's provisioning string — so a tech standing at
            # a drop read `HC-KOTHAMASS-2` where every desk screen named the
            # customer. It ranks through the shared `onuName` now, which needs
            # billing's two columns on the wire (`org_onu_rows` already selects
            # them). Read-only, and it adds no write path a worker can reach.
            row = {"mac": mac, "name": r.get("name"),
                   "radius_username": r.get("radius_username"),
                   "radius_name": r.get("radius_name"),
                   "pon_port": r.get("pon_port"),
                   "onu_id": r.get("onu_id"),
                   "state": r.get("state"),
                   "device_id": want,
                   "device_name": r.get("device_name")}
            if mac not in placed:
                unplaced.append(row)
                continue
            p = details.get(mac) or {}
            located.append({**row,
                            "label": p.get("label"),
                            "phone": p.get("phone"),
                            "lat": p.get("lat"),
                            "lng": p.get("lng"),
                            "witness": bool(p.get("witness")),
                            "accuracy_m": p.get("accuracy_m"),
                            "place_source": p.get("place_source"),
                            "placed_by": p.get("placed_by"),
                            "placed_at": p.get("placed_at")})
        key = lambda x: (x["pon_port"] or "", x["onu_id"] or 0)  # noqa: E731
        unplaced.sort(key=key)
        located.sort(key=key)

    h._reply(200, {
        "total": sum(o["total"] for o in olts.values()),
        "placed": sum(o["placed"] for o in olts.values()),
        "olts": sorted(olts.values(), key=lambda o: (o["device_name"] or "")),
        "unplaced": unplaced,
        "located": located,
    })


def field_onu(h, user, body):


    org = body.get("org_id") or user["org_id"]
    if not can_survey(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    if not org:
        h._reply(400, {"error": "org_id is required to locate an ONU"})
        return
    clean = inventory.clean_field_onu_payload(body)
    scope = visible_device_ids(h, user, org)
    known = {onuroster._norm_mac(r.get("serial"))
             for r in h.store.org_onu_rows(org) if in_scope(scope, r.get("device_id"))}
    if clean["mac"] not in known:
        h._reply(404, {"error": "no ONU with that MAC is in this org's roster"})
        return
    ok = h.store.place_onu_in_field(
        org, clean["mac"], clean["lat"], clean["lng"],
        witness=h.store.onu_place_witness(org, clean["mac"]) is True,
        accuracy_m=clean["accuracy_m"], source=clean["source"],
        placed_by=user["username"], label=clean["label"],
        phone=clean["phone"])
    h._reply(200, {"ok": ok})


def field_onu_name(h, user, body):


    org = body.get("org_id") or user["org_id"]
    if not can_survey(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    if not org:
        h._reply(400, {"error": "org_id is required"})
        return
    clean = inventory.clean_field_onu_name_payload(body)
    scope = visible_device_ids(h, user, org)
    if scope is not None and not any(
            in_scope(scope, r.get("device_id")) for r in h.store.org_onu_rows(org)
            if onuroster._norm_mac(r.get("serial")) == clean["mac"]):
        h._reply(404, {"error": "that subscriber has no location yet"})
        return
    ok = h.store.set_onu_place_contact(org, clean["mac"], clean["label"],
                                       clean["phone"])
    h._reply(200 if ok else 404,
             {"ok": ok} if ok else {"error": "that subscriber has no location yet"})


def onu_places(h, qs):


    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    now = datetime.now(timezone.utc)
    roster = onuroster.current_roster(h.store.org_onu_rows(org), now, stale_s=None)
    by_mac: dict[str, list[dict]] = {}
    for r in roster:
        mac = onuroster._norm_mac(r.get("serial"))
        if mac:
            by_mac.setdefault(mac, []).append(r)
    resolved = []
    for p in h.store.list_onu_places(org, located_only=True):
        hits = by_mac.get(p["mac"], [])
        r = hits[0] if len(hits) == 1 else {}
        resolved.append((p, hits, r))
    ifaces = h.store.onu_interfaces(
        org, {r["device_id"] for _, _, r in resolved if r.get("device_id")})
    drops_by_mac = {d["mac"]: d for d in h.store.list_onu_drops(org)}
    out = []
    for p, hits, r in resolved:
        token = onuroster.onu_if_token(r.get("pon_port"), r.get("onu_id"))
        port = ifaces.get((r.get("device_id"), token)) if token else None
        out.append({**p,
                    "matched": bool(hits),
                    "witness": bool(p.get("witness")),
                    "ambiguous": len(hits) > 1,
                    "slots": len(hits),
                    "drop_passive_id": (drops_by_mac.get(p["mac"]) or {}).get("passive_id"),
                    "drop_waypoints": (drops_by_mac.get(p["mac"]) or {}).get("waypoints") or [],
                    "device_id": r.get("device_id"),
                    "device_name": r.get("device_name"),
                    "onu_id": r.get("onu_id"),
                    "pon_port": r.get("pon_port"),
                    "name": r.get("name"),
                    "state": r.get("state"),
                    "rx_dbm": r.get("rx_dbm"),
                    "severity": r.get("severity"),
                    "optics_updated_at": r.get("updated_at"),
                    "if_name": port["if_name"] if port else None,
                    "port_state": port["oper_status"] if port else None,
                    "in_bps": port["in_bps"] if port else None,
                    "out_bps": port["out_bps"] if port else None,
                    "port_updated_at": port["updated_at"] if port else None})
    h._reply(200, {"places": keep_visible(out, visible_device_ids(h, user, org))})


_MAX_PLANT_HOPS = 12


def subscriber(h, qs):


    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    mac = onuroster._norm_mac((qs.get("mac") or [""])[0])
    if not mac:
        h._reply(400, {"error": "mac is required"})
        return
    now = datetime.now(timezone.utc)

    scope = visible_device_ids(h, user, org)
    hits = [r for r in onuroster.current_roster(h.store.org_onu_rows(org), now,
                                                stale_s=None)
            if onuroster._norm_mac(r.get("serial")) == mac]
    if scope is not None and not any(in_scope(scope, r.get("device_id"))
                                     for r in hits):
        h._reply(403, {"error": "forbidden"})
        return
    record = h.store.get_onu_place(org, mac)
    out: dict = {
        "mac": mac,
        "record": ({**record, "witness": bool(record.get("witness"))}
                   if record else None),
        "matched": bool(hits),
        "ambiguous": len(hits) > 1,
        "slots": len(hits),
        "roster": None, "olt": None, "drop": None, "rate": None,
        "thresholds": None, "user_macs": [], "user_mac_status": None,
        "radius": None, "radius_panels": h.store.org_radius_status(org),
    }
    if len(hits) != 1:
        h._reply(200, out)
        return

    did = hits[0].get("device_id")
    dev = h.store.get_org_device(org, did) if did is not None else None
    if not dev:
        h._reply(200, out)
        return
    by_id = {d["id"]: d for d in h.store.list_org_devices(org)}

    row = next((o for o in onuroster.current_roster(
                    h.store.list_onu_optics(org, did), now, stale_s=None)
                if o.get("onu_key") == hits[0].get("onu_key")), None)
    out["roster"] = row or hits[0]
    out["olt"] = {"id": dev["id"], "name": dev.get("name"),
                  "state": (by_id.get(did) or {}).get("state"),
                  "optics_updated_at": (row or {}).get("updated_at")}
    out["thresholds"] = {
        "warn_dbm": (dev.get("optical_warn_dbm")
                     if dev.get("optical_warn_dbm") is not None
                     else h.cfg.optical_warn_dbm),
        "crit_dbm": (dev.get("optical_crit_dbm")
                     if dev.get("optical_crit_dbm") is not None
                     else h.cfg.optical_crit_dbm)}

    out["user_macs"] = h.store.user_macs_for_slot(org, did,
                                                  hits[0].get("onu_key") or "")
    out["user_mac_status"] = h.store.get_web_mac_status(org, did)
    out["radius"] = h.store.radius_link_for(org, did, hits[0].get("onu_key") or "")

    token = onuroster.onu_if_token(hits[0].get("pon_port"), hits[0].get("onu_id"))
    port = h.store.onu_interfaces(org, {did}).get((did, token)) if token else None
    if port:
        out["rate"] = {"if_name": port["if_name"],
                       "port_state": port["oper_status"],
                       "in_bps": port["in_bps"], "out_bps": port["out_bps"],
                       "updated_at": port["updated_at"]}

    passive_id = h.store.onu_drop_map(org).get(mac)
    if passive_id is not None:
        chain = []
        node = by_id.get(passive_id)
        seen: set[int] = set()
        while node and node["id"] not in seen and len(chain) < _MAX_PLANT_HOPS:
            seen.add(node["id"])
            chain.append({"id": node["id"], "name": node.get("name"),
                          "device_type": node.get("device_type"),
                          "split_ratio": node.get("split_ratio"),
                          "split_inputs": node.get("split_inputs"),
                          "pon_port": node.get("pon_port")})
            if node["id"] == did:
                break
            parent = node.get("parent_device_id")
            node = by_id.get(parent) if parent is not None else None
        out["drop"] = {"passive_id": passive_id, "chain": chain}
    h._reply(200, out)


def _resolved_drops(h, org: str, now):

    roster = onuroster.current_roster(h.store.org_onu_rows(org), now,
                                      stale_s=None)
    resolved = drops.resolve_drops(roster, h.store.onu_drop_map(org),
                                   witness_macs=h.store.onu_place_macs(org))
    return resolved, roster


def onu_drops(h, qs):

    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    now = datetime.now(timezone.utc)
    resolved, roster = _resolved_drops(h, org, now)
    loads = drops.splitter_loads(resolved)
    devices = h.store.list_org_devices(org)
    down_olts, stale_olts = olt_liveness(devices, now, h.cfg.central_node_stale_s)
    fresh = {d["id"] for d in devices} - down_olts - stale_olts
    faults = drops.branch_faults(
        resolved, h.store.org_plant_feed_map(org), fresh_olt_ids=fresh,
        passive_ids={d["id"] for d in devices
                     if d.get("device_type") in inventory.PASSIVE_TYPES})
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


    clean = inventory.clean_onu_drops_payload(body)
    if clean["passive_id"] is None:
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
            "a drop comes off passive plant: pick a splitter, FDB or closure")
    clean = inventory.clean_onu_drops_payload(body, split_ratio=dev.get("split_ratio"))
    n = h.store.set_onu_drops(org, clean["macs"], clean["passive_id"],
                              clean["leg_no"])
    h._reply(200, {"ok": True, "attached": n})


def onu_place(h, user, body):


    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org:
        h._reply(400, {"error": "org_id is required to place a reference ONU"})
        return
    clean = inventory.clean_onu_place_payload(body)
    if clean["lat"] is None:
        ok = h.store.clear_onu_place_coords(org, clean["mac"])
    else:
        ok = h.store.set_onu_place(
            org, clean["mac"], clean["lat"], clean["lng"],
            clean["label"], clean["notes"], phone=clean["phone"],
            witness=h.store.onu_place_witness(org, clean["mac"]) is True)
    h._reply(200, {"ok": ok})


def onu_witness(h, user, body):


    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org:
        h._reply(400, {"error": "org_id is required"})
        return
    clean = inventory.clean_onu_witness_payload(body)
    if not h.store.set_onu_witness(org, clean["mac"], clean["witness"]):
        h._reply(404, {"error": "nothing is recorded for that subscriber yet"})
        return
    h._reply(200, {"ok": True, "witness": clean["witness"]})


def onu_contact(h, user, body):

    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org:
        h._reply(400, {"error": "org_id is required to record a subscriber"})
        return
    clean = inventory.clean_onu_contact_payload(body)
    ok = h.store.set_onu_contact(org, clean["mac"], clean["label"],
                                 clean["phone"], clean["notes"])
    h._reply(200, {"ok": ok})


def _link_write_scope(h, user, clean):


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
            "no link between those devices. Set the parent first.")
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
    clean = inventory.clean_link_style_payload(body)
    org = _link_write_scope(h, user, clean)
    if org is DENIED:
        return
    h.store.set_link_style(org, clean["child_id"], clean["parent_id"],
                           clean["fields"], updated_by=user["username"])
    h._reply(200, {"ok": True})


def cable_gone(h, user, body):

    raise inventory.InventoryError(
        "fibre is recorded on the cable itself now — reload the page")


def cables(h, qs):

    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    h._reply(200, {"cables": h.store.list_org_cables(org),
                   "cabled_pairs": h.store.org_cabled_pairs(org),
                   "counts": list(fiber.FIBER_COUNTS)})


def device_ports(h, qs):


    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    h._reply(200, {"ports": {str(k): v
                             for k, v in h.store.org_device_ports(org).items()}})


def cable_save(h, user, body):


    clean = inventory.clean_cable_payload(body)
    if clean["id"] is None:
        org = body_org_write(h, user, body)
        if org is DENIED:
            return
        if not org:
            h._reply(400, {"error": "org_id is required"})
            return
    else:
        org = h.store.cable_org(clean["id"])
        if not org:
            h._reply(404, {"error": "cable not found"})
            return
        if not h._can_write(user, org):
            h._reply(403, {"error": "forbidden"})
            return
    if not _ends_in_org(h, org, clean["a"], clean["b"]):
        return
    try:
        cable_id = h.store.set_org_cable(
            org, clean["id"], name=clean["name"], cores=clean["cores"],
            notes=clean["notes"], a=clean["a"], b=clean["b"],
            updated_by=user["username"])
    except fiber.FiberError as exc:
        raise inventory.InventoryError(str(exc)) from exc
    except ValueError as exc:
        raise inventory.InventoryError(str(exc)) from exc
    h._reply(200, {"ok": True, "id": cable_id})


def _ends_in_org(h, org: str, *ends) -> bool:

    devices, macs = set(), set()
    for end in ends:
        if not end:
            continue
        if end.get("device_id") is not None:
            devices.add(end["device_id"])
        elif end.get("mac"):
            macs.add(end["mac"])
    for device_id in devices:
        if h.store.device_org(device_id) != org:
            h._reply(404, {"error": "device not found"})
            return False
    if macs and not h.store.onu_places_exist(org, macs):
        h._reply(404, {"error": "customer not found"})
        return False
    return True


def cable_core(h, user, body):

    try:
        cable_id = int(body.get("cable_id"))
        core_no = int(body.get("core_no"))
    except (TypeError, ValueError):
        raise inventory.InventoryError("cable_id and core_no are required")
    org = h.store.cable_org(cable_id)
    if not org:
        h._reply(404, {"error": "cable not found"})
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    cable = next((c for c in h.store.list_org_cables(org) if c["id"] == cable_id), None)
    if cable and cable["cores"] and not (1 <= core_no <= cable["cores"]):
        raise inventory.InventoryError(
            f"core number must be between 1 and {cable['cores']}")
    label = str(body.get("label") or "").strip()[:80] or None
    h.store.set_cable_core_label(org, cable_id, core_no, label,
                                 updated_by=user["username"])
    h._reply(200, {"ok": True})


def cable_delete(h, user, body):

    try:
        cable_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise inventory.InventoryError("cable id is required")
    org = h.store.cable_org(cable_id)
    if not org:
        h._reply(404, {"error": "cable not found"})
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    h._reply(200, {"ok": h.store.delete_org_cable(org, cable_id)})


def cable_path(h, user, body):

    clean = inventory.clean_cable_path_payload(body)
    org = h.store.cable_org(clean["cable_id"])
    if not org:
        h._reply(404, {"error": "cable not found"})
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    if not h.store.set_cable_path(org, clean["cable_id"], clean["path"],
                                  user["username"]):
        h._reply(404, {"error": "cable not found"})
        return
    h._reply(200, {"ok": True})


def cable_split(h, user, body):


    clean = inventory.clean_cable_split_payload(body)
    org = h.store.cable_org(clean["cable_id"])
    if not org:
        h._reply(404, {"error": "cable not found"})
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    try:
        out = h.store.split_org_cable(
            org, clean["cable_id"], lat=clean["lat"], lng=clean["lng"],
            name=clean["name"], updated_by=user["username"])
    except ValueError as exc:
        raise inventory.InventoryError(str(exc)) from exc
    h._reply(200, {"ok": True, **out})


def cable_move(h, user, body):


    clean = inventory.clean_cable_move_payload(body)
    orgs = {h.store.cable_org(cid) for cid in clean["cable_ids"]}
    if None in orgs or len(orgs) != 1:
        h._reply(404, {"error": "cable not found"})
        return
    org = orgs.pop()
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    ends = [{"device_id": d, "mac": m} for d, m in (clean["from"], clean["to"])]
    if not _ends_in_org(h, org, *ends):
        return
    try:
        h._reply(200, h.store.move_cable_ends(org, clean,
                                              updated_by=user["username"]))
    except ValueError as exc:
        raise inventory.InventoryError(str(exc)) from exc


def _joint_scope(h, user, body, *cable_keys):

    ids = []
    for key in cable_keys:
        raw = body.get(key)
        if raw in (None, "", "null"):
            continue
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            raise inventory.InventoryError(f"{key} is invalid")
    if not ids:
        raise inventory.InventoryError("a cable is required")
    org = h.store.cable_org(ids[0])
    if not org:
        h._reply(404, {"error": "cable not found"})
        return DENIED, None
    for other in ids[1:]:
        if h.store.cable_org(other) != org:
            h._reply(404, {"error": "cable not found"})
            return DENIED, None
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return DENIED, None
    return org, ids


def fibre_joint(h, user, body):


    org, _ = _joint_scope(h, user, body, "a_cable_id", "b_cable_id")
    if org is DENIED:
        return
    cables = {c["id"]: c for c in h.store.list_org_cables(org)}
    a_cable = cables.get(_int_or_none(body.get("a_cable_id")))
    b_cable = cables.get(_int_or_none(body.get("b_cable_id")))
    clean = inventory.clean_fibre_joint_payload(
        body, a_cores=a_cable["cores"] if a_cable else None,
        b_cores=b_cable["cores"] if b_cable else None,
        **_port_bounds(h, org, _int_or_none(body.get("device_id"))))
    if not _ends_in_org(h, org, clean):
        return
    h._reply(200, h.store.set_fibre_joint(org, clean, updated_by=user["username"]))


def _port_bounds(h, org, device_id) -> dict:

    row = h.store.get_org_device(org, device_id) if device_id else None
    return {"split_ratio": row["split_ratio"] if row else None,
            "split_inputs": row["split_inputs"] if row else None}


def fibre_connect(h, user, body):


    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org:
        h._reply(400, {"error": "org_id is required"})
        return
    to_cable = _int_or_none(body.get("to_cable_id"))
    to_cores = None
    if to_cable is not None:
        if h.store.cable_org(to_cable) != org:
            h._reply(404, {"error": "cable not found"})
            return
        to_cores = next((c["cores"] for c in h.store.list_org_cables(org)
                         if c["id"] == to_cable), None)
    clean = inventory.clean_fibre_connect_payload(
        body, to_cores=to_cores,
        **_port_bounds(h, org, _int_or_none(body.get("device_id"))),
        **{f"to_{k}": v for k, v in
           _port_bounds(h, org, _int_or_none(body.get("to_device_id"))).items()})
    if not _ends_in_org(h, org, clean, clean["to"]):
        return
    h._reply(200, h.store.connect_points(org, clean, updated_by=user["username"]))


def fibre_tail(h, user, body):


    org, _ = _joint_scope(h, user, body, "a_cable_id")
    if org is DENIED:
        return
    cables = {c["id"]: c for c in h.store.list_org_cables(org)}
    a_cable = cables.get(_int_or_none(body.get("a_cable_id")))
    to_cable = _int_or_none(body.get("to_cable_id"))
    to_cores = None
    if to_cable is not None:
        if h.store.cable_org(to_cable) != org:
            h._reply(404, {"error": "cable not found"})
            return
        to_cores = (cables.get(to_cable) or {}).get("cores")
    clean = inventory.clean_fibre_tail_payload(
        body, a_cores=a_cable["cores"] if a_cable else None, to_cores=to_cores,
        **_port_bounds(h, org, _int_or_none(body.get("to_device_id"))))
    if not _ends_in_org(h, org, clean, clean["to"]):
        return
    h._reply(200, h.store.take_core_to_box(org, clean, updated_by=user["username"]))


def fibre_through(h, user, body):

    org, _ = _joint_scope(h, user, body, "a_cable_id", "b_cable_id")
    if org is DENIED:
        return
    clean = inventory.clean_fibre_through_payload(body)
    if not _ends_in_org(h, org, clean):
        return
    try:
        out = h.store.splice_through(org, clean, updated_by=user["username"])
    except ValueError as exc:
        raise inventory.InventoryError(str(exc)) from exc
    h._reply(200, out)


def fibre_clear(h, user, body):

    org, _ = _joint_scope(h, user, body, "cable_id")
    if org is DENIED:
        return
    clean = inventory.clean_fibre_clear_payload(body)
    if not _ends_in_org(h, org, clean):
        return
    h._reply(200, {"ok": h.store.clear_fibre_joint(org, clean)})


def _int_or_none(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def device_fibre(h, qs):


    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    device_id = _int_or_none((qs.get("device") or [""])[0])
    mac = (qs.get("onu") or [""])[0].strip()
    if device_id is None and not mac:
        h._reply(400, {"error": "device or onu is required"})
        return
    h._reply(200, h.store.point_fibre(
        org, device_id=device_id,
        mac=onuroster._norm_mac(mac) if mac else None))


def fibre_trace(h, qs):

    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    cable_id = _int_or_none((qs.get("cable") or [""])[0])
    core_no = _int_or_none((qs.get("core") or [""])[0])
    if cable_id is None or core_no is None:
        h._reply(400, {"error": "cable and core are required"})
        return
    h._reply(200, h.store.trace_fibre(org, cable_id, core_no))


def drop_route(h, user, body):

    clean = inventory.clean_drop_route_payload(body)
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org:
        h._reply(400, {"error": "org_id is required"})
        return
    ok = h.store.set_onu_drop_route(org, clean["mac"], clean["waypoints"])
    if not ok:
        h._reply(404, {"error": "no drop recorded for that subscriber."
                                " Record which splitter feeds it first."})
        return
    h._reply(200, {"ok": True, "points": len(clean["waypoints"])})


def snmp(h, user, body):
    did = int(body.get("id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    clean = inventory.clean_snmp_payload(body)
    ok = h.store.set_org_device_snmp(org, did, clean)
    h._reply(200 if ok else 404, {"ok": ok})


def web_access(h, user, body):
    did = int(body.get("id") or body.get("device_id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    dev = h.store.get_org_device(org, did) if org is not None else None
    if not dev:
        h._reply(404, {"ok": False, "error": "device not found"})
        return
    clean = inventory.clean_web_access_payload(body)
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


def webui_credentials(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    did = q_int_required(h, qs, "device_id")
    if did is None:
        return
    org = device_write_org(h, user, did)
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
    auth_mode = "basic" if str(body.get("auth_mode") or "").lower() == "basic" else "form"
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


def _walk_target_node(h, org: str, did: int) -> str | None:
    """The refusals every walk queue shares. None once one has been answered.

    A walk reaches gear only through the device's assigned probe, in that
    probe's next `/report` reply, so a device with SNMP off or no node cannot
    be walked at all. Both queues (the platform admin's raw walk and the
    owner's SNMP test) refuse identically, by name.
    """
    device = h.store.get_org_device(org, did)
    if not device:
        h._reply(404, {"error": "device not found"})
        return None
    if not device.get("snmp_enabled") or not device.get("snmp_community"):
        raise inventory.InventoryError(
            "enable SNMP (with a community) on this device first")
    node = device.get("assigned_node_id")
    if not node:
        raise inventory.InventoryError(
            "assign this device to a probe first. The walk runs from "
            "its assigned node.")
    return node


def snmp_walk_create(h, user, body):
    if not superadmin_write_or_403(h, user):
        return
    did = int(body.get("device_id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    node = _walk_target_node(h, org, did)
    if node is None:
        return
    clean = inventory.clean_walk_payload(body)
    wid = h.store.create_snmp_walk(org, did, node, clean["root_oid"],
                                   clean["max_varbinds"],
                                   requested_by=user["username"])
    h._reply(200, {"id": wid})


def snmp_test_create(h, user, body):
    """The owner's "Test SNMP" button. A pinned walk, never an OID the client names.

    Kept for owners on purpose: every fault it names is the ISP's own to fix
    (a wrong community string, a source-IP ACL, UDP 161 not forwarded through
    NAT), and there is no other way for them to tell "we never asked" from
    "the box never answered".

    The root and the cap come from `inventory.SNMP_TEST_*` and a `root_oid` in
    the body is IGNORED, not echoed: the raw walk stays superadmin-only, and a
    gate that reads a client-chosen field is not a gate. Owner-level via
    `device_write_org` (`_can_write`), and workers never reach it at all: a new
    `/api/*` route is worker-blocked by default at `_WORKER_ROUTES`, which is
    right, because a worker does not manage devices.

    It rides the SAME `store.create_snmp_walk` as the raw queue, so the
    per-device retention cap and the supersede-pending behaviour keep working
    and the table gains no second write path.
    """
    did = int(body.get("device_id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    node = _walk_target_node(h, org, did)
    if node is None:
        return
    wid = h.store.create_snmp_walk(org, did, node,
                                   inventory.SNMP_TEST_ROOT_OID,
                                   inventory.SNMP_TEST_MAX_VARBINDS,
                                   requested_by=user["username"])
    h._reply(200, {"id": wid})


SYS_DESCR_OID = "1.3.6.1.2.1.1.1"
SYS_DESCR_MAX = 200


def _sys_descr(rows) -> str | None:
    """sysDescr out of a system-group dump, extracted HERE, server-side.

    The verdict route ships this one string and no varbinds, so the extraction
    cannot live in the SPA: shipping rows for the client to search is shipping
    the dump.
    """
    if not isinstance(rows, list):
        return None
    pairs = [r for r in rows if isinstance(r, (list, tuple)) and len(r) >= 2]
    if not pairs:
        return None
    hit = next((r for r in pairs if str(r[0]).startswith(SYS_DESCR_OID)), pairs[0])
    text = " ".join(str(hit[1] or "").split())
    return text[:SYS_DESCR_MAX] or None


def snmp_test_result(h, qs):
    """The verdict, NOT the dump: status, answered, sysDescr, error.

    An owner may never read raw varbinds through this route, so it composes a
    fixed five-key shape and never passes the walk row through. It also serves
    only walks on the pinned test root, so it cannot be pointed at a walk this
    button did not queue (a CLI walk on some vendor subtree answers 404).
    """
    user = reader_or_401(h)
    if not user:
        return
    wid = q_int_required(h, qs, "id")
    if wid is None:
        return
    org = h.store.snmp_walk_org(wid)
    if org is None or not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    walk = h.store.get_snmp_walk(org, wid)
    if not walk or walk.get("root_oid") != inventory.SNMP_TEST_ROOT_OID:
        h._reply(404, {"error": "test not found"})
        return
    status = walk.get("status")
    count = walk.get("varbind_count") or 0
    h._reply(200, {"test": {
        "id": walk["id"],
        "status": status,
        "answered": bool(status == "done" and count > 0),
        "sys_descr": _sys_descr(walk.get("result")) if status == "done" else None,
        "error": walk.get("error"),
    }})


def profile_create(h, user, body):
    # Authoring is superadmin-only; org-scoped rows are still writable, by
    # naming the org in the body (that is how the platform admin ships an
    # override for one ISP's box).
    if not superadmin_write_or_403(h, user):
        return
    clean = inventory.clean_profile_payload(body)
    pid = h.store.create_snmp_profile(body.get("org_id") or None, clean)
    h._reply(200, {"id": pid})


def _profile_mutate(h, user, body, *, delete: bool):
    if not superadmin_write_or_403(h, user):
        return
    profile = h.store.get_snmp_profile(int(body.get("id") or 0))
    if not profile:
        h._reply(404, {"error": "profile not found"})
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


def gpon_profiles(h, qs):
    # THE RULE FOR EVERY RECIPE LIST (snmp / gpon / web-optics / nvr): the list
    # stays owner-READABLE while authoring is superadmin-only. Writing a recipe
    # is internal work; choosing which recipe applies to your own OLT is the
    # ISP's, and once recipes are global that choice is the entire payoff — an
    # owner picks "C-Data" instead of waiting for a per-org copy.
    # Locking the read would be data loss, not a cosmetic refusal: the device
    # form's vendor dropdown is built from this list, a Select with no item for
    # its value renders BLANK, and the next save unstamps a correctly-vendored
    # OLT. It is also why `org_devices.gpon_vendor` validates against these
    # rows rather than the built-in names.
    # The shape is safe to hand an owner: recipes are paths and OIDs, never a
    # host or a secret (those live on the ACCOUNT and on the device's own
    # credentials), and `_scope_org` pins the rows to global + their own org.
    user = reader_or_401(h)
    if not user:
        return
    org = h._scope_org(user, qs)
    h._reply(200, {"profiles": h.store.list_gpon_profiles(org),
                   "oid_fields": list(inventory.GPON_PROFILE_OIDS),
                   "states": list(inventory.GPON_PROFILE_STATES),
                   "pon_index_strategies": list(inventory.GPON_PON_INDEX_STRATEGIES)})


def gpon_profile_create(h, user, body):
    if not superadmin_write_or_403(h, user):
        return
    clean = inventory.clean_gpon_profile_payload(body)
    pid = h.store.create_gpon_profile(body.get("org_id") or None, clean)
    h._reply(200, {"id": pid})


def _gpon_profile_mutate(h, user, body, *, delete: bool):
    if not superadmin_write_or_403(h, user):
        return
    profile = h.store.get_gpon_profile(int(body.get("id") or 0))
    if not profile:
        h._reply(404, {"error": "profile not found"})
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


def web_optics_profiles(h, qs):
    # Owner-readable, the recipe-list rule stated on `gpon_profiles`: this feeds
    # the weboptics vendor field. Authoring is superadmin-only below.
    user = reader_or_401(h)
    if not user:
        return
    org = h._scope_org(user, qs)
    h._reply(200, {
        "profiles": h.store.list_web_optics_profiles(org),
        "builtins": list(weboptics_profiles.builtin_names()),
        "fields": list(weboptics_profiles.FIELDS),
        "sessions": list(weboptics_profiles.SESSION_STRATEGIES),
        "methods": list(weboptics_profiles.OPTICS_METHODS),
        "charsets": list(weboptics_profiles.CHARSETS),
        "onu_id_shapes": list(weboptics_profiles.ONU_ID_SHAPES),
        "example": weboptics_profiles.BUILTIN_SPECS.get("dbc", {}),
    })


def web_optics_profile_create(h, user, body):
    if not superadmin_write_or_403(h, user):
        return
    clean = weboptics_profiles.clean_web_optics_profile_payload(body)
    h._reply(200, {"id": h.store.create_web_optics_profile(
        body.get("org_id") or None, clean)})


def _web_optics_profile_mutate(h, user, body, *, delete: bool):
    if not superadmin_write_or_403(h, user):
        return
    profile = h.store.get_web_optics_profile(int(body.get("id") or 0))
    if not profile:
        h._reply(404, {"error": "profile not found"})
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


def assignments(h, qs):

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
        scope = assignment.scope_of(u["id"], parents, amap)
        accounts.append({
            "user_id": u["id"], "username": u["username"], "role": u["role"],
            "has_whatsapp": bool(u.get("whatsapp_number")),
            "assigned": sum(1 for r in rows if r["user_id"] == u["id"]),
            "devices": len(scope),
        })
    unassigned = sum(1 for did in parents
                     if not assignment.responsible_users(did, parents, amap))
    h._reply(200, {"assignments": rows, "accounts": accounts,
                   "unassigned": unassigned})


def assign(h, user, body):


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
        h._reply(422, {"error": "unknown account for this org"})
        return
    ok = h.store.set_device_assignees(org, did, user_ids, user["username"])
    if not ok:
        h._reply(404, {"ok": False, "error": "no such device"})
        return
    h._reply(200, {"ok": True, "assignee_ids": user_ids,
                   "unreachable": _unreachable(h, org, user_ids)})


def _unreachable(h, org: str, user_ids: list[int]) -> list[str]:
    wanted = set(user_ids)
    return [u["username"] for u in h.store.list_users(org)
            if u["id"] in wanted and u["org_id"] == org
            and not u.get("whatsapp_number")]
