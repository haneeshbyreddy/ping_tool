"""Notification egress — the WhatsApp channel every central paging shell sends
through.

ntfy was REMOVED 2026-07-24; WhatsApp (Meta WhatsApp Cloud API) is now the SOLE
channel. What used to be an additive, best-effort second channel is now primary
and authoritative: its send result is what a page's success is judged on and
what `alert_log.channel` (always `'whatsapp'`) records.

Two properties survive the promotion:

  * **A send can never crash the report cycle.** Every WhatsApp failure mode
    returns a `NotifyResult(False, …)` and NOTHING here raises — a bad token,
    timeout, or Meta 4xx is captured and logged, the same discipline as "a
    failed heartbeat is a warning, never a crashed cycle." The only difference
    from when it was secondary is that the result now drives the logged
    sent/failed status instead of being discarded.
  * **Recipients are numbers, not a topic.** A page's audience is a list of
    E.164 numbers (`whatsapp=`), resolved per org+role from each login account
    (`users.whatsapp_number`); there is no channel-wide topic anymore, so the
    old ntfy `recipient` positional is gone from `send`.

Central-only by construction: `build_notifier` reads the superadmin's live
config out of `app_settings` via the `store` it is handed. The edge passes no
store and never calls `send` (all paging is central's)."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.egress.notifiers")

# Meta rejects a template body parameter that is empty, or that contains a
# newline / tab, or runs past ~1024 chars. Every fact is squeezed through this.
_WA_MAXLEN = 900


@dataclass(frozen=True)
class NotifyResult:
    ok: bool
    detail: str = ""

@dataclass(frozen=True)
class _Attempt:
    result: NotifyResult
    retryable: bool

def send_with_retry(
    attempt: Callable[[], _Attempt],
    *,
    attempts: int,
    backoff: float,
    sleep: Callable[[float], None] = time.sleep,
) -> NotifyResult:
    last = NotifyResult(False, "no attempt made")
    for i in range(1, max(1, attempts) + 1):
        a = attempt()
        if a.result.ok or not a.retryable:
            return a.result
        last = a.result
        if i < attempts:
            sleep(backoff * (2 ** (i - 1)))
    return last


def _wa_clean(value) -> str:
    """One template parameter, made Meta-safe: whitespace (incl. newlines)
    collapsed to single spaces, truncated, and never empty ('—' stands in for a
    blank field so the 4-parameter template always has 4 non-empty params)."""
    s = " ".join(str(value or "").split())
    return s[:_WA_MAXLEN] or "—"


def _free_text(value) -> str:
    """A free-form message body (bot replies), unlike a template param: NEWLINES
    are kept (the reply cards are multi-line), only trailing space trimmed, capped
    at Meta's 4096-char text limit, never empty."""
    s = str(value or "").strip()
    return s[:4096] or "—"


_ZONES: dict = {}


def _display_zone(name: str):
    """Resolve `WISP_DISPLAY_TZ` to a tzinfo, cached, UTC on anything unknown —
    a missing tz database must degrade a timestamp, never break a page."""
    name = (name or "").strip() or "UTC"
    if name not in _ZONES:
        zone = timezone.utc
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(name)
        except Exception:
            log.warning("unknown display timezone %r; page times stay UTC", name)
        _ZONES[name] = zone
    return _ZONES[name]


def _wa_local(value, tz_name: str = "") -> datetime | None:
    """One stored UTC timestamp as an AWARE datetime in the operator's own zone,
    or None when it isn't a timestamp at all.

    THE zone-resolution choke point. `_wa_time` renders it as text for a WhatsApp
    page; the issues export renders it as a real spreadsheet date cell. Two
    renderers, ONE conversion — a second place that decided what "local" means
    is how half an export ends up 5h30m out from the other half.

    Best-effort like everything else in this module: never raises."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        # '+00:00' from the report cycle; space-separated naive from SQLite.
        dt = datetime.fromisoformat(raw.replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_display_zone(tz_name or CONFIG.display_tz))


def _wa_time(value, tz_name: str = "") -> str:
    """Render one stored UTC timestamp in the operator's own zone.

    Central stores UTC everywhere and the dashboard localises in the browser, so
    a WhatsApp page is the ONE place a stored timestamp reaches a human with
    nothing to convert it — and it shipped raw, so every alert's "Time Logged"
    read 5h30m behind the wall clock in India (reported as "some 6 hours").
    Applied HERE, in `params()`, rather than at the ~8 shells that build facts,
    so a new paging shell cannot reintroduce it.

    Best-effort like every other path in this module: a value that isn't an ISO
    timestamp (or a zone the tz database doesn't know) passes through untouched
    rather than raising inside a send."""
    raw = str(value or "").strip()
    local = _wa_local(raw, tz_name)
    if local is None:
        return raw
    hour12 = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return (f"{local:%d %b %Y}, {hour12}:{local:%M} {ampm} "
            f"{local:%Z}").strip()


@dataclass(frozen=True)
class WhatsAppFacts:
    """The four structured facts the approved `wisp_alert1` template renders:
    `{{1}}` Device, `{{2}}` Status, `{{3}}` Detail, `{{4}}` Time Logged —
    i.e. subject / status / detail / timestamp.

    Precise facts come from the ICMP device page (dispatch.py); every other
    caller derives reasonable ones from the page title/body via `derive`, so no
    caller is forced to hand-build them."""
    subject: str
    status: str
    detail: str
    timestamp: str

    def params(self) -> list[str]:
        return [_wa_clean(self.subject), _wa_clean(self.status),
                _wa_clean(self.detail), _wa_clean(_wa_time(self.timestamp))]

    @classmethod
    def derive(cls, title: str, body: str, status: str = "",
               ts: str = "") -> "WhatsAppFacts":
        return cls(subject=title or "—", status=status or "alert",
                   detail=body or "—",
                   timestamp=ts or datetime.now(timezone.utc)
                   .isoformat(timespec="seconds"))


def _wa_numbers(nums: Sequence[str] | None) -> list[str]:
    """Normalise a recipient list to E.164 digits-only (Meta's `to` format:
    country code + number, NO leading '+'), de-duplicated, junk dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for n in nums or ():
        digits = re.sub(r"\D", "", str(n))
        if len(digits) >= 8 and digits not in seen:
            seen.add(digits)
            out.append(digits)
    return out


class WhatsAppNotifier:
    """Meta WhatsApp Cloud API — the sole notification channel (2026-07-24).
    Best-effort by construction: every failure mode returns a
    `NotifyResult(False, …)` and NOTHING here raises, so a send bug can never
    take down the report/sweep cycle that called it.

    The live config (enable toggle, token, phone-number-id, template, language,
    api version) is the superadmin's, read FRESH from `app_settings` on each
    send so a dashboard change applies with no restart; the frozen `Config`
    env-vars are the fallback when the store carries nothing. Numbers are NOT
    config — they ride the `whatsapp` recipient arg, resolved per org+role from
    each login account (`users.whatsapp_number`).

    `post` is injectable (defaults to `httpx.post`) so unit tests can assert the
    exact Graph payload without a network — same pattern as the probers'
    `sock_factory`."""
    channel = "whatsapp"

    def __init__(self, cfg: Config = CONFIG, store=None, *,
                 post: Callable | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self._retries = max(1, cfg.notify_retries)
        self._backoff = cfg.notify_retry_backoff_s
        self._post = post

    def _settings(self) -> dict:
        raw: dict = {}
        if self.store is not None:
            try:
                raw = self.store.whatsapp_settings() or {}
            except Exception:
                log.exception("could not read whatsapp settings; using env config")
                raw = {}
        cfg = self.cfg

        def pick(key: str, fallback: str) -> str:
            v = raw.get(key)
            return v if v not in (None, "") else fallback

        toggle = raw.get("enabled")
        if toggle in (None, ""):
            enabled = cfg.enable_whatsapp
        else:
            enabled = str(toggle).strip().lower() in ("1", "true", "yes", "on")
        return {
            "enabled": enabled,
            "token": pick("token", cfg.whatsapp_token),
            "phone_id": pick("phone_id", cfg.whatsapp_phone_id),
            "template": pick("template", cfg.whatsapp_template),
            "lang": pick("lang", cfg.whatsapp_lang),
            "api_version": pick("api_version", cfg.whatsapp_api_version),
        }

    def _poster(self) -> Callable | None:
        if self._post is not None:
            return self._post
        try:
            import httpx
        except ImportError:
            return None
        return httpx.post

    def send(self, title: str, body: str, priority: int = 3, *,
             whatsapp: Sequence[str] = (), facts: WhatsAppFacts | None = None
             ) -> NotifyResult:
        # `priority` is accepted for call-convention parity — WhatsApp templates
        # carry no priority. The report cycle must never die on a send bug, so
        # everything is wrapped and turned into a NotifyResult.
        try:
            return self._send(whatsapp, title, body, facts)
        except Exception as exc:  # never let a page-time bug escape this channel
            log.exception("whatsapp send raised; ignored")
            return NotifyResult(False, f"whatsapp raised: {exc}")

    def _send(self, whatsapp, title, body, facts) -> NotifyResult:
        s = self._settings()
        if not s["enabled"]:
            return NotifyResult(False, "whatsapp disabled")
        if not (s["token"] and s["phone_id"]):
            return NotifyResult(False, "whatsapp not configured")
        numbers = _wa_numbers(whatsapp)
        if not numbers:
            return NotifyResult(False, "no whatsapp recipients")
        post = self._poster()
        if post is None:
            return NotifyResult(False, "httpx missing")

        f = facts or WhatsAppFacts.derive(title, body)
        params = [{"type": "text", "text": p} for p in f.params()]
        url = (f"https://graph.facebook.com/{s['api_version']}"
               f"/{s['phone_id']}/messages")
        headers = {"Authorization": f"Bearer {s['token']}"}

        sent = 0
        last = ""
        for number in numbers:
            payload = {
                "messaging_product": "whatsapp",
                "to": number,
                "type": "template",
                "template": {
                    "name": s["template"],
                    "language": {"code": s["lang"]},
                    "components": [{"type": "body", "parameters": params}],
                },
            }

            def _attempt() -> _Attempt:
                try:
                    resp = post(url, headers=headers, json=payload, timeout=10.0)
                    code = getattr(resp, "status_code", 0)
                    if code >= 500:
                        return _Attempt(NotifyResult(False, f"HTTP {code}"), True)
                    if code >= 400:
                        return _Attempt(NotifyResult(False, f"HTTP {code}"), False)
                    return _Attempt(NotifyResult(True), False)
                except Exception as exc:
                    return _Attempt(NotifyResult(False, str(exc)), True)

            res = send_with_retry(_attempt, attempts=self._retries,
                                  backoff=self._backoff)
            if res.ok:
                sent += 1
            else:
                last = res.detail
        if sent:
            return NotifyResult(True, f"whatsapp sent to {sent}/{len(numbers)}")
        return NotifyResult(False, last or "whatsapp send failed")

    # ----- inbound-bot free-form replies (central/whatsapp_bot.py) -------------
    # These reply to someone who JUST messaged us, so the 24h customer-service
    # window is open and a free-form (non-template) message is allowed — unlike
    # `send`, which pages cold and must use the approved wisp_alert1 template.
    # Same best-effort discipline: never raise, always a NotifyResult.

    def send_text(self, to: str, body: str) -> NotifyResult:
        """Plain-text reply to ONE number (the sender)."""
        try:
            return self._send_free(
                to, {"type": "text",
                     "text": {"preview_url": False, "body": _free_text(body)}})
        except Exception as exc:
            log.exception("whatsapp send_text raised; ignored")
            return NotifyResult(False, f"whatsapp raised: {exc}")

    def send_buttons(self, to: str, body: str,
                     buttons: Sequence[tuple[str, str]]) -> NotifyResult:
        """Interactive reply-button message: body + up to 3 quick-reply buttons,
        each `(id, title)`. Meta caps buttons at 3, the id at 256 chars (it comes
        back verbatim as the `button_reply` payload the dispatcher classifies) and
        the visible title at 20."""
        try:
            btns = [{"type": "reply",
                     "reply": {"id": str(bid)[:256], "title": (str(title) or "?")[:20]}}
                    for bid, title in list(buttons)[:3]]
            if not btns:
                return self.send_text(to, body)
            return self._send_free(to, {
                "type": "interactive",
                "interactive": {"type": "button",
                                "body": {"text": _free_text(body)},
                                "action": {"buttons": btns}}})
        except Exception as exc:
            log.exception("whatsapp send_buttons raised; ignored")
            return NotifyResult(False, f"whatsapp raised: {exc}")

    def _send_free(self, to: str, message: dict) -> NotifyResult:
        # Shared transport for the free-form replies: same config/gating as `_send`
        # but posts an arbitrary message object to ONE recipient rather than the
        # template to a list.
        s = self._settings()
        if not s["enabled"]:
            return NotifyResult(False, "whatsapp disabled")
        if not (s["token"] and s["phone_id"]):
            return NotifyResult(False, "whatsapp not configured")
        numbers = _wa_numbers([to])
        if not numbers:
            return NotifyResult(False, "no whatsapp recipient")
        post = self._poster()
        if post is None:
            return NotifyResult(False, "httpx missing")
        url = (f"https://graph.facebook.com/{s['api_version']}"
               f"/{s['phone_id']}/messages")
        headers = {"Authorization": f"Bearer {s['token']}"}
        payload = {"messaging_product": "whatsapp", "to": numbers[0], **message}

        def _attempt() -> _Attempt:
            try:
                resp = post(url, headers=headers, json=payload, timeout=10.0)
                code = getattr(resp, "status_code", 0)
                if code >= 500:
                    return _Attempt(NotifyResult(False, f"HTTP {code}"), True)
                if code >= 400:
                    return _Attempt(NotifyResult(False, f"HTTP {code}"), False)
                return _Attempt(NotifyResult(True), False)
            except Exception as exc:
                return _Attempt(NotifyResult(False, str(exc)), True)

        return send_with_retry(_attempt, attempts=self._retries,
                               backoff=self._backoff)


def build_notifier(cfg: Config = CONFIG, store=None) -> WhatsAppNotifier:
    """WhatsApp is the only channel now. Central passes a `store` so the
    notifier can read the superadmin's live config out of `app_settings`; the
    edge passes none and never calls `send` (all paging is central's), so a
    store-less notifier is inert by construction — it just has nowhere to read
    a token/numbers from."""
    return WhatsAppNotifier(cfg, store)
