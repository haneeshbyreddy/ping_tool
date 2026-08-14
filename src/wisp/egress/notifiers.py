from __future__ import annotations

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from wisp.config import CONFIG, Config

log = logging.getLogger("wisp.egress.notifiers")

_WA_MAXLEN = 900


class SendPool:
    def __init__(self, workers: int = 3, capacity: int = 256) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=capacity)
        self._workers = workers
        self._started = False
        self._lock = threading.Lock()

    def _ensure_started(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        for i in range(self._workers):
            threading.Thread(target=self._loop, name=f"wa-send-{i}",
                             daemon=True).start()

    def _loop(self) -> None:
        while True:
            job = self._q.get()
            try:
                job()
            except Exception:
                log.exception("queued send failed")

    def submit(self, job: Callable[[], None]) -> bool:
        self._ensure_started()
        try:
            self._q.put_nowait(job)
            return True
        except queue.Full:
            return False


_SEND_POOL = SendPool()


def queue_send(notifier, title: str, body: str, priority: int = 3, *,
               whatsapp: Sequence[str] = (), facts=None,
               on_result: Callable | None = None) -> "NotifyResult":
    fn = getattr(notifier, "send_queued", None)
    if fn is not None:
        return fn(title, body, priority, whatsapp=whatsapp, facts=facts,
                  on_result=on_result)
    res = notifier.send(title, body, priority, whatsapp=whatsapp, facts=facts)
    if on_result is not None:
        try:
            on_result(res)
        except Exception:
            log.exception("send completion callback failed")
    return res


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
    s = " ".join(str(value or "").split())
    return s[:_WA_MAXLEN] or "—"


def _free_text(value) -> str:
    s = str(value or "").strip()
    return s[:4096] or "—"


_ZONES: dict = {}


def _display_zone(name: str):
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


    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_display_zone(tz_name or CONFIG.display_tz))


def _wa_time(value, tz_name: str = "") -> str:


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
    out: list[str] = []
    seen: set[str] = set()
    for n in nums or ():
        digits = re.sub(r"\D", "", str(n))
        if len(digits) >= 8 and digits not in seen:
            seen.add(digits)
            out.append(digits)
    return out


class WhatsAppNotifier:


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
        try:
            return self._send(whatsapp, title, body, facts)
        except Exception as exc:
            log.exception("whatsapp send raised; ignored")
            return NotifyResult(False, f"whatsapp raised: {exc}")

    def send_queued(self, title: str, body: str, priority: int = 3, *,
                    whatsapp: Sequence[str] = (),
                    facts: WhatsAppFacts | None = None,
                    on_result: Callable | None = None) -> NotifyResult:
        recipients = list(whatsapp)

        def job() -> None:
            res = self.send(title, body, priority, whatsapp=recipients,
                            facts=facts)
            if on_result is not None:
                try:
                    on_result(res)
                except Exception:
                    log.exception("send completion callback failed")

        if _SEND_POOL.submit(job):
            return NotifyResult(True, "queued")
        res = self.send(title, body, priority, whatsapp=recipients, facts=facts)
        if on_result is not None:
            try:
                on_result(res)
            except Exception:
                log.exception("send completion callback failed")
        return res

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


    def send_text(self, to: str, body: str) -> NotifyResult:
        try:
            return self._send_free(
                to, {"type": "text",
                     "text": {"preview_url": False, "body": _free_text(body)}})
        except Exception as exc:
            log.exception("whatsapp send_text raised; ignored")
            return NotifyResult(False, f"whatsapp raised: {exc}")

    def send_buttons(self, to: str, body: str,
                     buttons: Sequence[tuple[str, str]]) -> NotifyResult:
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
    return WhatsAppNotifier(cfg, store)
