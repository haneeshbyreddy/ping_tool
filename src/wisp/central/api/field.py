"""Worker location tracking: shifts, the owner's live view, tracker credentials.

The INGEST is not here — `/field/track` is a public, machine-credentialed route
handled in `server.py` beside `/whatsapp/webhook`, before the session gate, the
same category as `/report` and `/edge/snmp-walk`. These are the cookie-authed
dashboard calls that surround it.
"""
from __future__ import annotations

from wisp.central import field, inventory
from wisp.central.api.common import DENIED, body_org_write, org_or_400, reader_or_401


def _self_org(h, user):
    """The caller's OWN org, for the routes that act on the caller.

    `org_id` and `user_id` come from the SESSION and are never read off the body:
    a shift is a statement about who is working, and a body-supplied identity
    would let anyone make it about somebody else. A superadmin has no org and so
    no shift of its own — that is a 400, not a silent no-op.
    """
    if not user.get("org_id"):
        h._reply(400, {"error": "a shift belongs to an org account"})
        return None
    return user["org_id"]


def shift(h, qs):
    """GET — the caller's own shift state.

    Worker-readable (`_WORKER_GET`): the Start/End button has to know which one
    it is before it is pressed, and a button that guesses would be a worker
    ending a shift they never started."""
    user = reader_or_401(h)
    if not user:
        return
    org = _self_org(h, user)
    if not org:
        return
    last = h.store.last_shift(org, user["id"])
    h._reply(200, {
        "on_shift": bool(last and not last["ended_at"]),
        "started_at": last["started_at"] if last else None,
        "ended_at": last["ended_at"] if last else None,
        # Whether a tracker credential exists at all. Without it "on shift" is a
        # declaration nothing can corroborate, and the worker should be told that
        # rather than left to wonder why the owner can't see them.
        "has_token": any(
            r["user_id"] == user["id"] and r["issued_at"] and not r["revoked_at"]
            for r in h.store.list_field_tokens(org)),
    })


def shift_write(h, user, body):
    """POST {action: start|end} — the caller's own shift. Idempotent both ways.

    On `_WORKER_POST`: this is the one thing the tracking feature asks a worker to
    do, and it is a statement about themselves. It writes no location, names no
    device, and cannot touch another account."""
    org = _self_org(h, user)
    if not org:
        return
    action = str(body.get("action") or "").strip().lower()
    if action not in ("start", "end"):
        raise inventory.InventoryError("action must be 'start' or 'end'")
    if action == "start":
        row = h.store.start_shift(org, user["id"])
        h._reply(200, {"ok": True, "on_shift": True,
                       "started_at": row["started_at"],
                       "already": bool(row.get("already"))})
        return
    ended = h.store.end_shift(org, user["id"])
    h._reply(200, {"ok": True, "on_shift": False, "already": not ended})


def workers(h, qs):
    """GET — every account's live position, today's trail and shift state.

    Owner-only by omission from `_WORKER_GET`: where the crew is, is the owner's
    view of the org, not something a worker needs of their colleagues.

    Ships FACTS and one threshold, never a verdict. The four states the map must
    tell apart — here now / on shift but gone quiet / went home / never reported
    — are classified in the SPA (`map/workers.ts`), because freshness ticks with
    the clock and a state stamped at response time would go on claiming "here
    now" for as long as the tab stayed open.
    """
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    since = field.trail_since(h.cfg)
    rows = h.store.worker_tracking(org, trail_since=since)
    for r in rows:
        r["on_shift"] = bool(r["shift_started_at"] and not r["shift_ended_at"])
    h._reply(200, {
        "workers": rows,
        "trail_since": since,
        "fresh_s": h.cfg.field_track_fresh_s,
        "retention_days": h.cfg.field_track_retention_days,
    })


def _track_url(h) -> str:
    """The tracker's server URL, built from the Host that served this request.

    Same trick the WhatsApp bot's [On map] link uses: no separate public-URL
    setting to drift out of step with whatever domain the dashboard is actually
    being read on. It is the ONE string that is identical for every worker, which
    is the whole reason the token rides Traccar's `id` field instead of the path.
    """
    host = (h.headers.get("Host") or "").strip()
    if not host:
        return "/field/track"
    # Central sits behind Caddy and never sees the TLS itself, so the scheme has
    # to come from the flag that already asserts it — the same one that decides
    # whether to send HSTS and a Secure cookie. Guessing https unconditionally
    # would hand a dev install a URL its own tracker could not reach.
    scheme = "https" if h.cfg.session_cookie_secure else "http"
    return f"{scheme}://{host}/field/track"


def tokens(h, qs):
    """GET — the org's accounts and their tracker-credential state.

    Owner-only (it enumerates accounts, like `/api/users`). Carries `issued_at`
    and nothing resembling a token: the plaintext is shown once at issue and is
    not recoverable afterwards, the same contract node tokens keep.
    """
    user = reader_or_401(h)
    if not user:
        return
    org = org_or_400(h, user, qs)
    if not org:
        return
    if not h._can_write(user, org):
        h._reply(403, {"error": "forbidden"})
        return
    h._reply(200, {
        "accounts": h.store.list_field_tokens(org),
        "server_url": _track_url(h),
        "retention_days": h.cfg.field_track_retention_days,
    })


def token_issue(h, user, body):
    """POST {user_id} — mint or ROTATE a worker's tracker token.

    The plaintext comes back exactly once. Rotating is the only way to replace a
    lost one, and it un-revokes — an owner reaching for this after a handset went
    missing wants the new string to work, not to find the row still switched off.
    """
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org:
        h._reply(400, {"error": "org required"})
        return
    target = _target_user(h, org, body)
    if target is None:
        return
    token = h.store.issue_field_token(org, target, created_by=user["id"])
    h._reply(200, {"ok": True, "user_id": target, "token": token,
                   "server_url": _track_url(h)})


def token_revoke(h, user, body):
    """POST {user_id} — switch a worker's tracker credential off.

    The row survives revoked rather than being deleted, so the panel can still
    say the account HAD one — an absence and a withdrawal are different facts
    about a phone that stopped reporting.
    """
    org = body_org_write(h, user, body)
    if org is DENIED:
        return
    if not org:
        h._reply(400, {"error": "org required"})
        return
    target = _target_user(h, org, body)
    if target is None:
        return
    ok = h.store.revoke_field_token(org, target)
    h._reply(200, {"ok": ok})


def _target_user(h, org: str, body: dict) -> int | None:
    """The account a credential write is about, re-resolved against THIS org.

    The body's id is never trusted as a scope: like every `org_devices` write, the
    org comes from the DB row, so an owner cannot mint a credential inside
    somebody else's org by naming one of their user ids.
    """
    try:
        uid = int(body.get("user_id"))
    except (TypeError, ValueError):
        raise inventory.InventoryError("user_id is required")
    if not any(r["user_id"] == uid for r in h.store.list_field_tokens(org)):
        h._reply(404, {"error": "no such active account in this org"})
        return None
    return uid
