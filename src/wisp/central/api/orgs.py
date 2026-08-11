from __future__ import annotations

import logging
import re

from wisp.central import billing as billing_mod
from wisp.central import inventory, mapdetail, sysinfo, theme
from wisp.central.api.common import (DENIED, body_org_write, now_iso, org_or_400,
                                     public_user, reader_or_401,
                                     superadmin_or_403)

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_QR_MAX_CHARS = 700_000

log = logging.getLogger("wisp.central.api.orgs")


def _admin_whatsapp(h) -> list[str]:
    num = (h.store.whatsapp_settings().get("admin_number")
           or h.cfg.whatsapp_admin_number or "").strip()
    return [num] if num else []


def healthz(h, qs):
    h._reply(200, {"ok": True, "counts": h.store.counts()})


def me(h, qs):
    user = h._user()
    if not user:
        h._reply(401, {"error": "unauthorized"})
        return
    h._reply(200, {"user": public_user(user, h.store)})


def system(h, qs):
    if not superadmin_or_403(h):
        return
    doc = sysinfo.snapshot(h.cfg.central_db)
    doc["release_sync"] = h.store.release_sync_status()
    releases = h.store.list_releases()
    doc["latest_release"] = releases[0]["version"] if releases else None
    h._reply(200, doc)


def admin_overview(h, qs):
    if not superadmin_or_403(h):
        return
    h._reply(200, h.store.admin_overview())


def _whatsapp_public(h) -> dict:
    wa = h.store.whatsapp_settings()
    toggle = wa.get("enabled")
    if toggle in (None, ""):
        enabled = h.cfg.enable_whatsapp
    else:
        enabled = str(toggle).strip().lower() in ("1", "true", "yes", "on")
    return {"enabled": enabled,
            "phone_id": wa.get("phone_id") or "",
            "template": wa.get("template") or "",
            "lang": wa.get("lang") or "",
            "api_version": wa.get("api_version") or "",
            "admin_number": wa.get("admin_number") or "",
            "token_set": bool(wa.get("token"))}


def admin_settings(h, qs):
    if not superadmin_or_403(h):
        return
    h._reply(200, {"google_maps_key": h.store.get_setting("google_maps_key"),
                   "billing_gpay_number": billing_mod.gpay_number(h.store),
                   "billing_qr_image": h.store.get_setting("billing_qr_image"),
                   "whatsapp": _whatsapp_public(h),
                   "theme_overrides": theme.load(h.store),
                   "map_detail": mapdetail.load(h.store)})


def list_orgs(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = h._scope_org(user, qs)
    orgs = h.store.orgs()
    if org:
        orgs = [o for o in orgs if o["org_id"] == org]
    gkey = h.store.get_setting("google_maps_key")
    detail = mapdetail.load(h.store)
    for o in orgs:
        o["google_maps_key"] = gkey
        o["map_detail"] = detail
    if user["org_id"] and user["role"] == "worker":
        for o in orgs:
            for k in ("ntfy_topic", "ntfy_topic_owner", "ntfy_topic_worker"):
                o.pop(k, None)
    h._reply(200, {"orgs": orgs})


def create(h, user, body):
    if not user["is_superadmin"]:
        h._reply(403, {"error": "forbidden"})
        return
    org = inventory.clean_org_id(body.get("org_id"))
    if h.store.org_exists(org):
        h._reply(409, {"error": f"org {org!r} already exists"})
        return
    h.store.set_org(org, name=body.get("name"))
    h._reply(200, {"org_id": org})


def delete(h, user, body):


    if not superadmin_or_403(h):
        return
    try:
        org = inventory.clean_org_id(body.get("org_id"))
    except inventory.InventoryError as exc:
        h._reply(422, {"error": str(exc)})
        return
    if not h.store.org_exists(org):
        h._reply(404, {"error": f"org {org!r} not found"})
        return
    if str(body.get("confirm") or "").strip() != org:
        h._reply(422, {"error": "type the org id to confirm deletion"})
        return
    summary = h.store.org_summary(org)
    deleted = h.store.delete_org(org)
    h.registry.forget(org)
    log.warning("org %s DELETED by %s (%s devices, %s nodes, %s users)",
                org, user["username"], summary["devices"], summary["nodes"],
                summary["users"])
    h._reply(200, {"ok": True, "org_id": org, "deleted": deleted})


def update(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    map_region = body.get("map_region")
    if map_region is not None:
        map_region = str(map_region).strip().lower()[:64] or None
    if "poll_interval_s" in body:
        raw = body.get("poll_interval_s")
        if raw in (None, "", "null", 0, "0"):
            seconds = None
        else:
            try:
                seconds = int(raw)
            except (TypeError, ValueError):
                h._reply(422, {"error": "poll_interval_s must be a number of seconds"})
                return
            if not 10 <= seconds <= 120:
                h._reply(422, {"error": "poll_interval_s must be between 10 and 120 seconds"})
                return
        h.store.set_org_poll_interval(org, seconds)
    if "auto_update" in body:
        h.store.set_org_auto_update(org, bool(body.get("auto_update")))
    if "web_proxy" in body:
        if not user["is_superadmin"]:
            h._reply(403, {"error": "web_proxy is superadmin-set"})
            return
        h.store.set_org_web_proxy(org, bool(body.get("web_proxy")))
    h.store.set_org(org, name=body.get("name"), ntfy_topic=body.get("ntfy_topic"),
                    ntfy_topic_owner=body.get("ntfy_topic_owner"),
                    ntfy_topic_worker=body.get("ntfy_topic_worker"),
                    map_region=map_region)
    h._reply(200, {"ok": True})


def admin_settings_write(h, user, body):
    if not user["is_superadmin"]:
        h._reply(403, {"error": "forbidden"})
        return
    google_key = body.get("google_maps_key")
    if google_key is not None:
        h.store.set_setting("google_maps_key",
                            str(google_key).strip()[:128])
    gpay = body.get("billing_gpay_number")
    if gpay is not None:
        h.store.set_setting("billing_gpay_number", str(gpay).strip()[:32])
    qr = body.get("billing_qr_image")
    if qr is not None:
        qr = str(qr).strip()
        if qr and not qr.startswith("data:image/"):
            h._reply(422, {"error": "QR must be an uploaded image"})
            return
        if len(qr) > _QR_MAX_CHARS:
            h._reply(422, {"error": "QR image is too large. Use a smaller PNG."})
            return
        h.store.set_setting("billing_qr_image", qr)
    wa = body.get("whatsapp")
    if isinstance(wa, dict):
        if "enabled" in wa:
            h.store.set_setting("whatsapp_enabled", "1" if wa.get("enabled") else "0")
        for key, cap in (("phone_id", 64), ("template", 128), ("lang", 16),
                         ("api_version", 16), ("admin_number", 24)):
            if key in wa:
                h.store.set_setting(f"whatsapp_{key}", str(wa.get(key) or "").strip()[:cap])
        if wa.get("token"):
            h.store.set_setting("whatsapp_token", str(wa["token"]).strip()[:512])
        elif wa.get("token_clear"):
            h.store.set_setting("whatsapp_token", "")
    if "theme_overrides" in body:
        theme.save(h.store, body.get("theme_overrides"))
    if "map_detail" in body:
        mapdetail.save(h.store, body.get("map_detail"))
    h._reply(200, {"ok": True})


def billing(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    st = billing_mod.org_status(h.store, org)
    cap = billing_mod.device_cap(st["plan"])
    h._reply(200, {
        **st,
        "paid_months": sorted(h.store.paid_months(org)),
        "device_count": h.store.org_monitored_device_count(
            org, inventory.PASSIVE_TYPES),
        "device_cap": cap,
        "node_count": h.store.active_node_token_count(org),
        "node_cap": billing_mod.node_cap(st["plan"]),
        "gpay_number": billing_mod.gpay_number(h.store),
        "qr_image": h.store.get_setting("billing_qr_image"),
        "plans": billing_mod.PLANS,
    })


def billing_paid(h, user, body):
    org = org_or_400(h, user, body if isinstance(body, dict) else {})
    if not org:
        return
    numbers = _admin_whatsapp(h)
    if not numbers:
        h._reply(200, {"ok": True, "notified": False})
        return
    name = h.store.org_name(org) or org
    plan = billing_mod.PLANS.get(h.store.org_plan(org), {})
    st = billing_mod.org_status(h.store, org)
    due = st.get("due_month") or st.get("current_month") or ""
    body_line = f"{plan.get('label', '')} · ₹{plan.get('price_inr', '')}"
    if due:
        body_line += f" · {billing_mod.month_label(due)}"
    body_line += " · verify & mark the month paid"
    ok = False
    try:
        from wisp.egress.notifiers import WhatsAppFacts
        ok = h.notifier.send(
            f"💰 {name} says they've paid", body_line, 4, whatsapp=numbers,
            facts=WhatsAppFacts(subject=name, status="PAID (claim)",
                                detail=body_line, timestamp=now_iso())).ok
    except Exception:
        log.exception("payment-claim notification failed for %s", org)
    h._reply(200, {"ok": True, "notified": bool(ok)})


def billing_plan(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org or not h.store.org_exists(org):
        h._reply(404, {"error": "unknown org"})
        return
    plan = billing_mod.clean_plan(body.get("plan"))
    if plan != "free":
        h._reply(422, {"error": "only the free plan can be chosen without "
                                "payment. Pay the admin to move to a paid plan."})
        return
    prior = h.store.org_plan(org)
    if prior != "free":
        h.store.set_org_plan(org, "free")
        _notify_admin_plan_change(h, org, prior)
    st = billing_mod.org_status(h.store, org)
    h._reply(200, {"ok": True, **st,
                   "paid_months": sorted(h.store.paid_months(org))})


def _notify_admin_plan_change(h, org: str, prior: str) -> None:
    numbers = _admin_whatsapp(h)
    if not numbers:
        return
    try:
        name = h.store.org_name(org) or org
        from wisp.egress.notifiers import WhatsAppFacts
        detail = f"was {prior} · self-serve downgrade"
        h.notifier.send(f"📉 {name} switched to Free", detail, 3, whatsapp=numbers,
                        facts=WhatsAppFacts(subject=name, status="CHURN → Free",
                                            detail=detail, timestamp=now_iso()))
    except Exception:
        log.exception("plan-change notification failed for %s", org)


def admin_billing_write(h, user, body):
    if not user["is_superadmin"]:
        h._reply(403, {"error": "forbidden"})
        return
    org = str(body.get("org_id") or "").strip()
    if not org or not h.store.org_exists(org):
        h._reply(404, {"error": "unknown org"})
        return
    if body.get("plan") is not None:
        plan = billing_mod.clean_plan(body.get("plan"))
        if not plan:
            h._reply(422, {"error": "plan must be one of: "
                                    + ", ".join(billing_mod.PLANS)})
            return
        h.store.set_org_plan(org, plan)
    month = body.get("month")
    if month is not None:
        month = str(month).strip()
        if not _MONTH_RE.match(month):
            h._reply(422, {"error": "month must be YYYY-MM"})
            return
        h.store.set_billing_month(org, month, bool(body.get("paid")),
                                  marked_by=user["username"])
    st = billing_mod.org_status(h.store, org)
    h._reply(200, {"ok": True, **st,
                   "paid_months": sorted(h.store.paid_months(org))})


def test_alert(h, user, body):
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    whatsapp = list(h.store.org_alert_recipients(org))
    if not whatsapp:
        h._reply(422, {"error": "no WhatsApp recipients. Add WhatsApp numbers to "
                                "the team's owner/worker accounts first."})
        return
    from wisp.egress.notifiers import WhatsAppFacts
    body_line = f"This is a test alert for {org}."
    res = h.notifier.send(
        "✅ WISP Central test alert", body_line, 3, whatsapp=whatsapp,
        facts=WhatsAppFacts(subject=f"{org} (test)", status="TEST",
                            detail="channel test alert",
                            timestamp=now_iso()))
    h._reply(200, {"ok": res.ok, "detail": res.detail, "channel": h.notifier.channel,
                   "whatsapp_count": len(whatsapp)})
