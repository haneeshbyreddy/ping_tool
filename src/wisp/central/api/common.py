"""Shared request helpers for the central API handler modules.

Every handler function receives the live request handler instance ``h``
(see ``server.py``). Services ride on it as class attributes — ``h.cfg``,
``h.store``, ``h.notifier``, ``h.registry`` — and the transport/auth
plumbing stays on the handler (``h._reply``, ``h._user``, ``h._reader``,
``h._scope_org``, ``h._can_write``, ``h._ingest_ok``).
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def public_user(user: dict, store) -> dict:
    org_name = store.org_name(user["org_id"]) if user["org_id"] else None
    return {"id": user["id"], "username": user["username"], "org_id": user["org_id"],
            "org_name": org_name, "role": user["role"], "is_superadmin": user["org_id"] is None}


def worker_org(store, worker_id) -> str | None:
    if worker_id is None:
        return None
    with store._connect() as conn:
        row = conn.execute("SELECT org_id FROM org_workers WHERE id=?",
                           (int(worker_id),)).fetchone()
    return row["org_id"] if row else None


def reader_or_401(h) -> dict | None:
    user = h._reader()
    if not user:
        h._reply(401, {"error": "unauthorized"})
    return user


def superadmin_or_403(h) -> dict | None:
    user = reader_or_401(h)
    if not user:
        return None
    if not user["is_superadmin"]:
        h._reply(403, {"error": "forbidden"})
        return None
    return user


def org_or_400(h, user, qs) -> str | None:
    org = h._scope_org(user, qs)
    if not org:
        h._reply(400, {"error": "org required"})
    return org


def q_int_required(h, qs, key: str) -> int | None:
    try:
        return int((qs.get(key) or [None])[0])
    except (TypeError, ValueError):
        h._reply(400, {"error": f"{key} required"})
        return None


def q_int_or(qs, key: str, fallback: int) -> int:
    try:
        return int((qs.get(key) or [fallback])[0])
    except (TypeError, ValueError):
        return fallback


def device_read_scope(h, user, qs) -> tuple[int, str] | None:
    """?device_id=N reads: parse the id, derive org from the DB row, 403 unless
    the caller is superadmin or in that org. Replies on failure."""
    did = q_int_required(h, qs, "device_id")
    if did is None:
        return None
    org = h.store.device_org(did)
    if org is None or not (user["is_superadmin"] or user["org_id"] == org):
        h._reply(403, {"error": "forbidden"})
        return None
    return did, org


def device_write_org(h, user, device_id: int):
    """Writes keyed by device id: org comes from the DB row (body org_id is
    never trusted), owner/superadmin only. Returns the org or DENIED after a
    403 — a superadmin hitting an unknown device gets org None and falls
    through to the store's own 404, exactly like the pre-split handler."""
    org = h.store.device_org(device_id)
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return DENIED
    return org


# Returned by body_org_write when the caller was rejected (403 already sent).
# A sentinel, not None — a superadmin writing with no org_id legitimately
# yields org=None and must not read as a denial.
DENIED = object()


def body_org_write(h, user, body: dict):
    """Writes scoped by body org_id (create) or the caller's own org.
    Returns the org (possibly None for superadmin) or DENIED after a 403."""
    org = body.get("org_id") or user["org_id"]
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return DENIED
    return org
