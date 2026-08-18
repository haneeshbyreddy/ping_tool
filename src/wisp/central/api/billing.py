"""Billing v2 API: the metered postpaid ledger's read and write surface.

Owner-only on BOTH layers (route table + `_can_write`), the customers-page
discipline: the ledger carries what an org pays and is not a worker's
business. `/api/billing` is deliberately OUT of `_WORKER_GET`.

Every route here is in server.py's `_BILLING_EXEMPT` except the superadmin
console, which the gate never reaches anyway. A locked org MUST be able to see
its bill and pay it: gating the pay screen behind the paywall it exists to
clear is the one unforgivable own-goal.

There is NO owner-facing write that moves the bill. The self-declared count
was removed on the operator's call (2026-08-17): a customer typing their own
billable number is not a measurement, and the `declared` rung it fed is gone
from the ladder entirely. The bill is metered per ONU off the roster the OLTs
walk; an org whose OLTs report no roster counts zero and pays the device
floor, which is an honest answer.

The payment webhook is NOT an `/api/*` route (`/payments/webhook`, wired in
server.py beside `/whatsapp/webhook`): it carries no session, must skip both
the billing gate and the worker gate, and needs the RAW body for its HMAC.
"""

from __future__ import annotations

import logging
import re

from wisp.central import billing as billing_mod
from wisp.central import billing_pdf, inventory, metering, payments
from wisp.central.api.common import (DENIED, body_org_write, now_iso,
                                     org_or_400, reader_or_401,
                                     superadmin_or_403)

log = logging.getLogger("wisp.central.api.billing")

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# A single payment is capped well above any real bill and well below a
# fat-finger. Credit is legitimate (advance payment IS the credit mechanism),
# so the cap is generous rather than tied to the outstanding amount.
_MAX_PAY_PAISE = 100_000_000          # Rs 10,00,000


def _admin_contact(h) -> str:
    """The ops number an owner is told to contact while payments are dormant."""
    return (h.store.whatsapp_settings().get("admin_number")
            or h.cfg.whatsapp_admin_number or "").strip()


def _payment_public(h) -> dict:
    """What the SPA may know about the gateway. Dormant until configured: the
    pay button becomes an honest sentence, never a broken button."""
    provider = payments.get_provider(h.store, h.secretbox, h.cfg)
    if provider is None:
        return {"enabled": False, "provider": None, "key_id": None,
                "admin_contact": _admin_contact(h)}
    return {"enabled": True, "provider": provider.name,
            "key_id": provider.key_id, "admin_contact": _admin_contact(h)}


def _org_document(h, org: str) -> dict:
    """The whole billing document for one org. One shape for every state: the
    SPA composes the honest headline from these facts and guesses nothing."""
    st = billing_mod.org_status(h.store, org)
    today = billing_mod.operator_today(h.cfg)
    month = metering.month_key(today)
    accruals = h.store.accruals_for_month(org, month)
    return {
        **st,
        "org_name": h.store.org_name(org) or org,
        "device_count": h.store.org_monitored_device_count(
            org, inventory.PASSIVE_TYPES),
        "month": month,
        "month_label": metering.month_label(month),
        "month_to_date_paise": sum(int(a["paise"]) for a in accruals),
        "days_in_month": metering.days_in_month(month),
        "accruals": accruals,
        "invoices": h.store.org_invoices(org),
        "payments": h.store.org_payments(org),
        "payment": _payment_public(h),
    }


# --------------------------------------------------------------- owner reads

def billing(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    h._reply(200, _org_document(h, org))


def invoice_pdf(h, qs):
    """One invoice as a PDF. The daily table is the invoice's OWN accrual
    rows, and the total is the stored number printed verbatim: the bill
    always equals what the chart shows."""
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    month = str((qs.get("month") or [""])[0]).strip()
    if not _MONTH_RE.match(month):
        h._reply(400, {"error": "month must be YYYY-MM"})
        return
    invoice = h.store.org_invoice(org, month)
    if not invoice:
        h._reply(404, {"error": "no invoice for that month"})
        return
    body = billing_pdf.render_invoice(
        org_name=h.store.org_name(org) or org, org_id=org, invoice=invoice,
        accruals=h.store.accruals_for_month(org, month),
        payments=h.store.org_payments(org),
        outstanding_paise=h.store.outstanding_paise(org),
        tz_name=h.cfg.display_tz)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", org).strip("-") or "org"
    h._send_binary(200, "application/pdf", body,
                   filename=f"invoice-{safe}-{month}.pdf")


# -------------------------------------------------------------- owner writes

def pay(h, user, body):
    """Create a gateway order. The amount is the SPA's, not ours: partial
    payments are legitimate and so is paying ahead (that is the credit
    mechanism). We never invent an amount the payer did not choose."""
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org or not h.store.org_exists(org):
        h._reply(404, {"error": "unknown org"})
        return
    provider = payments.get_provider(h.store, h.secretbox, h.cfg)
    if provider is None:
        contact = _admin_contact(h)
        h._reply(503, {"error": "Online payment is not yet enabled. "
                                + (f"Contact {contact}." if contact
                                   else "Contact your administrator."),
                       "enabled": False})
        return
    try:
        paise = int(body.get("paise"))
    except (TypeError, ValueError):
        paise = 0
    if paise <= 0:
        h._reply(422, {"error": "enter an amount to pay"})
        return
    if paise > _MAX_PAY_PAISE:
        h._reply(422, {"error": "that amount is larger than this gateway "
                                "accepts in one payment. Pay in parts."})
        return
    # The receipt is the gateway's own reference and is length-capped there;
    # org plus a second-resolution stamp is unique enough to find a payment by.
    receipt = f"{org}-{now_iso()}"[:40]
    try:
        order = provider.create_order(org, paise, receipt)
    except payments.PaymentError as exc:
        log.warning("payment order failed for %s: %s", org, exc)
        h._reply(502, {"error": str(exc)})
        return
    h._reply(200, {"ok": True, **order})


def payment_return(h, user, body):
    """The browser's post-checkout return, verified for INSTANT feedback only.

    The webhook is the source of truth and the only path that records money:
    the return signature covers order id and payment id but NOT the amount, so
    posting a ledger row from here would let a client name its own figure. A
    verified return means "your payment went through, the ledger is catching
    up" and the SPA shows processing until the webhook lands."""
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    provider = payments.get_provider(h.store, h.secretbox, h.cfg)
    if provider is None:
        h._reply(503, {"error": "Online payment is not enabled."})
        return
    ok = False
    try:
        ok = bool(provider.verify_return(body if isinstance(body, dict) else {}))
    except Exception:
        log.exception("payment return verification failed for %s", org)
    if not ok:
        h._reply(200, {"ok": False, "verified": False,
                       "error": "We could not verify that payment. If money "
                                "left your account it will still be recorded. "
                                "Check the payment history in a minute."})
        return
    h._reply(200, {"ok": True, "verified": True,
                   "outstanding_paise": h.store.outstanding_paise(org)})


def plan(h, user, body):
    """Plans died with billing v1 (operator decision, 2026-08-17). A stale SPA
    tab still posting here gets a sentence, never a 404: the SPA deploys
    instantly and central needs a restart, so the two disagree for a window.
    The `/api/inventory/cable/run` precedent."""
    raise inventory.InventoryError(
        "Plans have been replaced by metered billing. Reload the page.")


# -------------------------------------------------------- superadmin console

def _org_row(h, org_id: str) -> dict:
    """One org in the shape `billing_org_rows()` yields, so the write reply
    and the fleet table cannot render the same org differently."""
    b = h.store.org_billing(org_id)
    return {"org_id": org_id, "name": h.store.org_name(org_id) or org_id,
            "billing_exempt": b["exempt"], "deactivated": b["deactivated"],
            "conn_rate_paise": b["conn_rate_paise"],
            "floor_paise": b["floor_paise"]}


def _console_row(h, org: dict, today) -> dict:
    org_id = org["org_id"]
    open_inv = h.store.oldest_open_invoice(org_id)
    ladder = metering.ladder_stage(
        open_inv["month"] if open_inv else None, today,
        exempt=bool(org.get("billing_exempt")),
        deactivated=bool(org.get("deactivated")))
    row = h.store.accrual_on(org_id, today.isoformat()) or {}
    return {
        "org_id": org_id,
        "name": org.get("name") or org_id,
        "outstanding_paise": h.store.outstanding_paise(org_id),
        "exempt": bool(org.get("billing_exempt")),
        "deactivated": bool(org.get("deactivated")),
        "conn_rate_paise": org.get("conn_rate_paise"),
        "floor_paise": org.get("floor_paise"),
        "open_invoice": open_inv,
        "stage": ladder["stage"],
        "days_overdue": ladder["days_overdue"],
        "deactivation_candidate": ladder["deactivation_candidate"],
        "today": {
            "day": row.get("day"),
            "paise": row.get("paise"),
            "conn_count": row.get("conn_count"),
            "conn_source": row.get("conn_source"),
            "device_count": row.get("device_count"),
            "winning_side": row.get("winning_side"),
            "flags": row.get("flags") or {},
        } if row else None,
    }


def admin_billing(h, qs):
    """The fleet table, plus one org's full ledger when `org` names one.

    Every number the console's chips filter on rides these rows, so a chip
    recounts what it filters to rather than trusting a separate total (the
    /issues rule)."""
    user = superadmin_or_403(h)
    if not user:
        return
    today = billing_mod.operator_today(h.cfg)
    conn_rate, floor = h.store.global_billing_rates()
    doc = {
        "today": today.isoformat(),
        "month": metering.month_key(today),
        "rates": {"conn_paise": conn_rate, "floor_paise": floor},
        "orgs": [_console_row(h, o, today) for o in h.store.billing_org_rows()],
        "payment": _payment_public(h),
    }
    org = (qs.get("org") or [None])[0]
    if org:
        doc["ledger"] = {
            "org_id": org,
            "accruals": h.store.accruals_since(
                org, metering.month_start(metering.prev_month(
                    metering.month_key(today))).isoformat()),
            "invoices": h.store.org_invoices(org),
            "payments": h.store.org_payments(org),
        }
    h._reply(200, doc)


def _admin_org(h, body) -> str | None:
    org = str(body.get("org_id") or "").strip()
    if not org or not h.store.org_exists(org):
        h._reply(404, {"error": "unknown org"})
        return None
    return org


def admin_billing_write(h, user, body):
    """Superadmin ledger writes: rate overrides, the exempt and deactivate
    flags, and manual payments or adjustments.

    Deactivation is a CLICK and lands here, never in the sweep: a lapsed bill
    must not silence an alarm, and the decision to stand a fleet's probes down
    belongs to a human who typed the org's name into a confirm dialog."""
    if not user["is_superadmin"]:
        h._reply(403, {"error": "forbidden"})
        return
    org = _admin_org(h, body)
    if not org:
        return

    # -- rate overrides: null clears back to the global default -------------
    if "conn_rate_paise" in body or "floor_paise" in body:
        current = h.store.org_billing(org)
        rates = {}
        for key in ("conn_rate_paise", "floor_paise"):
            if key not in body:
                rates[key] = current[key]
                continue
            raw = body.get(key)
            if raw is None or raw == "":
                rates[key] = None
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                h._reply(422, {"error": f"{key} must be a whole number of paise"})
                return
            if value < 0:
                h._reply(422, {"error": f"{key} cannot be negative"})
                return
            rates[key] = value
        h.store.set_org_billing_rates(org, **rates)

    # -- exempt / deactivate ------------------------------------------------
    if "exempt" in body or "deactivated" in body:
        prior = h.store.org_billing(org)
        exempt = bool(body["exempt"]) if "exempt" in body else None
        deactivated = (bool(body["deactivated"])
                       if "deactivated" in body else None)
        # Re-anchor accrual when a flag that was SUPPRESSING it clears: the
        # days an org spent exempt or deactivated are a deliberate hole in the
        # ledger, and the backfill pass must not charge across them as if
        # central had merely been down.
        resume = None
        was_off = prior["exempt"] or prior["deactivated"]
        now_off = (prior["exempt"] if exempt is None else exempt) or \
                  (prior["deactivated"] if deactivated is None else deactivated)
        if was_off and not now_off:
            resume = billing_mod.operator_today(h.cfg).isoformat()
        h.store.set_org_billing_flags(org, exempt=exempt,
                                      deactivated=deactivated,
                                      resume_day=resume)

    # -- manual payment or adjustment ---------------------------------------
    if body.get("payment") is not None:
        pay_body = body.get("payment")
        if not isinstance(pay_body, dict):
            h._reply(422, {"error": "payment must be an object"})
            return
        kind = str(pay_body.get("kind") or "manual").strip()
        if kind not in ("manual", "adjustment"):
            h._reply(422, {"error": "kind must be manual or adjustment"})
            return
        try:
            paise = int(pay_body.get("paise"))
        except (TypeError, ValueError):
            h._reply(422, {"error": "amount must be a whole number of paise"})
            return
        note = str(pay_body.get("note") or "").strip()[:500] or None
        if kind == "adjustment" and not note:
            h._reply(422, {"error": "an adjustment needs a note saying why"})
            return
        try:
            h.store.record_payment(org, paise, kind, note=note,
                                   recorded_by=user["username"])
        except ValueError as exc:
            h._reply(422, {"error": str(exc)})
            return
        h.store.settle_invoices(org)

    # -- void or reopen one invoice -----------------------------------------
    if body.get("invoice") is not None:
        inv = body.get("invoice")
        if not isinstance(inv, dict):
            h._reply(422, {"error": "invoice must be an object"})
            return
        month = str(inv.get("month") or "").strip()
        status = str(inv.get("status") or "").strip()
        if not _MONTH_RE.match(month):
            h._reply(422, {"error": "month must be YYYY-MM"})
            return
        if status not in ("open", "void"):
            h._reply(422, {"error": "an invoice may be set open or void. "
                                    "Paid is derived from the payments."})
            return
        if not h.store.org_invoice(org, month):
            h._reply(404, {"error": "no invoice for that month"})
            return
        h.store.set_invoice_status(org, month, status)
        h.store.settle_invoices(org)

    h._reply(200, {"ok": True, "org": _console_row(
        h, _org_row(h, org), billing_mod.operator_today(h.cfg))})


# ------------------------------------------------------------------ webhook

def webhook(h) -> None:
    """`POST /payments/webhook`: the gateway's own report and the ONLY path
    that records gateway money (its payload carries a SIGNED amount).

    Auth-exempt by construction, signature-verified, replay-safe: the store's
    partial unique index on provider_payment_id makes a re-delivered event a
    no-op rather than a double credit. Answers:

      400 bad or missing signature      (a webhook we cannot verify does not
                                         exist, and a retry after the operator
                                         fixes the secret is the right cure)
      503 payments not configured       (transient: the operator has not
                                         finished setting the gateway up)
      200 everything else               (including replays and non-capture
                                         events, so the gateway stops retrying
                                         something we have already handled)
    """
    raw = h._read_raw()
    provider = payments.get_provider(h.store, h.secretbox, h.cfg)
    if provider is None:
        h._reply(503, {"error": "payments are not configured"})
        return
    try:
        event = provider.verify_webhook(dict(h.headers), raw)
    except Exception:
        log.exception("payment webhook verification failed")
        event = None
    if not event:
        h._reply(400, {"error": "bad signature"})
        return
    if event.get("status") != "captured":
        # An authorization that failed is not money. Recorded nowhere, and
        # answered 200 so the gateway stops re-delivering it.
        h._reply(200, {"ok": True, "recorded": False, "reason": "not captured"})
        return
    org = event.get("org_id")
    payment_id = event.get("payment_id") or ""
    paise = int(event.get("paise") or 0)
    if not org or not payment_id or paise <= 0:
        # Unattributable: the order was created outside this install, or the
        # notes were stripped. Nothing to post and nothing a retry would fix.
        log.warning("payment webhook could not be attributed: org=%r id=%r "
                    "paise=%r", org, payment_id, paise)
        h._reply(200, {"ok": True, "recorded": False,
                       "reason": "no org on the event"})
        return
    if not h.store.org_exists(org):
        log.warning("payment webhook names unknown org %r", org)
        h._reply(200, {"ok": True, "recorded": False, "reason": "unknown org"})
        return
    row_id = h.store.record_payment(
        org, paise, "gateway", provider=provider.name,
        provider_payment_id=payment_id,
        provider_order_id=event.get("order_id"))
    if row_id is None:
        h._reply(200, {"ok": True, "recorded": False, "reason": "replay"})
        return
    h.store.settle_invoices(org)
    log.info("payment recorded for %s: %s paise (%s)", org, paise, payment_id)
    h._reply(200, {"ok": True, "recorded": True})
