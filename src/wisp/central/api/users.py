"""Dashboard accounts, field-worker roster and attendance."""
from __future__ import annotations

from wisp.central import auth
from wisp.central.api.common import (DENIED, body_org_write, org_or_400,
                                     reader_or_401, worker_org)


def list_users(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = h._scope_org(user, qs)
    if not user["is_superadmin"] and user["role"] != "owner":
        h._reply(403, {"error": "forbidden"})
        return
    h._reply(200, {"users": h.store.list_users(org_id=org)})


def team(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    h._reply(200, {"team": h.store.list_workers(org)})


def attendance(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    h._reply(200, h.store.attendance_overview(org))


def create(h, user, body):
    org = body.get("org_id") if user["is_superadmin"] else user["org_id"]
    if not (user["is_superadmin"] or user["role"] == "owner"):
        h._reply(403, {"error": "forbidden"})
        return
    uid = auth.create_user(h.store, org, body.get("username", ""),
                           body.get("password", ""), body.get("role", "operator"))
    h._reply(200, {"id": uid})


def deactivate(h, user, body):
    if not (user["is_superadmin"] or user["role"] == "owner"):
        h._reply(403, {"error": "forbidden"})
        return
    target = h.store.get_user(int(body["id"]))
    if target and (user["is_superadmin"] or target["org_id"] == user["org_id"]):
        h.store.set_user_active(int(body["id"]), bool(body.get("active", False)))
        h._reply(200, {"ok": True})
    else:
        h._reply(403, {"error": "forbidden"})


def delete(h, user, body):
    if not (user["is_superadmin"] or user["role"] == "owner"):
        h._reply(403, {"error": "forbidden"})
        return
    target_id = int(body.get("id") or 0)
    if target_id == user["id"]:
        h._reply(422, {"error": "cannot delete your own account"})
        return
    target = h.store.get_user(target_id)
    if target and (user["is_superadmin"] or target["org_id"] == user["org_id"]):
        h.store.delete_user(target_id)
        h._reply(200, {"ok": True})
    else:
        h._reply(403, {"error": "forbidden"})


def password(h, user, body):
    target_id = int(body.get("id") or user["id"])
    if target_id == user["id"]:
        if not auth.verify_login(h.store, user["username"], body.get("current_password", "")):
            h._reply(422, {"error": "current password is incorrect"})
            return
    else:
        if not (user["is_superadmin"] or user["role"] == "owner"):
            h._reply(403, {"error": "forbidden"})
            return
        target = h.store.get_user(target_id)
        if not target or not (user["is_superadmin"] or target["org_id"] == user["org_id"]):
            h._reply(403, {"error": "forbidden"})
            return
    auth.set_password(h.store, target_id, body.get("new_password", ""))
    h._reply(200, {"ok": True})


def team_add(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    wid = h.store.add_worker(org, body["name"], body.get("role", "operator"),
                             body.get("region"), body.get("notes"))
    h._reply(200, {"id": wid})


def team_update(h, user, body):
    w = worker_org(h.store, body.get("id"))
    if not h._can_write(user, w):
        h._reply(403, {"error": "forbidden"})
        return
    fields = {k: body[k] for k in ("name", "role", "region", "notes") if k in body}
    h.store.update_worker(int(body["id"]), **fields)
    h._reply(200, {"ok": True})


def team_delete(h, user, body):
    w = worker_org(h.store, body.get("id"))
    if not h._can_write(user, w):
        h._reply(403, {"error": "forbidden"})
        return
    h.store.delete_worker(int(body["id"]))
    h._reply(200, {"ok": True})


def attendance_set(h, user, body):
    w = worker_org(h.store, body.get("worker_id"))
    if not h._can_write(user, w):
        h._reply(403, {"error": "forbidden"})
        return
    h.store.set_attendance(w, int(body["worker_id"]), bool(body.get("present")),
                           body.get("day"))
    h._reply(200, {"ok": True})
