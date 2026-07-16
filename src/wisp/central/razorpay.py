"""Razorpay checkout glue: order creation + payment-signature verification.

Self-serve payments on top of the paywall (2026-07-16): the org owner hits
"Pay" on the lock screen / billing card, Razorpay Checkout (browser-side
script) collects UPI/card, and central verifies the returned HMAC before
marking months paid — no admin in the loop. The manual GPay flow survives as
the fallback whenever keys aren't configured (``enabled`` is False).

Keys live in ``app_settings`` (``razorpay_key_id``/``razorpay_key_secret``,
superadmin-pasted once — Settings → Payments), not env vars, so pasting or
rotating them never needs a restart; the gateway re-reads them per call.
``key_id`` ships to browsers by design (Checkout needs it); the secret never
leaves central. No razorpay SDK — central stays pure stdlib: the REST call is
one urllib POST, verification is HMAC-SHA256 over ``order_id|payment_id``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.request

API_BASE = "https://api.razorpay.com"


class GatewayError(Exception):
    """Order creation failed (bad keys, Razorpay down, rejected payload)."""


class RazorpayGateway:

    def __init__(self, store) -> None:
        self.store = store

    @property
    def key_id(self) -> str | None:
        return self.store.get_setting("razorpay_key_id")

    def _secret(self) -> str | None:
        return self.store.get_setting("razorpay_key_secret")

    @property
    def enabled(self) -> bool:
        return bool(self.key_id and self._secret())

    def create_order(self, amount_paise: int, receipt: str,
                     notes: dict | None = None) -> dict:
        """One Razorpay order; returns the API's order document (id, amount,
        …). Network call — callers must hold no DB locks (dispatch rule)."""
        return self._post("/v1/orders", {
            "amount": int(amount_paise),
            "currency": "INR",
            "receipt": receipt[:40],  # Razorpay caps receipts at 40 chars
            "notes": notes or {},
        })

    def _post(self, path: str, payload: dict) -> dict:
        creds = base64.b64encode(
            f"{self.key_id}:{self._secret()}".encode()).decode()
        req = urllib.request.Request(
            API_BASE + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Basic {creds}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode())["error"]["description"]
            except Exception:
                pass
            raise GatewayError(detail or f"Razorpay HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GatewayError(f"Razorpay unreachable: {exc}") from exc

    def verify_signature(self, order_id: str, payment_id: str,
                         signature: str) -> bool:
        """Checkout success handshake: Razorpay signs ``order_id|payment_id``
        with the key secret; only a match proves the payment is real."""
        secret = self._secret()
        if not (secret and order_id and payment_id and signature):
            return False
        expected = hmac.new(secret.encode(),
                            f"{order_id}|{payment_id}".encode(),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(signature))
