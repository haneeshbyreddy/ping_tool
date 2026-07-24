"""Notification egress — the notifiers every central paging shell sends through.

ntfy is the default and AUTHORITATIVE channel: its send result is what a page's
success is judged on and what `alert_log.channel` records. WhatsApp (Meta Cloud
API) is an ADDITIVE, experimental second channel (2026-07-23). A `MultiNotifier`
fans a page out to both, and two rules are load-bearing:

  * **ntfy stays byte-identical.** `NtfyNotifier.send` is unchanged; the new
    `whatsapp` / `facts` keyword args are accepted and IGNORED by it. The
    recipient it POSTs is still the bare ntfy topic string, so every existing
    call site and test keeps working. (The plan's "widen recipient to a value
    object" is realised as a companion `whatsapp=` kwarg instead — same effect,
    zero blast radius on the ntfy path, and the `str` recipient that ~a dozen
    tests assert on is preserved.)
  * **WhatsApp can never break a page.** Its send is fully exception-wrapped and
    it is never the primary channel, so a bad token / timeout / Meta 4xx is
    logged only. `MultiNotifier.send` returns the PRIMARY (ntfy) result. Same
    discipline as "a failed heartbeat is a warning, never a crashed cycle."

WhatsApp is central-only: `build_notifier` builds it only when handed a `store`
(so it can read the superadmin's live config out of `app_settings`), which the
edge never passes — the edge gets a bare `NtfyNotifier`, exactly as before.
"""
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


@dataclass(frozen=True)
class WhatsAppFacts:
    """The four structured facts the approved template renders as
    `🔻 {{1}} — {{2}} ({{3}}) · {{4}}` = subject — status (detail) · timestamp.

    Precise facts come from the ICMP device page (dispatch.py); everything else
    derives reasonable ones from the ntfy title/body via `derive`, so no caller
    is forced to hand-build them."""
    subject: str
    status: str
    detail: str
    timestamp: str

    def params(self) -> list[str]:
        return [_wa_clean(self.subject), _wa_clean(self.status),
                _wa_clean(self.detail), _wa_clean(self.timestamp)]

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


class NtfyNotifier:
    channel = "ntfy"

    def __init__(self, cfg: Config = CONFIG) -> None:
        self.base = cfg.ntfy_base_url.rstrip("/")
        self._retries = max(1, cfg.ntfy_retries)
        self._backoff = cfg.ntfy_retry_backoff_s

    def send(self, recipient: str | None, title: str, body: str, priority: int,
             *, whatsapp: Sequence[str] = (), facts: WhatsAppFacts | None = None
             ) -> NotifyResult:
        # `whatsapp`/`facts` are accepted so the fan-out call convention is one
        # signature; ntfy ignores them (its recipient is the topic string).
        if not recipient:
            return NotifyResult(False, "no ntfy topic")
        try:
            import httpx
        except ImportError as exc:
            return NotifyResult(False, f"httpx missing: {exc}")

        def _attempt() -> _Attempt:
            try:
                resp = httpx.post(
                    self.base,
                    json={
                        "topic": recipient,
                        "title": title,
                        "message": body,
                        "priority": max(1, min(5, priority)),
                    },
                    timeout=10.0,
                )
                if resp.status_code >= 500:
                    return _Attempt(NotifyResult(False, f"HTTP {resp.status_code}"), True)
                resp.raise_for_status()
                return _Attempt(NotifyResult(True), False)
            except httpx.HTTPStatusError as exc:
                return _Attempt(NotifyResult(False, str(exc)), False)
            except Exception as exc:
                return _Attempt(NotifyResult(False, str(exc)), True)

        return send_with_retry(
            _attempt, attempts=self._retries, backoff=self._backoff)


class WhatsAppNotifier:
    """Meta WhatsApp Cloud API, experimental (2026-07-23). Best-effort by
    construction: every failure mode returns a `NotifyResult(False, …)` and
    NOTHING here raises, so a `MultiNotifier` can layer it beside ntfy without a
    bad token ever taking a real page down.

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
        self._retries = max(1, cfg.ntfy_retries)
        self._backoff = cfg.ntfy_retry_backoff_s
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

    def send(self, recipient: str | None, title: str, body: str, priority: int,
             *, whatsapp: Sequence[str] = (), facts: WhatsAppFacts | None = None
             ) -> NotifyResult:
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


class MultiNotifier:
    """Fan one page out to several channels. The FIRST channel is primary: its
    result is what `send` returns and what `channel` (→ `alert_log.channel`)
    reports, so ntfy stays authoritative. Every non-primary channel is
    exception-isolated, so an experimental channel can never downgrade or crash
    a real page."""

    def __init__(self, channels: Sequence) -> None:
        self.channels = list(channels)

    @property
    def channel(self) -> str:
        return self.channels[0].channel if self.channels else "none"

    def send(self, recipient: str | None, title: str, body: str, priority: int,
             *, whatsapp: Sequence[str] = (), facts: WhatsAppFacts | None = None
             ) -> NotifyResult:
        primary: NotifyResult | None = None
        for i, ch in enumerate(self.channels):
            try:
                res = ch.send(recipient, title, body, priority,
                              whatsapp=whatsapp, facts=facts)
            except Exception as exc:
                log.warning("notifier %s raised (ignored): %s",
                            getattr(ch, "channel", "?"), exc)
                res = NotifyResult(False, f"{getattr(ch, 'channel', '?')} raised")
            if i == 0:
                primary = res
        return primary if primary is not None else NotifyResult(False, "no channels")


def build_notifier(cfg: Config = CONFIG, store=None):
    """Assemble the enabled channels. ntfy first (authoritative) when on;
    WhatsApp added when a `store` is present (central — it reads the superadmin's
    live config out of it) or the env flag forces it on. The edge passes no
    store and defaults to WhatsApp-off, so it still gets a bare `NtfyNotifier` —
    byte-identical to before. A single channel is returned unwrapped."""
    channels: list = []
    if cfg.enable_ntfy:
        channels.append(NtfyNotifier(cfg))
    if store is not None or cfg.enable_whatsapp:
        channels.append(WhatsAppNotifier(cfg, store))
    if not channels:
        channels.append(NtfyNotifier(cfg))
    return channels[0] if len(channels) == 1 else MultiNotifier(channels)
