"""Inbound WhatsApp lookup bot (v1).

The OUTBOUND half (``egress/notifiers.py`` WhatsAppNotifier, template
``wisp_alert1``) pages the fleet. This is the INBOUND half: an owner or worker
messages the business number with a MAC or ONU name and gets a reply card back,
with reply-buttons for follow-ups ([Refresh dBm] [On map] [Recent]).

**Every dead end offers the menu.** A greeting, a 1–2 char needle, a miss, a
photo, an unknown button id — all reply with the same tappable
[Search by MAC] [Search by name] card rather than a bare instruction line, and
those two buttons only PRINT the format. There is deliberately no per-sender
"waiting for a MAC" state: one search covers both (`search_key` over serial AND
name), so nothing can desync from a conversation that went quiet for a day.

**Central-only, org-scoped by construction.** The sender's number resolves to
exactly ONE ``(user, org, role)`` via ``store.whatsapp_user``; EVERY lookup is
scoped to THAT org. An unknown/ambiguous/org-less number is IGNORED — no reply,
no org scoped — the same lateral-move caution ``onu-search`` takes. Owner-only
actions ([Refresh dBm] spends the OLT's stored web login) gate on the resolved
role, mirroring ``can_triage`` / ``_WORKER_ROUTES``.

**Honesty.** A NULL ``rx_dbm`` never renders as ``0`` — it says "no dBm reported
for this OLT/vendor" (when the OLT reports none at all) or "no reading" (when it
reports Rx for other ONUs but not this one). A down/stale OLT prints a "readings
are frozen" note with the last-walk age. Same rules the SPA rx-diagnosis follows.

**Reuse, not reimplementation.** Lookup rides ``store.onu_search_device_ids`` +
``onuroster.current_roster`` — byte-for-byte the ``/api/inventory/onu-search``
path. Refresh rides the SAME ``WebOpticsSweeper.target()/scrape_device`` the
dashboard rx-refresh button uses (owner-only, per-OLT lock, recorded outcome).

Deferred (noted, NOT built): the "/" command menu (Conversational Automation),
Lists, Flows, buttoned alert templates.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from wisp.central import onuroster
from wisp.core.state_machine import DOWN_FAMILY

log = logging.getLogger("wisp.central.whatsapp_bot")

_NOT_YOURS = "That OLT isn't in your network."
_MIN_NEEDLE = 3       # matches ONU_SEARCH_MIN — a 1–2 char needle is noise
_MAX_LIST = 8         # cap the lines in a multi-ONU / multi-OLT reply

# A greeting is not a needle. Matched on `search_key` (alphanumeric, upper), so
# "Hi there!" and "hi_there" both land here rather than being searched for and
# reported missing — a miss reads as a fault when the user only said hello.
_GREETINGS = frozenset({
    "HI", "HII", "HIII", "HIHI", "HITHERE", "HELLO", "HELLOW", "HEY", "HEYTHERE",
    "HELP", "MENU", "START", "HOLA", "NAMASTE", "NAMASKARAM", "OK", "OKAY",
    "THANKS", "THANKYOU", "THX", "TEST", "TESTING",
    "GOODMORNING", "GOODAFTERNOON", "GOODEVENING", "GM", "GE",
})
# Reply-button ids. `ask:*` only prints the format — the lookup itself is
# format-agnostic (one search covers MAC and name), so there is deliberately NO
# per-sender "waiting for a MAC" state to keep in sync with a 24h window.
_ASK_MAC, _ASK_NAME = "ask:mac", "ask:name"
_MENU_BUTTONS = ((_ASK_MAC, "Search by MAC"), (_ASK_NAME, "Search by name"))
_FMT_MAC = ("📇 Send the ONU's MAC address.\n\n"
            "Example: a4:f2:1b:00:11:22\n"
            "• separators don't matter — a4f21b001122 works too\n"
            "• a partial MAC is fine (at least 3 characters), e.g. 1b0011")
_FMT_NAME = ("🔖 Send the ONU's name as provisioned on the OLT.\n\n"
             "Example: hc_kiran\n"
             "• a partial name is fine (at least 3 characters), e.g. kiran\n"
             "• case doesn't matter")


class _LoggedNotifier:
    """Wraps the notifier so a REFUSED reply is visible in the log.

    Every send here is best-effort by construction (`NotifyResult(False, …)`,
    never an exception), and the happy path logs nothing — so a reply Meta
    rejects (expired 24h window, a number outside the allowed list on an
    unpublished app, a bad token) presented EXACTLY like a message that was
    never delivered to us at all. One wrapper rather than ~16 call sites so a
    reply added later can't forget to check."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def send_text(self, to, body):
        return self._logged("text", to, self._inner.send_text(to, body))

    def send_buttons(self, to, body, buttons):
        return self._logged("buttons", to,
                            self._inner.send_buttons(to, body, buttons))

    def send(self, title, body, priority=3, *, whatsapp=(), facts=None):
        """The cold-page template. Not a bot REPLY — the one thing the bot sends
        to somebody who didn't just message us (telling an owner their assignment
        was accepted), where the 24h window belongs to the wrong person."""
        return self._logged("template", ",".join(whatsapp),
                            self._inner.send(title, body, priority,
                                             whatsapp=list(whatsapp), facts=facts))

    @staticmethod
    def _logged(kind: str, to: str, res):
        # A double may return None; only a real NotifyResult carries `ok`.
        if res is not None and not getattr(res, "ok", True):
            log.warning("whatsapp bot reply (%s) to …%s FAILED: %s",
                        kind, str(to)[-4:], getattr(res, "detail", ""))
        return res


class WhatsAppBot:
    """One-shot handler for an inbound webhook payload. Construct per delivery
    (cheap) so per-batch caches (`_dm`) don't outlive their org."""

    def __init__(self, store, notifier, sweeper=None, *, base_url: str = "") -> None:
        self.store = store
        self.notifier = _LoggedNotifier(notifier)
        self.sweeper = sweeper
        self.base_url = (base_url or "").rstrip("/")
        self._dm: tuple[str, dict] | None = None   # (org, {id: device_row})

    # -- entry ----------------------------------------------------------------

    def handle(self, payload: dict) -> None:
        """Never raises — this runs on a background thread off the acked webhook;
        a bug here must not take the thread (or the next message) down."""
        try:
            for msg, sender in self._messages(payload):
                try:
                    self._dispatch(sender, msg)
                except Exception:
                    log.exception("whatsapp bot: message handling failed")
        except Exception:
            log.exception("whatsapp bot: payload walk failed")

    @staticmethod
    def _messages(payload: dict):
        """Yield (message, from_number) for every inbound message, skipping the
        delivery/read STATUS callbacks (which carry `statuses`, not `messages`)."""
        for entry in (payload.get("entry") or []):
            for change in (entry.get("changes") or []):
                value = change.get("value") or {}
                for m in (value.get("messages") or []):
                    frm = m.get("from")
                    if frm:
                        yield m, frm

    def _dispatch(self, sender: str, msg: dict) -> None:
        user = self.store.whatsapp_user(sender)
        if not user:
            # Unknown/ambiguous/org-less number — ignore by design (no reply).
            log.info("whatsapp inbound from unrecognised number; ignored")
            return
        org = user["org_id"]
        mtype = msg.get("type")
        if mtype == "text":
            self._handle_text(sender, user, org, (msg.get("text") or {}).get("body") or "")
        elif mtype == "interactive":
            inter = msg.get("interactive") or {}
            if inter.get("type") == "button_reply":
                bid = (inter.get("button_reply") or {}).get("id") or ""
                self._handle_button(sender, user, org, bid)
            else:
                self._menu(sender, user)
        else:  # image / audio / location / … — not part of v1
            self._menu(sender, user)

    # -- the menu -------------------------------------------------------------

    def _menu(self, sender: str, user: dict, prefix: str = "") -> None:
        """The greeting/fallback reply: what the bot can do, as tappable options.
        Sent for a greeting, a too-short needle, a miss, an unhandled message
        type and an unknown button id — every dead end offers the way forward."""
        who = (user.get("username") or "").strip()
        body = f"👋 Hi {who}." if who else "👋 Hi."
        body += " What would you like to look up?"
        if prefix:
            body = f"{prefix}\n\n{body}"
        self.notifier.send_buttons(sender, body, _MENU_BUTTONS)

    # -- text lookup ----------------------------------------------------------

    def _handle_text(self, sender: str, user: dict, org: str, text: str) -> None:
        text = (text or "").strip()
        needle = onuroster.search_key(text)
        if needle in _GREETINGS or len(needle) < _MIN_NEEDLE:
            self._menu(sender, user)
            return
        matches, now = self._search(org, needle)
        if not matches:
            self._menu(sender, user, f'No ONU found matching "{text[:60]}".')
            return
        if len(matches) == 1:
            dev, hits = matches[0]
            onu_id = hits[0].get("id") if len(hits) == 1 else None
            self.notifier.send_buttons(sender, self._card(dev, hits, now),
                                       self._buttons(dev, onu_id, user.get("role")))
            return
        # Several OLTs matched — ambiguous which one a button would act on, so no
        # buttons; list the OLTs and ask for a fuller identifier.
        lines = [f'Matches on {len(matches)} OLTs for "{text[:60]}":']
        for dev, hits in matches[:_MAX_LIST]:
            lines.append(f'• {dev.get("name") or dev.get("id")}: {len(hits)} ONU(s)')
        lines.append("\nReply with a fuller MAC or name to narrow to one OLT.")
        self.notifier.send_text(sender, "\n".join(lines))

    def _search(self, org: str, needle: str):
        """Mirror /api/inventory/onu-search: org-scoped, CURRENT roster (freshest
        walk, stale OLTs kept — same as the drill-down), merged rx_dbm. Returns
        [(device_row, [onu, …]), …] sorted by OLT name, plus `now`."""
        now = datetime.now(timezone.utc)
        out: list[tuple[dict, list[dict]]] = []
        for did in self.store.onu_search_device_ids(org, needle):
            dev = self._device(org, did)
            if not dev:
                continue
            roster = onuroster.current_roster(
                self.store.list_onu_optics(org, did), now, stale_s=None)
            # the OPERATOR's name is searchable beside the walked one — after a
            # field survey it is the name the tech texting us actually knows
            hits = [o for o in roster
                    if needle in onuroster.search_key(o.get("serial"))
                    or needle in onuroster.search_key(o.get("name"))
                    or needle in onuroster.search_key(o.get("label"))]
            if not hits:
                continue
            hits.sort(key=lambda o: (str(o.get("pon_port") or ""),
                                     o.get("onu_id") or 0, str(o.get("onu_key") or "")))
            out.append((dev, hits))
        out.sort(key=lambda m: (m[0].get("name") or "").lower())
        return out, now

    def _card(self, dev: dict, hits: list[dict], now: datetime) -> str:
        name = dev.get("name") or f"device {dev.get('id')}"
        down = dev.get("state") in DOWN_FAMILY
        olt_has_rx = (dev.get("onus_rx") or 0) > 0
        lines: list[str] = []
        if len(hits) == 1:
            o = hits[0]
            title = onuroster.display_name(o) or "ONU"
            lines.append(f"📡 {title}")
            lines.append(f"OLT: {name} · PON {o.get('pon_port') or '?'}"
                         f" · {_state_label(o.get('state'))}")
            lines.append(f"Rx: {_rx(o, olt_has_rx)}")
            lines.append(f"Distance: {_distance(o.get('distance_m'))}")
            lines.append(f"Last online: {_last_online(o, now)}")
        else:
            lines.append(f"Found {len(hits)} ONUs on {name}:")
            for o in hits[:_MAX_LIST]:
                title = onuroster.display_name(o) or "ONU"
                lines.append(f"• {title} — PON {o.get('pon_port') or '?'}"
                             f" · {_state_label(o.get('state'))} · Rx {_rx(o, olt_has_rx)}")
            if len(hits) > _MAX_LIST:
                lines.append(f"…and {len(hits) - _MAX_LIST} more")
        note = _freshness_note(dev, now, down)
        if note:
            lines.append("")
            lines.append(note)
        return "\n".join(lines)

    @staticmethod
    def _buttons(dev: dict, onu_id, role: str | None):
        did = dev.get("id")
        btns: list[tuple[str, str]] = []
        if role == "owner":      # spends the OLT's web login — owner-only
            btns.append((f"refresh:{did}", "Refresh dBm"))
        btns.append((f"map:{did}" + (f":{onu_id}" if onu_id else ""), "On map"))
        btns.append((f"recent:{did}", "Recent"))
        return btns

    # -- button follow-ups ----------------------------------------------------

    def _handle_button(self, sender: str, user: dict, org: str, payload: str) -> None:
        action, _, rest = payload.partition(":")
        if payload == _ASK_MAC:
            self.notifier.send_text(sender, _FMT_MAC)
        elif payload == _ASK_NAME:
            self.notifier.send_text(sender, _FMT_NAME)
        elif action == "accept" or action == "acc":
            self._accept(sender, user, org, _int(rest))
        elif action == "refresh":
            self._refresh(sender, user, org, _int(rest))
        elif action == "map":
            did, _, onu = rest.partition(":")
            self._map(sender, org, _int(did), onu)
        elif action == "recent":
            self._recent(sender, org, _int(rest))
        else:
            self._menu(sender, user)

    def _accept(self, sender: str, user: dict, org: str, oid: int | None) -> None:
        """[✅ I'm on it] on an assignment page — the same accept the dashboard
        button performs, on the same store method and the same rule (only a named
        assignee may accept). A worker at a pole answers from the notification
        instead of finding a laptop, which is the whole reason the page carries a
        button; the dashboard then shows them as accepted like any other yes.

        The reply is always a plain sentence, never a dead end: every outcome
        (accepted / already / not yours / already resolved) is a fact the tapper
        needs, and silence would read as a button that did nothing."""
        if oid is None:
            return
        outcome = self.store.accept_outage(org, oid, user.get("username") or "")
        # `org` came from the sender's own account, so a cross-org outage id is
        # simply "missing" here — never another org's device name.
        if outcome == "ok":
            row = self._outage(org, oid)
            device = (row or {}).get("device_name") or "the outage"
            self.notifier.send_text(
                sender, f"✅ Thanks — you're marked as on the way to {device}. "
                        f"The team can see it.")
            self._tell_assigner(org, row, user.get("username") or "")
        elif outcome == "already":
            self.notifier.send_text(sender, "✅ You had already accepted this one.")
        elif outcome == "closed":
            self.notifier.send_text(
                sender, "👍 That outage has already recovered — nothing to go out for.")
        else:
            self.notifier.send_text(
                sender, "🔒 That job isn't assigned to you (it may have been "
                        "reassigned). Nothing was changed.")

    def _tell_assigner(self, org: str, row: dict | None, who: str) -> None:
        """Tell whoever assigned it that the answer came in — the same courtesy
        the dashboard accept sends, since the two buttons must not differ in
        anything but where they were pressed."""
        by = (row or {}).get("assigned_by")
        if not by:
            return
        device = (row or {}).get("device_name") or "the outage"
        detail = f"{who} accepted the assignment on {device}"
        text = f"✅ {detail}."
        # The assigner did NOT just message us, so their 24h window is usually
        # shut — free-form first (it's the nicer message), template for the rest.
        cold = [n for n in self.store.named_whatsapp(org, [by])
                if not getattr(self.notifier.send_text(n, text), "ok", True)]
        if cold:
            from wisp.egress.notifiers import WhatsAppFacts
            self.notifier.send(
                f"✅ Accepted: {device}", text, 3, whatsapp=cold,
                facts=WhatsAppFacts(subject=device, status="ACCEPTED",
                                    detail=detail,
                                    timestamp=(row or {}).get("accepted_at") or ""))

    def _outage(self, org: str, oid: int) -> dict | None:
        return next((o for o in self.store.triage_outages(org) if o["id"] == oid),
                    None)

    def _refresh(self, sender: str, user: dict, org: str, did: int | None) -> None:
        if did is None:
            return
        # Owner-gated: same grade as opening a proxy session (it spends the stored
        # web-UI credential down the tunnel), which a worker never has.
        if user.get("role") != "owner":
            self.notifier.send_text(sender, "🔒 Refresh dBm is owner-only.")
            return
        dev = self._device(org, did)   # org-scoped: a cross-org id returns None
        if not dev:
            self.notifier.send_text(sender, _NOT_YOURS)
            return
        sweeper = self.sweeper
        if sweeper is None:
            self.notifier.send_text(
                sender, "Web-UI optical reads aren't enabled on this server.")
            return
        if sweeper.busy(did):
            self.notifier.send_text(
                sender, f"A read of {dev.get('name')} is already running — "
                        "try again shortly.")
            return
        target = sweeper.target(org, did)   # the SAME eligibility the button/route use
        if target is None:
            self.notifier.send_text(
                sender, f"{dev.get('name')} isn't set up for web-UI optical reads.")
            return

        def _run() -> None:
            try:
                sweeper.scrape_device(target)
            except Exception:
                log.exception("whatsapp rx-refresh failed device=%s", did)

        threading.Thread(target=_run, name=f"wisp-wa-rxrefresh-{did}",
                         daemon=True).start()
        log.info("whatsapp rx-refresh queued by user=%s for %s/device=%s",
                 user["id"], org, did)
        self.notifier.send_text(
            sender, f"🔄 Reading {dev.get('name')} now — send the MAC again "
                    "in a minute for fresh dBm.")

    def _map(self, sender: str, org: str, did: int | None, onu: str) -> None:
        dev = self._device(org, did) if did is not None else None
        if not dev:
            self.notifier.send_text(sender, _NOT_YOURS)
            return
        lat, lng = dev.get("lat"), dev.get("lng")
        lines = [f"🗺️ {dev.get('name') or dev.get('id')}"]
        if lat is not None and lng is not None:
            # A tappable pin — the useful thing for a tech standing in the field.
            lines.append(f"https://www.google.com/maps?q={lat},{lng}")
        else:
            lines.append("Not placed on the map yet.")
        if self.base_url:
            lines.append(f"Dashboard map: {self.base_url}/app/#/map")
        self.notifier.send_text(sender, "\n".join(lines))

    def _recent(self, sender: str, org: str, did: int | None) -> None:
        dev = self._device(org, did) if did is not None else None
        if not dev:
            self.notifier.send_text(sender, _NOT_YOURS)
            return
        now = datetime.now(timezone.utc)
        rows = self.store.recent_device_outages(org, did, 5)
        if not rows:
            self.notifier.send_text(sender, f"🟢 No recent outages for {dev.get('name')}.")
            return
        lines = [f"Recent outages — {dev.get('name')}:"]
        for r in rows:
            when = _ago(r.get("started_at"), now)
            if r.get("resolved_at"):
                dur = _span(r.get("started_at"), r.get("resolved_at"))
                extra = f" · {r['root_cause']}" if r.get("root_cause") else ""
                lines.append(f"• {when} ago · {r.get('final_state')} · lasted {dur}{extra}")
            else:
                lines.append(f"• {when} ago · {r.get('final_state')} · 🔴 ONGOING")
        self.notifier.send_text(sender, "\n".join(lines))

    # -- per-batch device cache (one list_org_devices call, org-scoped) --------

    def _device(self, org: str, did) -> dict | None:
        if self._dm is None or self._dm[0] != org:
            self._dm = (org, {d["id"]: d for d in self.store.list_org_devices(org)})
        return self._dm[1].get(did)


# --- formatting helpers ------------------------------------------------------

def _int(s) -> int | None:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def _state_label(state: str | None) -> str:
    return {"UP": "🟢 UP", "DEGRADED": "🟡 DEGRADED", "DOWN": "🔴 DOWN",
            "UNREACHABLE": "🔴 UNREACHABLE", "online": "🟢 online",
            "offline": "🔴 offline"}.get(state or "", state or "unknown")


def _rx(o: dict, olt_has_rx: bool) -> str:
    """Never a bare 0 for a missing reading — the whole point of the honesty
    rules. A real figure carries its severity marker; a NULL says WHY."""
    rx = o.get("rx_dbm")
    if rx is not None:
        mark = {"crit": " ⛔", "warn": " ⚠️"}.get(o.get("severity"), "")
        return f"{rx:.2f} dBm{mark}"
    if not olt_has_rx:
        return "no dBm reported for this OLT/vendor"
    return "no reading"


def _distance(m) -> str:
    try:
        v = float(m)
    except (TypeError, ValueError):
        return "unranged"
    return f"{v / 1000:.2f} km" if v > 0 else "unranged"


def _naive(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _age_secs(ts, now: datetime) -> float | None:
    dt = _naive(ts)
    if dt is None:
        return None
    return (now.replace(tzinfo=None) - dt).total_seconds()


def _fmt_secs(secs: float) -> str:
    secs = max(0, int(secs))
    if secs < 90:
        return f"{secs}s"
    mins = secs // 60
    if mins < 90:
        return f"{mins}m"
    hrs = mins // 60
    if hrs < 48:
        return f"{hrs}h"
    return f"{hrs // 24}d"


def _ago(ts, now: datetime) -> str:
    secs = _age_secs(ts, now)
    return _fmt_secs(secs) if secs is not None else "?"


def _span(a, b) -> str:
    da, db = _naive(a), _naive(b)
    if da is None or db is None:
        return "?"
    return _fmt_secs((db - da).total_seconds())


def _last_online(o: dict, now: datetime) -> str:
    if o.get("state") == "online":
        return "now (online)"
    ago = _ago(o.get("last_online_at"), now)
    return f"{ago} ago" if ago != "?" else "unknown"


def _freshness_note(dev: dict, now: datetime, down: bool) -> str:
    opt_ts = dev.get("optics_updated_at")
    if down:
        age = _ago(opt_ts, now)
        tail = f" (last optics {age} ago)" if age != "?" else ""
        return f"⚠️ {dev.get('name')} is {dev.get('state')} — readings are frozen{tail}."
    secs = _age_secs(opt_ts, now)
    if secs is not None and secs > onuroster.STALE_S:
        return f"⏳ Optics last walked {_ago(opt_ts, now)} ago — readings may be stale."
    return ""
