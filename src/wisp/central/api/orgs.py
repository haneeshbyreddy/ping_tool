"""Org-level and superadmin routes: org CRUD, server-wide settings, system
stats, coverage overview, test alerts, plan/billing."""
from __future__ import annotations

import logging
import re

from wisp.central import billing as billing_mod
from wisp.central import inventory, razorpay, sysinfo
from wisp.central.api.common import (DENIED, body_org_write, org_or_400,
                                     public_user, reader_or_401,
                                     superadmin_or_403)

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

log = logging.getLogger("wisp.central.api.orgs")


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


def admin_settings(h, qs):
    if not superadmin_or_403(h):
        return
    h._reply(200, {"google_maps_key": h.store.get_setting("google_maps_key"),
                   "billing_gpay_number": billing_mod.gpay_number(h.store),
                   "razorpay_key_id": h.store.get_setting("razorpay_key_id"),
                   # the secret never leaves central — the UI only learns
                   # whether one is configured
                   "razorpay_key_secret_set":
                       bool(h.store.get_setting("razorpay_key_secret"))})


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
    if "web_proxy" in body:
        # Web-UI proxy capability (webplan.md §6.7): a blast-radius switch,
        # not an org preference — only the superadmin grants or revokes it.
        if not user["is_superadmin"]:
            h._reply(403, {"error": "web_proxy is superadmin-set"})
            return
        h.store.set_org_web_proxy(org, bool(body.get("web_proxy")))
    h.store.set_org(org, name=body.get("name"), ntfy_topic=body.get("ntfy_topic"),
                    ntfy_topic_owner=body.get("ntfy_topic_owner"),
                    ntfy_topic_operator=body.get("ntfy_topic_operator"),
                    ntfy_topic_tech=body.get("ntfy_topic_tech"),
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
    # Razorpay keys (central/razorpay.py): blank clears — no keys means the
    # Pay buttons vanish everywhere and billing falls back to the GPay flow
    rzp_id = body.get("razorpay_key_id")
    if rzp_id is not None:
        h.store.set_setting("razorpay_key_id", str(rzp_id).strip()[:64])
    rzp_secret = body.get("razorpay_key_secret")
    if rzp_secret is not None:
        h.store.set_setting("razorpay_key_secret", str(rzp_secret).strip()[:64])
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
        # Checkout needs the public key id in the browser; None = Razorpay
        # not configured, the UI falls back to the manual GPay flow
        "razorpay_key_id": h.payments.key_id if h.payments.enabled else None,
        "plans": billing_mod.PLANS,
    })


def billing_order(h, user, body):
    """Create a Razorpay order for N months of a paid plan. Deliberately
    billing-exempt (server.py) — a LOCKED org pays its way out from the lock
    screen. Owner-only, like every other org write."""
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org or not h.store.org_exists(org):
        h._reply(404, {"error": "unknown org"})
        return
    if not h.payments.enabled:
        h._reply(422, {"error": "online payments are not configured — "
                                "pay by GPay instead"})
        return
    plan = billing_mod.clean_plan(body.get("plan") or h.store.org_plan(org))
    if plan not in billing_mod.PAID_PLANS:
        h._reply(422, {"error": "plan must be one of: "
                                + ", ".join(billing_mod.PAID_PLANS)})
        return
    raw_months = body.get("months")
    try:
        count = 1 if raw_months in (None, "") else int(raw_months)
    except (TypeError, ValueError):
        h._reply(422, {"error": "months must be a number"})
        return
    if not 1 <= count <= 12:
        h._reply(422, {"error": "months must be between 1 and 12"})
        return
    months = billing_mod.months_to_pay(plan, h.store.paid_months(org), count)
    amount = billing_mod.PLANS[plan]["price_inr"] * count * 100  # paise
    try:
        # network call — after this point only fast local writes (dispatch rule)
        order = h.payments.create_order(
            amount, receipt=f"{org[:30]}-{count}m",
            notes={"org_id": org, "plan": plan, "months": ",".join(months)})
    except razorpay.GatewayError as exc:
        h._reply(502, {"error": f"payment gateway error: {exc}"})
        return
    h.store.create_billing_payment(order["id"], org, plan, months, amount,
                                   created_by=user["username"])
    label = billing_mod.PLANS[plan]["label"]
    desc = f"{label} plan · {billing_mod.month_label(months[0])}"
    if len(months) > 1:
        desc += f" – {billing_mod.month_label(months[-1])} ({len(months)} months)"
    h._reply(200, {"order_id": order["id"], "amount": amount,
                   "currency": "INR", "key_id": h.payments.key_id,
                   "plan": plan, "months": months,
                   "org_name": h.store.org_name(org) or org,
                   "description": desc})


def billing_plan(h, user, body):
    """Self-serve plan change WITHOUT payment: only 'free' — every paid plan
    is entered by paying for it (billing_order/verify apply the new plan).
    Billing-exempt: the escape hatch for a locked org that would rather drop
    to Free than pay. Existing devices keep working; the free caps only stop
    new creates. Owner-only."""
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org or not h.store.org_exists(org):
        h._reply(404, {"error": "unknown org"})
        return
    plan = billing_mod.clean_plan(body.get("plan"))
    if plan != "free":
        h._reply(422, {"error": "only the free plan can be chosen without "
                                "payment — upgrades go through checkout"})
        return
    prior = h.store.org_plan(org)
    if prior != "free":
        h.store.set_org_plan(org, "free")
        _notify_admin_plan_change(h, org, prior)
    st = billing_mod.org_status(h.store, org)
    h._reply(200, {"ok": True, **st,
                   "paid_months": sorted(h.store.paid_months(org))})


def _notify_admin_plan_change(h, org: str, prior: str) -> None:
    # best-effort heads-up, same channel as payment notices — a lost churn
    # signal must never 500 the downgrade
    topic = h.cfg.central_ntfy_topic
    if not topic:
        return
    try:
        name = h.store.org_name(org) or org
        h.notifier.send(topic, f"📉 {name} switched to Free",
                        f"was {prior} — self-serve downgrade", 3)
    except Exception:
        log.exception("plan-change notification failed for %s", org)


def billing_verify(h, user, body):
    """Finalize checkout: verify Razorpay's HMAC over order|payment, then —
    exactly once per order — apply the plan and mark the months paid.
    Idempotent: re-submitting a settled order just re-reads status."""
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    order_id = str(body.get("razorpay_order_id") or "")
    payment_id = str(body.get("razorpay_payment_id") or "")
    signature = str(body.get("razorpay_signature") or "")
    pay = h.store.billing_payment(order_id)
    if not pay or (org and pay["org_id"] != org):
        h._reply(404, {"error": "unknown order"})
        return
    org = pay["org_id"]  # superadmin verifying without an org scope
    if not h.payments.verify_signature(order_id, payment_id, signature):
        h._reply(422, {"error": "payment signature mismatch — payment "
                                "not verified"})
        return
    if h.store.settle_billing_payment(order_id, payment_id):
        if h.store.org_plan(org) != pay["plan"]:
            h.store.set_org_plan(org, pay["plan"])
        for m in pay["months"]:
            h.store.set_billing_month(org, m, True,
                                      marked_by=f"razorpay:{payment_id}")
        _notify_admin_payment(h, org, pay, payment_id)
    st = billing_mod.org_status(h.store, org)
    h._reply(200, {"ok": True, **st,
                   "paid_months": sorted(h.store.paid_months(org))})


def _notify_admin_payment(h, org: str, pay: dict, payment_id: str) -> None:
    # Heads-up on the platform admin's central channel — the payment is
    # already settled, so a failed page must never 500 the checkout.
    topic = h.cfg.central_ntfy_topic
    if not topic:
        return
    try:
        name = h.store.org_name(org) or org
        h.notifier.send(
            topic, f"💰 {name} paid ₹{pay['amount_paise'] // 100:,}",
            f"{pay['plan']} · {', '.join(pay['months'])} · Razorpay {payment_id}",
            3)
    except Exception:
        log.exception("payment notification failed for %s", org)


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
    if role not in ("owner", "operator", "tech"):
        h._reply(422, {"error": "role must be one of: owner, operator, tech"})
        return
    topic = h.store.org_role_topic(org, role)
    if not topic:
        h._reply(422, {"error": f"no {role} channel configured — set it in "
                                "Settings first"})
        return
    res = h.notifier.send(topic, "✅ WISP Central test alert",
                          f"This is a test alert for {org}'s {role} channel.", 3)
    h._reply(200, {"ok": res.ok, "detail": res.detail, "channel": h.notifier.channel,
                   "recipient": topic, "role": role})
