from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wisp.core.analytics import _parse
from wisp.core.state_machine import DOWN_FAMILY


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def olt_liveness(devs: list[dict], now: datetime, node_stale_s: int
                 ) -> tuple[set[int], set[int]]:


    cutoff = now.replace(tzinfo=None) - timedelta(seconds=node_stale_s)

    def _fresh_state(d: dict) -> bool:
        ts = d.get("state_updated_at")
        if not ts:
            return False
        try:
            return _parse(ts) >= cutoff
        except (ValueError, TypeError):
            return False

    stale = {d["id"] for d in devs if not _fresh_state(d)}
    down = {d["id"] for d in devs
            if d.get("state") in DOWN_FAMILY and d["id"] not in stale}
    return down, stale


def public_user(user: dict, store) -> dict:
    org_name = store.org_name(user["org_id"]) if user["org_id"] else None
    return {"id": user["id"], "username": user["username"], "org_id": user["org_id"],
            "org_name": org_name, "role": user["role"],
            "whatsapp_number": user.get("whatsapp_number"),
            "totp_enabled": bool(user.get("totp_enabled")),
            "is_superadmin": user["org_id"] is None}


def can_triage(user: dict, org: str | None) -> bool:
    if user["is_superadmin"]:
        return True
    return user["org_id"] == org and user["role"] in ("owner", "worker")


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
    did = q_int_required(h, qs, "device_id")
    if did is None:
        return None
    org = h.store.device_org(did)
    if org is None or not (user["is_superadmin"] or user["org_id"] == org):
        h._reply(403, {"error": "forbidden"})
        return None
    return did, org


def device_write_org(h, user, device_id: int):
    org = h.store.device_org(device_id)
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return DENIED
    return org


def can_survey(user, org: str | None) -> bool:

    if user["is_superadmin"]:
        return True
    if user["org_id"] != org or org is None:
        return False
    return user.get("role") in ("owner", "worker")


def survey_write_org(h, user, device_id: int):
    org = h.store.device_org(device_id)
    if not can_survey(user, org):
        h._reply(403, {"error": "forbidden"})
        return DENIED
    return org


DENIED = object()


def body_org_write(h, user, body: dict):
    org = body.get("org_id") or user["org_id"]
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return DENIED
    return org
