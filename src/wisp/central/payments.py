"""Provider-agnostic payment-gateway seam.

Razorpay is the first adapter; PayU (and any later gateway) lands as another
Provider subclass plus one branch in get_provider. Nothing gateway-specific
may leak outside its adapter class: the API layer sees only the Provider
contract and the normalized webhook event.

Config lives in app_settings (superadmin), secrets encrypted at rest with the
install's SecretBox — the box is passed in, never constructed here:

- payment_provider        gateway name; empty/absent = payments dormant
- payment_key_id          plaintext (it ships to the browser in checkout)
- payment_key_secret_enc  secretbox token
- payment_webhook_secret_enc  secretbox token

Replay safety is the STORE's job (unique index on the provider payment id);
the normalized event just carries payment_id.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request

from wisp.central.secretbox import DecryptError
from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.payments")

PROVIDER_KEY = "payment_provider"
KEY_ID_KEY = "payment_key_id"
KEY_SECRET_KEY = "payment_key_secret_enc"
WEBHOOK_SECRET_KEY = "payment_webhook_secret_enc"

# The gateway names this install can be pointed at. A CLOSED vocabulary, like
# every other recipe list here: an unknown name is refused at the settings
# write rather than discovered as a dormant payment page months later.
# Adding PayU = one adapter class plus one entry here.
PROVIDERS = ("razorpay",)


class PaymentError(Exception):
    """A payment operation failed. str(exc) is a human sentence."""


class Provider:
    """The seam every gateway adapter implements."""

    name: str = ""

    def create_order(self, org_id: str, paise: int, receipt: str) -> dict:
        """Create a gateway order; returns the SPA checkout payload:

        {"provider": self.name, "key_id": str, "order_id": str,
         "amount": int (paise), "currency": "INR"}

        Raises PaymentError with a human sentence on any failure.
        """
        raise NotImplementedError

    def verify_return(self, params: dict) -> bool:
        """True iff the browser-return params carry a valid signature.

        params is a generic dict; each adapter picks its own keys.
        """
        raise NotImplementedError

    def verify_webhook(self, headers: dict, body: bytes) -> dict | None:
        """None = bad signature or unparseable. Else a NORMALIZED event:

        {"org_id": str|None, "payment_id": str, "order_id": str|None,
         "paise": int, "status": "captured"|"failed"|"other"}
        """
        raise NotImplementedError


def _decrypt_setting(box, token: str | None, key: str) -> str | None:
    """Decrypt one app_settings token; a failure degrades to None, never raises."""
    if not token:
        return None
    try:
        return box.decrypt(token)
    except DecryptError:
        log.warning("payment setting %s could not be decrypted; treating it as unset", key)
        return None
    except Exception:
        log.warning("payment setting %s could not be read; treating it as unset",
                    key, exc_info=True)
        return None


def provider_settings(store, box) -> dict:
    """Read the payment config from app_settings, decrypting the secrets.

    Returns {"provider", "key_id", "key_secret", "webhook_secret"}, each
    str | None. A missing setting or an undecryptable token yields None for
    that field only — this never raises.
    """
    provider = (store.get_setting(PROVIDER_KEY) or "").strip() or None
    key_id = (store.get_setting(KEY_ID_KEY) or "").strip() or None
    return {
        "provider": provider,
        "key_id": key_id,
        "key_secret": _decrypt_setting(box, store.get_setting(KEY_SECRET_KEY),
                                       KEY_SECRET_KEY),
        "webhook_secret": _decrypt_setting(box, store.get_setting(WEBHOOK_SECRET_KEY),
                                           WEBHOOK_SECRET_KEY),
    }


_warned_unknown: set[str] = set()


def get_provider(store, box, cfg: Config = CONFIG) -> Provider | None:
    """The configured gateway adapter, or None while payments are dormant.

    Dormant until provider + key_id + key_secret are all present (a missing
    webhook secret only disables webhook verification, not checkout). An
    unknown provider name yields None and logs once per name.
    """
    settings = provider_settings(store, box)
    name = settings["provider"]
    if not (name and settings["key_id"] and settings["key_secret"]):
        return None
    if name == "razorpay":
        return RazorpayProvider(key_id=settings["key_id"],
                                key_secret=settings["key_secret"],
                                webhook_secret=settings["webhook_secret"])
    if name not in _warned_unknown:
        _warned_unknown.add(name)
        log.warning("unknown payment provider %r; payments stay dormant", name)
    return None


class RazorpayProvider(Provider):
    """Razorpay adapter. Everything Razorpay-specific lives in this class."""

    name = "razorpay"

    def __init__(self, key_id: str, key_secret: str,
                 webhook_secret: str | None = None) -> None:
        self.key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret
        # Instance attribute on purpose: tests point it at a local API double.
        self.base_url = "https://api.razorpay.com"
        self.timeout = 15.0

    # -- checkout -----------------------------------------------------------

    def create_order(self, org_id: str, paise: int, receipt: str) -> dict:
        try:
            paise = int(paise)
        except (TypeError, ValueError):
            paise = 0
        if paise <= 0:
            raise PaymentError("Could not create the payment order: the amount "
                               "must be a positive number of paise")
        # Amounts are ALREADY paise end to end — never multiply by 100 here.
        body = json.dumps({"amount": paise, "currency": "INR",
                           "receipt": receipt,
                           "notes": {"org_id": org_id}}).encode("utf-8")
        auth = base64.b64encode(
            f"{self.key_id}:{self._key_secret}".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(
            self.base_url + "/v1/orders", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Basic " + auth})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise PaymentError("Could not create the payment order: "
                               + self._http_detail(exc)) from exc
        except OSError as exc:  # URLError and timeouts both land here
            reason = getattr(exc, "reason", None) or exc
            raise PaymentError("Could not create the payment order: could not "
                               f"reach the gateway ({reason})") from exc
        try:
            order = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PaymentError("Could not create the payment order: the gateway "
                               "reply was not valid JSON") from exc
        order_id = order.get("id") if isinstance(order, dict) else None
        if not order_id:
            raise PaymentError("Could not create the payment order: the gateway "
                               "reply carried no order id")
        return {"provider": self.name, "key_id": self.key_id,
                "order_id": str(order_id), "amount": paise, "currency": "INR"}

    @staticmethod
    def _http_detail(exc: urllib.error.HTTPError) -> str:
        """Razorpay's own error description when it sent one, else the code."""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            desc = str((body.get("error") or {}).get("description") or "").strip()
            if desc:
                return desc
        except Exception:
            pass
        return f"the gateway answered HTTP {exc.code}"

    # -- browser return -----------------------------------------------------

    def verify_return(self, params: dict) -> bool:
        params = params or {}
        order_id = params.get("razorpay_order_id")
        payment_id = params.get("razorpay_payment_id")
        signature = params.get("razorpay_signature")
        if not (order_id and payment_id and signature):
            return False
        expect = hmac.new(self._key_secret.encode("utf-8"),
                          f"{order_id}|{payment_id}".encode("utf-8"),
                          hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, str(signature))

    # -- webhook ------------------------------------------------------------

    def verify_webhook(self, headers: dict, body: bytes) -> dict | None:
        # A webhook we cannot verify does not exist.
        if not self._webhook_secret:
            return None
        signature = None
        for key, value in (headers or {}).items():
            if str(key).lower() == "x-razorpay-signature":
                signature = value
                break
        if not signature:
            return None
        expect = hmac.new(self._webhook_secret.encode("utf-8"), body,
                          hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, str(signature)):
            return None
        try:
            event = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(event, dict):
            return None
        status = {"payment.captured": "captured",
                  "payment.failed": "failed"}.get(event.get("event"), "other")
        payment: dict = {}
        payload = event.get("payload")
        if isinstance(payload, dict):
            wrapper = payload.get("payment")
            if isinstance(wrapper, dict):
                entity = wrapper.get("entity")
                if isinstance(entity, dict):
                    payment = entity
        notes = payment.get("notes")
        org_id = notes.get("org_id") if isinstance(notes, dict) else None
        if org_id is not None:
            org_id = str(org_id) or None
        try:
            paise = int(payment.get("amount") or 0)
        except (TypeError, ValueError):
            paise = 0
        order_id = payment.get("order_id")
        return {"org_id": org_id,
                "payment_id": str(payment.get("id") or ""),
                "order_id": str(order_id) if order_id else None,
                "paise": paise,
                "status": status}
