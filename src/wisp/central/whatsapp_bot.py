from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from wisp.central import onuroster
from wisp.core.state_machine import DOWN_FAMILY

log = logging.getLogger("wisp.central.whatsapp_bot")

_NOT_YOURS = "That OLT isn't in your network."
_MIN_NEEDLE = 3
_MAX_LIST = 8

_GREETINGS = frozenset({
    "HI", "HII", "HIII", "HIHI", "HITHERE", "HELLO", "HELLOW", "HEY", "HEYTHERE",
    "HELP", "MENU", "START", "HOLA", "NAMASTE", "NAMASKARAM", "OK", "OKAY",
    "THANKS", "THANKYOU", "THX", "TEST", "TESTING",
    "GOODMORNING", "GOODAFTERNOON", "GOODEVENING", "GM", "GE",
})
_ASK_MAC, _ASK_NAME = "ask:mac", "ask:name"
_MENU_BUTTONS = ((_ASK_MAC, "Search by MAC"), (_ASK_NAME, "Search by name"))
_FMT_MAC = ("📇 Send the ONU's MAC address.\n\n"
            "Example: a4:f2:1b:00:11:22\n"
            "• separators don't matter, a4f21b001122 works too\n"
            "• a partial MAC is fine (at least 3 characters), e.g. 1b0011")
_FMT_NAME = ("🔖 Send the ONU's name as provisioned on the OLT.\n\n"
             "Example: hc_kiran\n"
             "• a partial name is fine (at least 3 characters), e.g. kiran\n"
             "• case doesn't matter")


class _LoggedNotifier:

    def __init__(self, inner) -> None:
        self._inner = inner

    def send_text(self, to, body):
        return self._logged("text", to, self._inner.send_text(to, body))

    def send_buttons(self, to, body, buttons):
        return self._logged("buttons", to,
                            self._inner.send_buttons(to, body, buttons))

    def send(self, title, body, priority=3, *, whatsapp=(), facts=None):
        return self._logged("template", ",".join(whatsapp),
                            self._inner.send(title, body, priority,
                                             whatsapp=list(whatsapp), facts=facts))

    @staticmethod
    def _logged(kind: str, to: str, res):
        if res is not None and not getattr(res, "ok", True):
            log.warning("whatsapp bot reply (%s) to …%s FAILED: %s",
                        kind, str(to)[-4:], getattr(res, "detail", ""))
        return res


class WhatsAppBot:
    def __init__(self, store, notifier, sweeper=None, *, base_url: str = "") -> None:
        self.store = store
        self.notifier = _LoggedNotifier(notifier)
        self.sweeper = sweeper
        self.base_url = (base_url or "").rstrip("/")
        self._dm: tuple[str, dict] | None = None


    def handle(self, payload: dict) -> None:
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
        else:
            self._menu(sender, user)


    def _menu(self, sender: str, user: dict, prefix: str = "") -> None:
        who = (user.get("username") or "").strip()
        body = f"👋 Hi {who}." if who else "👋 Hi."
        body += " What would you like to look up?"
        if prefix:
            body = f"{prefix}\n\n{body}"
        self.notifier.send_buttons(sender, body, _MENU_BUTTONS)


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
        lines = [f'Matches on {len(matches)} OLTs for "{text[:60]}":']
        for dev, hits in matches[:_MAX_LIST]:
            lines.append(f'• {dev.get("name") or dev.get("id")}: {len(hits)} ONU(s)')
        lines.append("\nReply with a fuller MAC or name to narrow to one OLT.")
        self.notifier.send_text(sender, "\n".join(lines))

    def _search(self, org: str, needle: str):
        now = datetime.now(timezone.utc)
        out: list[tuple[dict, list[dict]]] = []
        for did in self.store.onu_search_device_ids(org, needle):
            dev = self._device(org, did)
            if not dev:
                continue
            roster = onuroster.current_roster(
                self.store.list_onu_optics(org, did), now, stale_s=None)
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
                lines.append(f"• {title} · PON {o.get('pon_port') or '?'}"
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
        if role == "owner":
            btns.append((f"refresh:{did}", "Refresh dBm"))
        btns.append((f"map:{did}" + (f":{onu_id}" if onu_id else ""), "On map"))
        btns.append((f"recent:{did}", "Recent"))
        return btns


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

        if oid is None:
            return
        outcome = self.store.accept_outage(org, oid, user.get("username") or "")
        if outcome == "ok":
            row = self._outage(org, oid)
            device = (row or {}).get("device_name") or "the outage"
            self.notifier.send_text(
                sender, f"✅ Thanks, you're marked as on the way to {device}. "
                        f"The team can see it.")
            self._tell_assigner(org, row, user.get("username") or "")
        elif outcome == "already":
            self.notifier.send_text(sender, "✅ You had already accepted this one.")
        elif outcome == "closed":
            self.notifier.send_text(
                sender, "👍 That outage has already recovered, nothing to go out for.")
        else:
            self.notifier.send_text(
                sender, "🔒 That job isn't assigned to you (it may have been "
                        "reassigned). Nothing was changed.")

    def _tell_assigner(self, org: str, row: dict | None, who: str) -> None:
        by = (row or {}).get("assigned_by")
        if not by:
            return
        device = (row or {}).get("device_name") or "the outage"
        detail = f"{who} accepted the assignment on {device}"
        text = f"✅ {detail}."
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
        if user.get("role") != "owner":
            self.notifier.send_text(sender, "🔒 Refresh dBm is owner-only.")
            return
        dev = self._device(org, did)
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
                sender, f"A read of {dev.get('name')} is already running. "
                        "Try again shortly.")
            return
        target = sweeper.target(org, did)
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
            sender, f"🔄 Reading {dev.get('name')} now. Send the MAC again "
                    "in a minute for fresh dBm.")

    def _map(self, sender: str, org: str, did: int | None, onu: str) -> None:
        dev = self._device(org, did) if did is not None else None
        if not dev:
            self.notifier.send_text(sender, _NOT_YOURS)
            return
        lat, lng = dev.get("lat"), dev.get("lng")
        lines = [f"🗺️ {dev.get('name') or dev.get('id')}"]
        if lat is not None and lng is not None:
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
        lines = [f"Recent outages · {dev.get('name')}:"]
        for r in rows:
            when = _ago(r.get("started_at"), now)
            if r.get("resolved_at"):
                dur = _span(r.get("started_at"), r.get("resolved_at"))
                extra = f" · {r['root_cause']}" if r.get("root_cause") else ""
                lines.append(f"• {when} ago · {r.get('final_state')} · lasted {dur}{extra}")
            else:
                lines.append(f"• {when} ago · {r.get('final_state')} · 🔴 ONGOING")
        self.notifier.send_text(sender, "\n".join(lines))


    def _device(self, org: str, did) -> dict | None:
        if self._dm is None or self._dm[0] != org:
            self._dm = (org, {d["id"]: d for d in self.store.list_org_devices(org)})
        return self._dm[1].get(did)


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
        return f"⚠️ {dev.get('name')} is {dev.get('state')}, so readings are frozen{tail}."
    secs = _age_secs(opt_ts, now)
    if secs is not None and secs > onuroster.STALE_S:
        return f"⏳ Optics last walked {_ago(opt_ts, now)} ago, so readings may be stale."
    return ""
