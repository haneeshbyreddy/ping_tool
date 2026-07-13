"""Probe-node enrollment tokens and manual fleet updates."""
from __future__ import annotations

from wisp.central import inventory
from wisp.central.api.common import (DENIED, body_org_write, org_or_400,
                                     reader_or_401)
from wisp.version import is_newer


def nodes(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    releases = h.store.list_releases()
    h._reply(200, {
        "nodes": h.store.list_node_tokens(org),
        "latest_version": releases[0]["version"] if releases else None,
        "rollout": h.store.get_rollout(org),
    })


def register(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    node_id = inventory.clean_node_id(body.get("node_id"))
    if h.store.get_node_token_status(org, node_id):
        raise inventory.InventoryError(
            f"node {node_id!r} is already registered for {org!r} — "
            "use rotate instead of registering it again")
    node_token = h.store.issue_node_token(org, node_id, created_by=user["id"])
    h._reply(200, {"node_id": node_id, "token": node_token})


def rotate(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    node_id = inventory.clean_node_id(body.get("node_id"))
    if not h.store.get_node_token_status(org, node_id):
        raise inventory.InventoryError(
            f"node {node_id!r} isn't registered for {org!r} yet")
    node_token = h.store.issue_node_token(org, node_id, created_by=user["id"])
    h._reply(200, {"node_id": node_id, "token": node_token})


def revoke(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    node_id = inventory.clean_node_id(body.get("node_id"))
    ok = h.store.revoke_node_token(org, node_id)
    h._reply(200 if ok else 404, {"ok": ok})


def delete(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    node_id = inventory.clean_node_id(body.get("node_id"))
    ok = h.store.delete_node_token(org, node_id)
    if ok:
        h._reply(200, {"ok": True})
    else:
        h._reply(404, {"ok": False, "error": f"{node_id!r} isn't registered"})


def update(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    node_id = inventory.clean_node_id(body.get("node_id"))
    releases = h.store.list_releases()
    if not releases:
        raise inventory.InventoryError("no release published yet")
    target = releases[0]["version"]
    node = next((n for n in h.store.node_versions(org)
                 if n["node_id"] == node_id), None)
    if node is None:
        raise inventory.InventoryError(
            f"{node_id!r} has never reported — the update directive rides "
            "its heartbeat, so there is no channel to deliver it through yet")
    if not is_newer(target, node.get("version")):
        raise inventory.InventoryError(
            f"{node_id!r} already runs {node.get('version')} — the latest "
            f"published release is {target}")
    h.store.set_rollout(org, target, [node_id],
                        note=f"manual update via dashboard ({user['username']})")
    h._reply(200, {"ok": True, "target_version": target})
