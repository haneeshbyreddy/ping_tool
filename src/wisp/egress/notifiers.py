from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from wisp.config import CONFIG, Config

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

class NtfyNotifier:
    channel = "ntfy"

    def __init__(self, cfg: Config = CONFIG) -> None:
        self.base = cfg.ntfy_base_url.rstrip("/")
        self._retries = max(1, cfg.ntfy_retries)
        self._backoff = cfg.ntfy_retry_backoff_s

    def send(self, recipient: str, title: str, body: str, priority: int) -> NotifyResult:
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

def build_notifier(cfg: Config = CONFIG):
    return NtfyNotifier(cfg)
