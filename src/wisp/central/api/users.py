"""Dashboard login accounts."""
from __future__ import annotations

import json
import re

from wisp.central import auth, totp
from wisp.central.api.common import reader_or_401

# E.164, optional leading '+', 8–15 digits (separators stripped before matching).
_WA_RE = re.compile(r"^\+?[1-9]\d{7,14}$")

# Shown as the account name's issuer in the authenticator app.
_TOTP_ISSUER = "WISP Central"


def _can_use_totp(user: dict) -> bool:
    # Owner + superadmin only — workers are 403'd off these routes by the
    # whitelist anyway (they're not in _WORKER_POST); this is belt-and-braces
    # and gates on IDENTITY before role, like every other superadmin check.
    return bool(user.get("is_superadmin")) or user.get("role") == "owner"


def list_users(h, qs):
    user = reader_or_401(h)
    if not user:
        return
    org = h._scope_org(user, qs)
    if not user["is_superadmin"] and user["role"] != "owner":
        h._reply(403, {"error": "forbidden"})
        return
    h._reply(200, {"users": h.store.list_users(org_id=org)})


def create(h, user, body):
    org = body.get("org_id") if user["is_superadmin"] else user["org_id"]
    if not (user["is_superadmin"] or user["role"] == "owner"):
        h._reply(403, {"error": "forbidden"})
        return
    uid = auth.create_user(h.store, org, body.get("username", ""),
                           body.get("password", ""), body.get("role", "worker"))
    h._reply(200, {"id": uid})


def deactivate(h, user, body):
    if not (user["is_superadmin"] or user["role"] == "owner"):
        h._reply(403, {"error": "forbidden"})
        return
    target = h.store.get_user(int(body["id"]))
    if target and (user["is_superadmin"] or target["org_id"] == user["org_id"]):
        h.store.set_user_active(int(body["id"]), bool(body.get("active", False)))
        h._reply(200, {"ok": True})
    else:
        h._reply(403, {"error": "forbidden"})


def delete(h, user, body):
    if not (user["is_superadmin"] or user["role"] == "owner"):
        h._reply(403, {"error": "forbidden"})
        return
    target_id = int(body.get("id") or 0)
    if target_id == user["id"]:
        h._reply(422, {"error": "cannot delete your own account"})
        return
    target = h.store.get_user(target_id)
    if target and (user["is_superadmin"] or target["org_id"] == user["org_id"]):
        h.store.delete_user(target_id)
        h._reply(200, {"ok": True})
    else:
        h._reply(403, {"error": "forbidden"})


def password(h, user, body):
    target_id = int(body.get("id") or user["id"])
    if target_id == user["id"]:
        if not auth.verify_login(h.store, user["username"], body.get("current_password", "")):
            h._reply(422, {"error": "current password is incorrect"})
            return
    else:
        if not (user["is_superadmin"] or user["role"] == "owner"):
            h._reply(403, {"error": "forbidden"})
            return
        target = h.store.get_user(target_id)
        if not target or not (user["is_superadmin"] or target["org_id"] == user["org_id"]):
            h._reply(403, {"error": "forbidden"})
            return
    auth.set_password(h.store, target_id, body.get("new_password", ""))
    # A password change ends every OTHER live session for that account — the
    # point of "change it because I might be compromised" is that it locks out a
    # stolen cookie too. Bumping the epoch invalidates them all; if you changed
    # your OWN password, re-issue THIS tab's cookie on the new generation so you
    # stay signed in. A teammate reset just invalidates the teammate's sessions.
    epoch = h.store.bump_session_epoch(target_id)
    cookie = None
    if target_id == user["id"]:
        tok = auth.issue_session(user["id"], h.cfg, remember=False, epoch=epoch)
        cookie = auth.session_cookie(
            tok, max_age=auth.session_cookie_max_age(h.cfg, remember=False),
            secure=h.cfg.session_cookie_secure)
    h._reply(200, {"ok": True}, cookie=cookie)


def whatsapp(h, user, body):
    """Set (or clear) a login account's WhatsApp number — the recipient half of
    the experimental WhatsApp channel. Anyone may set their OWN (self-service,
    like the password route, so a worker can add it from the field app);
    owners/superadmins may set it for any account in their org."""
    target_id = int(body.get("id") or user["id"])
    number = str(body.get("whatsapp_number") or "").strip()
    if number:
        compact = re.sub(r"[\s\-()]", "", number)
        if not _WA_RE.match(compact):
            h._reply(422, {"error": "enter the number in international format, "
                                    "e.g. +919000000000"})
            return
        number = compact
    else:
        number = None  # blank clears it
    if target_id != user["id"]:
        if not (user["is_superadmin"] or user["role"] == "owner"):
            h._reply(403, {"error": "forbidden"})
            return
        target = h.store.get_user(target_id)
        if not target or not (user["is_superadmin"] or target["org_id"] == user["org_id"]):
            h._reply(403, {"error": "forbidden"})
            return
    h.store.set_user_whatsapp(target_id, number)
    h._reply(200, {"ok": True, "whatsapp_number": number})


# --- TOTP two-factor (self-service, owner/superadmin) ------------------------
# Always operates on the CALLER's own account (user["id"]) — you can't enroll
# someone else's phone, and disabling is a downgrade only the account holder
# should do. Enabling/disabling and regenerating codes all re-check the password:
# it stops an attacker at an unlocked, already-signed-in desk from turning 2FA on
# (locking the owner out with their phone) or off (stripping the protection).

def totp_start(h, user, body):
    """Begin enrollment: mint a fresh secret, return it + the otpauth URI for the
    QR. Inert until confirmed — a set-but-unconfirmed secret is never enforced."""
    if not _can_use_totp(user):
        h._reply(403, {"error": "forbidden"})
        return
    secret = totp.new_secret()
    h.store.set_totp_pending(user["id"], h.secretbox.encrypt(secret))
    h._reply(200, {"secret": secret,
                   "otpauth_uri": totp.provisioning_uri(
                       secret, user["username"], _TOTP_ISSUER)})


def totp_confirm(h, user, body):
    """Verify the first code against the pending secret, switch 2FA on, and hand
    back the one-time recovery codes (shown once, stored only as hashes)."""
    if not _can_use_totp(user):
        h._reply(403, {"error": "forbidden"})
        return
    if not auth.verify_login(h.store, user["username"], body.get("password", "")):
        h._reply(422, {"error": "current password is incorrect"})
        return
    target = h.store.get_user(user["id"])
    if not target or not target["totp_secret"]:
        h._reply(422, {"error": "start two-factor setup first"})
        return
    try:
        secret = h.secretbox.decrypt(target["totp_secret"])
    except Exception:
        h._reply(422, {"error": "two-factor setup expired — start again"})
        return
    step = totp.verify(secret, body.get("code", ""))
    if step is None:
        h._reply(422, {"error": "that code didn't match — check your phone's "
                                "clock and try again"})
        return
    codes = totp.new_recovery_codes()
    h.store.activate_totp(user["id"], json.dumps([totp.recovery_hash(c) for c in codes]))
    h.store.claim_totp_step(user["id"], step)   # so this code can't be replayed at login
    h._reply(200, {"ok": True, "recovery_codes": codes})


def totp_disable(h, user, body):
    if not auth.verify_login(h.store, user["username"], body.get("password", "")):
        h._reply(422, {"error": "current password is incorrect"})
        return
    h.store.disable_totp(user["id"])
    h._reply(200, {"ok": True})


def totp_regenerate(h, user, body):
    """Mint a fresh set of recovery codes (the old ones stop working). Needs the
    password AND a live authenticator code — proof of possession, not just a
    hijacked session."""
    if not auth.verify_login(h.store, user["username"], body.get("password", "")):
        h._reply(422, {"error": "current password is incorrect"})
        return
    target = h.store.get_user(user["id"])
    if not target or not target["totp_enabled"] or not target["totp_secret"]:
        h._reply(422, {"error": "two-factor authentication is not enabled"})
        return
    try:
        secret = h.secretbox.decrypt(target["totp_secret"])
    except Exception:
        h._reply(422, {"error": "two-factor secret unreadable"})
        return
    step = totp.verify(secret, body.get("code", ""), after_step=target["totp_last_step"])
    if step is None:
        h._reply(422, {"error": "enter a current authenticator code"})
        return
    h.store.claim_totp_step(user["id"], step)
    codes = totp.new_recovery_codes()
    h.store.set_totp_recovery(user["id"], json.dumps([totp.recovery_hash(c) for c in codes]))
    h._reply(200, {"ok": True, "recovery_codes": codes})
