"""Org-level and superadmin routes: org CRUD, server-wide settings, system
stats, coverage overview, test alerts, plan/billing."""
from __future__ import annotations

import logging
import re

from wisp.central import auth
from wisp.central import billing as billing_mod
from wisp.central import inventory, sysinfo, theme
from wisp.central.api.common import (DENIED, body_org_write, now_iso, org_or_400,
                                     public_user, reader_or_401,
                                     superadmin_or_403)

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# generous ceiling on the uploaded QR data-URI (~512 KB of base64); a real QR
# PNG is a few KB, so this only stops someone pasting a photo by mistake.
_QR_MAX_CHARS = 700_000

log = logging.getLogger("wisp.central.api.orgs")


def _admin_payments_topic(h) -> str | None:
    """Where an org's 'I've paid' ping lands: the dedicated payments channel
    if the superadmin set one, else the shared central/admin topic."""
    return h.store.get_setting("billing_paid_topic") or h.cfg.central_ntfy_topic


def healthz(h, qs):
    h._reply(200, {"ok": True, "counts": h.store.counts()})


def me(h, qs):
    user = h._user()
    if not user:
        h._reply(401, {"error": "unauthorized"})
        return
    h._reply(200, {"user": public_user(user, h.store),
                   "channels": {"central": h.cfg.central_ntfy_topic}})


def system(h, qs):
    if not superadmin_or_403(h):
        return
    doc = sysinfo.snapshot(h.cfg.central_db)
    # Monitor-the-monitor: a dead release mirror stalls fleet
    # self-updates, so its health rides the superadmin box-stats card.
    doc["release_sync"] = h.store.release_sync_status()
    releases = h.store.list_releases()
    doc["latest_release"] = releases[0]["version"] if releases else None
    h._reply(200, doc)


def admin_overview(h, qs):
    if not superadmin_or_403(h):
        return
    h._reply(200, h.store.admin_overview())


def _whatsapp_public(h) -> dict:
    """The superadmin's WhatsApp config for the settings form. The TOKEN is a
    secret and is NEVER echoed — only whether one is stored (`token_set`). The
    `enabled` flag falls back to the env default when the dashboard hasn't set
    it, matching how the notifier resolves it."""
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
            "token_set": bool(wa.get("token"))}


def admin_settings(h, qs):
    if not superadmin_or_403(h):
        return
    h._reply(200, {"google_maps_key": h.store.get_setting("google_maps_key"),
                   "billing_gpay_number": billing_mod.gpay_number(h.store),
                   # the QR image (a data URI) and the payments channel aren't
                   # secret — echo them back so the settings page can preview
                   # and edit them
                   "billing_qr_image": h.store.get_setting("billing_qr_image"),
                   "billing_paid_topic":
                       h.store.get_setting("billing_paid_topic") or "",
                   # experimental WhatsApp channel — server-wide config (numbers
                   # are per-account, set in Accounts, not here)
                   "whatsapp": _whatsapp_public(h),
                   # sparse colour diff over the shipped palette; `{}` means a
                   # stock theme, NOT "no colours" (see central/theme.py)
                   "theme_overrides": theme.load(h.store)})


def list_orgs(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = h._scope_org(user, qs)
    orgs = h.store.orgs()
    if org:
        orgs = [o for o in orgs if o["org_id"] == org]
    # the ONE superadmin-pasted Google Maps key rides every org
    # row, so each org's Map view lights up without its own key
    gkey = h.store.get_setting("google_maps_key")
    for o in orgs:
        o["google_maps_key"] = gkey
    # A read-only worker reads this row for the org name and the Maps key, but the
    # ntfy paging topics are a capability (subscribe to every page, POST spoofed
    # ones), not just data — keep them owner/superadmin-only. Nothing a worker
    # renders needs them.
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
    """Erase an org and every row scoped to it. Superadmin-only, irreversible.

    Guarded by an ECHOED org id (`confirm`), not just the role: this is the one
    dashboard action with no undo and no backup — an org's devices, outage
    history, billing months and login accounts all go at once. The typed echo
    is what makes a mis-click impossible; the server enforces it so the check
    can't be lost to a SPA refactor.

    Deliberately NOT a tombstone: `_ensure_org` on the ingest path is how a new
    probe bootstraps its org, so an edge still pointed here re-creates the row
    (empty — devices/topics/plan are gone). Blocking that would break
    self-enrollment for everyone to tidy one case; the dialog says to uninstall
    the probe instead. The node's token IS purged here, so any deployment with
    ingest auth configured rejects it outright.
    """
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
    # the live engine is in-memory only and org ids are reusable — a stale one
    # would hand a later org of the same name this org's FSM state
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
            seconds = None  # back to automatic (edge env/adaptive default)
        else:
            try:
                seconds = int(raw)
            except (TypeError, ValueError):
                h._reply(422, {"error": "poll_interval_s must be a number of seconds"})
                return
            # 120s cap: the fleet watchdog pages NODE_STALE at 180s (default) —
            # a legitimate cadence must never look like a dead probe.
            if not 10 <= seconds <= 120:
                h._reply(422, {"error": "poll_interval_s must be between 10 and 120 seconds"})
                return
        h.store.set_org_poll_interval(org, seconds)
    if "auto_update" in body:
        # Fleet auto-update: central arms the staged rollout itself when the
        # release mirror gets ahead of the fleet (rollout.maybe_auto_rollout).
        h.store.set_org_auto_update(org, bool(body.get("auto_update")))
    if "web_proxy" in body:
        # Web-UI proxy capability (webplan.md §6.7): a blast-radius switch,
        # not an org preference — only the superadmin grants or revokes it.
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
    # server-wide, superadmin-only: the Google Maps key is pasted
    # ONCE here and served to every org (browser-exposed by design,
    # referrer-restricted — central never calls Google)
    if not user["is_superadmin"]:
        h._reply(403, {"error": "forbidden"})
        return
    google_key = body.get("google_maps_key")
    if google_key is not None:
        h.store.set_setting("google_maps_key",
                            str(google_key).strip()[:128])
    gpay = body.get("billing_gpay_number")
    if gpay is not None:
        # blank falls back to billing.DEFAULT_GPAY_NUMBER
        h.store.set_setting("billing_gpay_number", str(gpay).strip()[:32])
    # QR image the org scans to pay: a data URI ("data:image/png;base64,…").
    # Blank clears it (the lock screen falls back to just the GPay number).
    qr = body.get("billing_qr_image")
    if qr is not None:
        qr = str(qr).strip()
        if qr and not qr.startswith("data:image/"):
            h._reply(422, {"error": "QR must be an uploaded image"})
            return
        if len(qr) > _QR_MAX_CHARS:
            h._reply(422, {"error": "QR image is too large — use a smaller PNG"})
            return
        h.store.set_setting("billing_qr_image", qr)
    # Dedicated ntfy channel for "I've paid" pings; blank falls back to the
    # central/admin topic (see _admin_payments_topic).
    paid_topic = body.get("billing_paid_topic")
    if paid_topic is not None:
        h.store.set_setting("billing_paid_topic", str(paid_topic).strip()[:128])
    # Experimental WhatsApp channel config (app_settings, read fresh by the
    # notifier). The token is write-only: a blank field LEAVES the stored one
    # alone (so a routine save can't wipe the secret) — the SPA omits it unless
    # the superadmin typed a new one, and a `token_clear` flag removes it.
    wa = body.get("whatsapp")
    if isinstance(wa, dict):
        if "enabled" in wa:
            # store "1"/"0" (both non-empty, so a disable persists rather than
            # deleting the row and falling back to the env default)
            h.store.set_setting("whatsapp_enabled", "1" if wa.get("enabled") else "0")
        for key, cap in (("phone_id", 64), ("template", 128), ("lang", 16),
                         ("api_version", 16)):
            if key in wa:
                h.store.set_setting(f"whatsapp_{key}", str(wa.get(key) or "").strip()[:cap])
        if wa.get("token"):
            h.store.set_setting("whatsapp_token", str(wa["token"]).strip()[:512])
        elif wa.get("token_clear"):
            h.store.set_setting("whatsapp_token", "")
    # Server-wide colour overrides. Posting `{}` resets every org to the
    # shipped palette — that IS the reset button, so an empty dict has to be
    # distinguishable from the key being absent (absent = don't touch colours).
    if "theme_overrides" in body:
        theme.save(h.store, body.get("theme_overrides"))
    h._reply(200, {"ok": True})


def billing(h, qs):
    """Org-scoped plan + payment status. Deliberately readable while LOCKED —
    the lock screen renders from this (see server.py's _billing_blocked)."""
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
        # optional payment QR (a data URI) the lock screen renders beside the
        # GPay number; null when the admin hasn't uploaded one
        "qr_image": h.store.get_setting("billing_qr_image"),
        "plans": billing_mod.PLANS,
    })


def billing_paid(h, user, body):
    """"I've paid": the org tells the platform admin a manual GPay/QR payment
    is on its way. Pings the dedicated payments channel with the org name so
    the admin can verify and mark the month. Deliberately billing-exempt
    (server.py) — a LOCKED org taps this from the lock screen. Any signed-in
    member of the org may send it; there is nothing to authorize, only to
    notify."""
    org = org_or_400(h, user, body if isinstance(body, dict) else {})
    if not org:
        return
    topic = _admin_payments_topic(h)
    if not topic:
        # no admin channel configured — nothing to notify, but don't error the
        # user (their payment still stands; the admin reconciles by hand)
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
        ok = h.notifier.send(topic, f"💰 {name} says they've paid",
                             body_line, 4).ok
    except Exception:
        log.exception("payment-claim notification failed for %s", org)
    h._reply(200, {"ok": True, "notified": bool(ok)})


def billing_plan(h, user, body):
    """Self-serve plan change WITHOUT payment: only 'free'. Paid plans are
    entered by paying (GPay/QR) and the admin marking the month. Billing-exempt:
    the escape hatch for a locked org that would rather drop to Free than pay.
    Existing devices keep working; the free caps only stop new creates.
    Owner-only."""
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org or not h.store.org_exists(org):
        h._reply(404, {"error": "unknown org"})
        return
    plan = billing_mod.clean_plan(body.get("plan"))
    if plan != "free":
        h._reply(422, {"error": "only the free plan can be chosen without "
                                "payment — pay the admin to move to a paid plan"})
        return
    prior = h.store.org_plan(org)
    if prior != "free":
        h.store.set_org_plan(org, "free")
        _notify_admin_plan_change(h, org, prior)
    st = billing_mod.org_status(h.store, org)
    h._reply(200, {"ok": True, **st,
                   "paid_months": sorted(h.store.paid_months(org))})


def _notify_admin_plan_change(h, org: str, prior: str) -> None:
    # best-effort heads-up on the payments channel — a lost churn signal must
    # never 500 the downgrade
    topic = _admin_payments_topic(h)
    if not topic:
        return
    try:
        name = h.store.org_name(org) or org
        h.notifier.send(topic, f"📉 {name} switched to Free",
                        f"was {prior} — self-serve downgrade", 3)
    except Exception:
        log.exception("plan-change notification failed for %s", org)


def admin_billing_write(h, user, body):
    # Superadmin-only: set an org's plan and/or toggle a paid month. Marking
    # future months ahead of time IS the "no reminder this cycle" mechanism —
    # the sweeper only pages when the paid runway actually runs short.
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
    role = str(body.get("role") or "").strip().lower()
    if role not in auth.ROLES:
        h._reply(422, {"error": "role must be one of: " + ", ".join(auth.ROLES)})
        return
    topic = h.store.org_role_topic(org, role)
    # WhatsApp fans out to the same role's per-account numbers — so this button
    # verifies the WhatsApp channel too (Stage B: ntfy off, WhatsApp on).
    whatsapp = h.store.org_role_whatsapp(org, role)
    if not topic and not whatsapp:
        h._reply(422, {"error": f"no {role} channel configured — set an ntfy topic "
                                "or add WhatsApp numbers to the team's accounts first"})
        return
    from wisp.egress.notifiers import WhatsAppFacts
    body_line = f"This is a test alert for {org}'s {role} channel."
    res = h.notifier.send(
        topic, "✅ WISP Central test alert", body_line, 3, whatsapp=whatsapp,
        facts=WhatsAppFacts(subject=f"{org} · {role}", status="TEST",
                            detail="channel test alert",
                            timestamp=now_iso()))
    h._reply(200, {"ok": res.ok, "detail": res.detail, "channel": h.notifier.channel,
                   "recipient": topic, "role": role,
                   "whatsapp_count": len(whatsapp)})
