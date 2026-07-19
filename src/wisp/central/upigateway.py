"""UPIGateway checkout glue: dynamic-QR order creation + server-side status
verification.

The individual-friendly gateway (2026-07-16, after Razorpay rejected the
unregistered-business account): UPIGateway (upigateway.com) generates a
dynamic UPI QR against the platform admin's personal UPI — the payer scans
from any UPI app and the money lands directly in the admin's bank, 0% fees.

The crucial difference from Razorpay: there is NO signed browser handshake.
The redirect back from the payment page (and their optional webhook) carry
nothing verifiable, so the ONLY settlement truth is central calling
``check_order_status`` with the secret API key. Every settle path funnels
through :func:`attempt_settle`, which trusts nothing but that call.

Three settle triggers cover every way a payment can land:
- the SPA polls ``POST /api/billing/verify`` while the payment tab is open;
- ``GET /api/billing/upi-return`` (their redirect target) settles on the way
  back to the dashboard;
- the billing sweeper re-checks pending orders every sweep, so a payer who
  closed every tab still gets marked paid within the half hour. No inbound
  webhook — central only ever dials out (same taste as the edge).

The API key lives in ``app_settings`` (``upigateway_key``, superadmin-pasted
— Settings → Payments), re-read per call so pasting or rotating it never
needs a restart. It is a SECRET (it can create orders and read statuses) — it
never ships to browsers;
the SPA only ever sees ``upi_enabled`` and per-order ``payment_url``s.
Pure stdlib: two urllib POSTs, no SDK.
"""
from __future__ import annotations

import json
import logging
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger("wisp.central.upigateway")

API_BASE = "https://api.ekqr.in"
# UPIGateway's servers live in IST: check_order_status keys on the IST
# calendar date the order was created, so the date math must too.
IST = timezone(timedelta(hours=5, minutes=30))

# check_order_status verdicts that end an order's life
_SUCCESS = "success"
_FAILURE = "failure"


class GatewayError(Exception):
    """Order/status call failed (bad key, UPIGateway down, rejected payload)."""


def new_txn_id() -> str:
    """Client transaction id — doubles as billing_payments.order_id."""
    return f"wisp{secrets.token_hex(8)}"


def ist_txn_date(created_at_iso: str) -> str:
    """The dd-mm-yyyy (IST) date check_order_status wants, derived from the
    UTC ISO ``created_at`` we stored — deterministic, no extra column."""
    dt = datetime.fromisoformat(created_at_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d-%m-%Y")


class UpiGateway:

    def __init__(self, store) -> None:
        self.store = store

    def _key(self) -> str | None:
        return self.store.get_setting("upigateway_key")

    @property
    def enabled(self) -> bool:
        return bool(self._key())

    def create_order(self, client_txn_id: str, amount_inr: int, info: str,
                     customer_name: str, redirect_url: str) -> dict:
        """One dynamic-QR order; returns their ``data`` doc (``payment_url``,
        ``order_id``, …). Network call — callers must hold no DB locks
        (dispatch rule). Customer email/mobile are required by their API but
        we don't store payer contacts — placeholders are deliberate."""
        data = self._post("/api/create_order", {
            "key": self._key(),
            "client_txn_id": client_txn_id,
            "amount": str(int(amount_inr)),
            "p_info": info[:60],
            "customer_name": (customer_name or "WISP org")[:60],
            "customer_email": "billing@wisp.invalid",
            "customer_mobile": "9999999999",
            "redirect_url": redirect_url,
        })
        if not data.get("payment_url"):
            raise GatewayError("UPIGateway returned no payment_url")
        return data

    def order_status(self, client_txn_id: str, txn_date: str) -> dict:
        """The settlement truth: status is one of created/scanning/success/
        failure, plus ``upi_txn_id`` once paid. Network call — no DB locks."""
        return self._post("/api/check_order_status", {
            "key": self._key(),
            "client_txn_id": client_txn_id,
            "txn_date": txn_date,
        })

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            API_BASE + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                doc = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise GatewayError(f"UPIGateway HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise GatewayError(f"UPIGateway unreachable: {exc}") from exc
        # their envelope: {"status": bool, "msg": ..., "data": {...}}
        if not doc.get("status"):
            raise GatewayError(str(doc.get("msg") or "UPIGateway refused"))
        data = doc.get("data")
        if not isinstance(data, dict):
            raise GatewayError("UPIGateway reply carried no data")
        return data


def attempt_settle(store, gateway: UpiGateway, pay: dict,
                   on_paid=None) -> tuple[str, bool]:
    """Check one pending order against UPIGateway and settle it exactly once.

    Returns ``(status, settled_now)`` where status is 'success' | 'failure' |
    'pending'. Shared by the verify poll, the return redirect and the sweeper
    — settlement stays idempotent because only the settle_billing_payment
    winner (its WHERE status='created' guard) applies plan/months. The status
    call is network — run before any store write, never inside a lock."""
    if pay["status"] == "paid":
        return _SUCCESS, False
    if pay["status"] != "created":
        return _FAILURE, False
    st = gateway.order_status(pay["order_id"], ist_txn_date(pay["created_at"]))
    verdict = str(st.get("status") or "").lower()
    if verdict == _SUCCESS:
        upi_txn = str(st.get("upi_txn_id") or st.get("id") or "paid")
        if store.settle_billing_payment(pay["order_id"], upi_txn):
            org = pay["org_id"]
            if store.org_plan(org) != pay["plan"]:
                store.set_org_plan(org, pay["plan"])
            for m in pay["months"]:
                store.set_billing_month(org, m, True,
                                        marked_by=f"upigateway:{upi_txn}")
            if on_paid:
                on_paid(pay, upi_txn)
            return _SUCCESS, True
        return _SUCCESS, False
    if verdict == _FAILURE:
        store.fail_billing_payment(pay["order_id"])
        return _FAILURE, False
    return "pending", False
