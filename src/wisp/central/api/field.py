from __future__ import annotations

from wisp.central import field, inventory
from wisp.central.api.common import DENIED, body_org_write, org_or_400, reader_or_401


def _self_org(h, user):

    if not user.get("org_id"):
        h._reply(400, {"error": "a shift belongs to an org account"})
        return None
    return user["org_id"]


def shift(h, qs):

    user = reader_or_401(h)
    if not user:
        return
    org = _self_org(h, user)
    if not org:
        return
    last = h.store.last_shift(org, user["id"])
    h._reply(200, {
        "on_shift": bool(last and not last["ended_at"]),
        "started_at": last["started_at"] if last else None,
        "ended_at": last["ended_at"] if last else None,
        "has_token": any(
            r["user_id"] == user["id"] and r["issued_at"] and not r["revoked_at"]
            for r in h.store.list_field_tokens(org)),
    })


def shift_write(h, user, body):

    org = _self_org(h, user)
    if not org:
        return
    action = str(body.get("action") or "").strip().lower()
    if action not in ("start", "end"):
        raise inventory.InventoryError("action must be 'start' or 'end'")
    if action == "start":
        row = h.store.start_shift(org, user["id"])
        h._reply(200, {"ok": True, "on_shift": True,
                       "started_at": row["started_at"],
                       "already": bool(row.get("already"))})
        return
    ended = h.store.end_shift(org, user["id"])
    h._reply(200, {"ok": True, "on_shift": False, "already": not ended})


def workers(h, qs):


    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    since = field.trail_since(h.cfg)
    rows = h.store.worker_tracking(org, trail_since=since)
    for r in rows:
        r["on_shift"] = bool(r["shift_started_at"] and not r["shift_ended_at"])
    h._reply(200, {
        "workers": rows,
        "trail_since": since,
        "fresh_s": h.cfg.field_track_fresh_s,
        "retention_days": h.cfg.field_track_retention_days,
    })


def _track_url(h) -> str:

    host = (h.headers.get("Host") or "").strip()
    if not host:
        return "/field/track"
    scheme = "https" if h.cfg.session_cookie_secure else "http"
    return f"{scheme}://{host}/field/track"


def tokens(h, qs):

    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    h._reply(200, {
        "accounts": h.store.list_field_tokens(org),
        "server_url": _track_url(h),
        "retention_days": h.cfg.field_track_retention_days,
    })


def token_issue(h, user, body):

    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org:
        h._reply(400, {"error": "org required"})
        return
    target = _target_user(h, org, body)
    if target is None:
        return
    token = h.store.issue_field_token(org, target, created_by=user["id"])
    h._reply(200, {"ok": True, "user_id": target, "token": token,
                   "server_url": _track_url(h)})


def token_revoke(h, user, body):

    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org:
        h._reply(400, {"error": "org required"})
        return
    target = _target_user(h, org, body)
    if target is None:
        return
    ok = h.store.revoke_field_token(org, target)
    h._reply(200, {"ok": ok})


def _target_user(h, org: str, body: dict) -> int | None:

    try:
        uid = int(body.get("user_id"))
    except (TypeError, ValueError):
        raise inventory.InventoryError("user_id is required")
    if not any(r["user_id"] == uid for r in h.store.list_field_tokens(org)):
        h._reply(404, {"error": "no such active account in this org"})
        return None
    return uid
