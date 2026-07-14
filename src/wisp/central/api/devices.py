"""Device inventory routes: CRUD, placement, cable routes, regions, backup
links, switch ports, ONU/OLT optics, SNMP config/walks/profiles."""
from __future__ import annotations

from datetime import datetime, timezone

from wisp.central import inventory, onuroster
from wisp.central.api.common import (DENIED, body_org_write, device_read_scope,
                                     device_write_org, org_or_400, q_int_required,
                                     reader_or_401)


# ----- reads ---------------------------------------------------------------

def list_devices(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    h._reply(200, {"devices": h.store.list_org_devices(org)})


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


def optics(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    scope = device_read_scope(h, user, qs)
    if not scope:
        return
    did, org = scope
    dev = h.store.get_org_device(org, did) or {}
    # redundant-MAC groups are org-wide (a MAC cloned onto a second OLT is the
    # dangerous case); surface only the ones that touch THIS OLT in its panel
    dups = onuroster.duplicate_macs(h.store.org_onu_rows(org),
                                    datetime.now(timezone.utc))
    dup_macs = [d.as_dict() for d in dups
                if any(m["device_id"] == did for m in d.members)]
    h._reply(200, {
        "onus": h.store.list_onu_optics(org, did),
        "olt": h.store.get_olt_optics(org, did),
        "warn_dbm": dev.get("optical_warn_dbm") if dev.get("optical_warn_dbm") is not None else h.cfg.optical_warn_dbm,
        "crit_dbm": dev.get("optical_crit_dbm") if dev.get("optical_crit_dbm") is not None else h.cfg.optical_crit_dbm,
        "onu_pon_limit": dev.get("onu_pon_limit") if dev.get("onu_pon_limit") is not None else h.cfg.onu_pon_limit,
        "dup_macs": dup_macs,
    })


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


def location(h, user, body):
    did = int(body.get("id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    loc = inventory.clean_location_payload(body)
    ok = h.store.set_org_device_location(org, did, loc["lat"], loc["lng"])
    h._reply(200 if ok else 404, {"ok": ok})


def route(h, user, body):
    clean = inventory.clean_route_payload(body)
    org = device_write_org(h, user, clean["child_id"])
    if org is DENIED:
        return
    # geometry only attaches to a link that actually exists in this org
    child = h.store.get_org_device(org, clean["child_id"])
    if not child:
        h._reply(404, {"error": "device not found"})
        return
    if child.get("parent_device_id") != clean["parent_id"]:
        backups = {e["parent_id"] for e in h.store.org_device_backup_edges(org)
                   if e["child_id"] == clean["child_id"]}
        if clean["parent_id"] not in backups:
            raise inventory.InventoryError(
                "no link between those devices — set the parent first")
    h.store.set_link_route(org, clean["child_id"], clean["parent_id"],
                           clean["waypoints"], updated_by=user["username"])
    h._reply(200, {"ok": True})


def snmp(h, user, body):
    did = int(body.get("id") or 0)
    org = device_write_org(h, user, did)
    if org is DENIED:
        return
    clean = inventory.clean_snmp_payload(body)
    ok = h.store.set_org_device_snmp(org, did, clean)
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
