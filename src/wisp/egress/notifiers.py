"""ntfy push channel — the notification primitive shared by the edge daemon and
central's own dispatcher (`central/dispatch.py`).

The DB-coupled alert POLICY (who gets told, anti-spam dedupe, escalation timers)
used to live here as `AlertDispatcher`, back when the edge ran its own FSM. That
policy now lives on central (`central/dispatch.py`'s `CentralAlertDispatcher`,
over `org_devices`/`escalations` instead of `devices`/`poll_results`) — this
module is just the channel: `NtfyNotifier` sends real push notifications via
ntfy (httpx, lazy import), with a retry policy pure enough to unit-test without
a network call.
"""
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
    """One delivery attempt's outcome plus whether retrying could plausibly help
    (transient network / 5xx = yes; a 4xx config error = no, fail fast)."""
    result: NotifyResult
    retryable: bool


def send_with_retry(
    attempt: Callable[[], _Attempt],
    *,
    attempts: int,
    backoff: float,
    sleep: Callable[[float], None] = time.sleep,
) -> NotifyResult:
    """Call `attempt` up to `attempts` times, backing off exponentially between
    transient failures, so a single push never vanishes to a momentary blip. Stops
    early on success or a non-retryable error. Pure (clock injected) for testing."""
    last = NotifyResult(False, "no attempt made")
    for i in range(1, max(1, attempts) + 1):
        a = attempt()
        if a.result.ok or not a.retryable:
            return a.result
        last = a.result
        if i < attempts:
            sleep(backoff * (2 ** (i - 1)))
    return last


# --- Channels ---------------------------------------------------------------
class NtfyNotifier:
    channel = "ntfy"

    def __init__(self, cfg: Config = CONFIG) -> None:
        self.base = cfg.ntfy_base_url.rstrip("/")
        self._retries = max(1, cfg.ntfy_retries)
        self._backoff = cfg.ntfy_retry_backoff_s

    def send(self, recipient: str, title: str, body: str, priority: int) -> NotifyResult:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            return NotifyResult(False, f"httpx missing: {exc}")

        def _attempt() -> _Attempt:
            try:
                # ntfy's JSON publish endpoint (POST to the server root) so the
                # title/message carry UTF-8 (emoji) — the header form requires ASCII
                # and would reject '✅', '🔴', etc. `recipient` is the ntfy topic.
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
                if resp.status_code >= 500:  # server hiccup — worth retrying
                    return _Attempt(NotifyResult(False, f"HTTP {resp.status_code}"), True)
                resp.raise_for_status()      # 4xx -> raises below, not retried
                return _Attempt(NotifyResult(True), False)
            except httpx.HTTPStatusError as exc:  # 4xx: bad topic/config, won't self-heal
                return _Attempt(NotifyResult(False, str(exc)), False)
            except Exception as exc:  # timeout / connection error: transient
                return _Attempt(NotifyResult(False, str(exc)), True)

        return send_with_retry(
            _attempt, attempts=self._retries, backoff=self._backoff)


def build_notifier(cfg: Config = CONFIG):
    return NtfyNotifier(cfg)
