"""Layers 4 & 5 — alert dispatch.

Engine events (OutageOpened/Resolved/Uplink…) become messages routed to the
region technician, with a two-step escalation to the owner if nobody acks.

  * Notifier   — the channel interface. NtfyNotifier sends real push
                 notifications via ntfy (httpx, lazy import).
  * AlertDispatcher — the policy: who gets told, anti-spam dedupe, and the
                 DB-derived escalation timers (restart-safe) plus their sweeper.

Network sends happen OUTSIDE any DB transaction; only the alert_log / escalation
bookkeeping touches the database (through the retry helper), so a slow API call
never holds a write lock.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from wisp.config import CONFIG, Config
from wisp.database.client import connect, write_with_retry
from wisp.core.state_machine import (
    DOWN,
    UNREACHABLE,
    Event,
    MonitorEngine,
    OutageOpened,
    OutageRecategorized,
    OutageResolved,
    UplinkDown,
    UplinkRestored,
)


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


def role_topic(role: str, cfg: Config = CONFIG) -> str:
    """The ntfy topic (channel) for a role. People subscribe to the one channel
    that matches their role — owner / operator / tech — so there is no per-person
    routing key. Topics are fixed config (config.py), not derived from the team
    directory."""
    return {
        "owner": cfg.ntfy_topic_owner,
        "operator": cfg.ntfy_topic_operator,
        "tech": cfg.ntfy_topic_tech,
    }.get(role, cfg.ntfy_topic_tech)


# --- Time helpers -----------------------------------------------------------
def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _plus_minutes(ts: str, minutes: int) -> str:
    return (_parse(ts) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


# --- Dispatcher -------------------------------------------------------------
class AlertDispatcher:
    def __init__(self, engine: MonitorEngine, notifier, cfg: Config = CONFIG) -> None:
        self.engine = engine
        self.notifier = notifier
        self.cfg = cfg
        self.topic_tech = cfg.ntfy_topic_tech
        self.topic_owner = cfg.ntfy_topic_owner
        self.topic_operator = cfg.ntfy_topic_operator

    def _publish(self, role: str, title: str, body: str, priority: int) -> NotifyResult:
        """Send to a role's channel, with a copy to the operator channel (operators
        get full visibility into everything). Returns the primary send's result."""
        primary = role_topic(role, self.cfg)
        res = self.notifier.send(primary, title, body, priority)
        if role != "operator" and self.topic_operator and self.topic_operator != primary:
            self.notifier.send(self.topic_operator, title, body, priority)
        return res

    # -- small DB helpers --
    def _open_outage_id(self, conn, device_id: int) -> int | None:
        row = conn.execute(
            "SELECT id FROM outages WHERE device_id = ? AND resolved_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (device_id,),
        ).fetchone()
        return row["id"] if row else None

    def _recently_alerted(self, conn, recipient: str, device_id: int, ts: str) -> bool:
        cutoff = _plus_minutes(ts, -self.cfg.alert_dedupe_min)
        row = conn.execute(
            "SELECT 1 FROM alert_log WHERE recipient = ? AND device_id = ?"
            " AND status = 'sent' AND sent_at >= ? LIMIT 1",
            (recipient, device_id, cutoff),
        ).fetchone()
        return row is not None

    def _log(self, conn, outage_id, device_id, recipient, status, payload, ts) -> None:
        conn.execute(
            "INSERT INTO alert_log (outage_id, device_id, channel, recipient, sent_at,"
            " status, payload) VALUES (?,?,?,?,?,?,?)",
            (outage_id, device_id, self.notifier.channel, recipient, ts, status, payload),
        )

    def _priority(self, criticality: int) -> int:
        return max(1, min(5, criticality))

    # -- public API called by the daemon --
    def dispatch(self, events: list[Event], ts: str) -> None:
        for ev in events:
            if isinstance(ev, OutageOpened):
                self._on_open(ev, ts)
            elif isinstance(ev, OutageRecategorized):
                # promotion from UNREACHABLE -> real DOWN: treat as a fresh open
                if ev.state == DOWN:
                    self._on_open(OutageOpened(ev.device_id, DOWN, ev.inferred_cause), ts)
            elif isinstance(ev, OutageResolved):
                self._on_resolved(ev, ts)
            elif isinstance(ev, UplinkDown):
                self._send_owner("🚨 UPLINK_DOWN", "Our internet is down — local alerts frozen", ts, 5)
            elif isinstance(ev, UplinkRestored):
                self._send_owner("✅ Uplink restored", "Monitoring resumed", ts, 3)

    def _on_open(self, ev: OutageOpened, ts: str) -> None:
        dev = self.engine.meta[ev.device_id]
        if ev.state == UNREACHABLE:
            # topology-suppressed: record the decision, page no one
            self._record(ev.device_id, self.topic_tech, "suppressed",
                         "UNREACHABLE (parent down)", ts)
            return
        recipient = self.topic_tech
        cause = ev.inferred_cause or "unknown"
        title = f"🔴 DOWN — {dev.name} ({dev.region})"
        body = (f"{'⚡POWER' if 'Power' in cause else '🔧LINK'} · crit {dev.criticality}")

        # anti-spam (escalations bypass this)
        def _do():
            with connect(self.cfg) as conn:
                if self._recently_alerted(conn, recipient, ev.device_id, ts):
                    oid = self._open_outage_id(conn, ev.device_id)
                    self._log(conn, oid, ev.device_id, recipient, "suppressed",
                              "dedupe window", ts)
                    conn.commit()
                    return None
                oid = self._open_outage_id(conn, ev.device_id)
                return oid

        oid = write_with_retry(_do)
        if oid is None:
            return  # was suppressed

        res = self._publish("tech", title, body, self._priority(dev.criticality))

        def _after():
            with connect(self.cfg) as conn:
                self._log(conn, oid, ev.device_id, recipient,
                          "sent" if res.ok else "failed", body, ts)
                # schedule the two escalation steps (restart-safe, idempotent)
                for kind, mins in (("realert", self.cfg.realert_after_min),
                                   ("escalate_to_owner", self.cfg.escalate_owner_after_min)):
                    conn.execute(
                        "INSERT OR IGNORE INTO escalations (outage_id, kind, due_at)"
                        " VALUES (?,?,?)",
                        (oid, kind, _plus_minutes(ts, mins)),
                    )
                conn.commit()

        write_with_retry(_after)

    def _on_resolved(self, ev: OutageResolved, ts: str) -> None:
        dev = self.engine.meta[ev.device_id]
        recipient = self.topic_tech

        with connect(self.cfg) as conn:
            row = conn.execute(
                "SELECT final_state FROM outages WHERE device_id = ?"
                " AND resolved_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                (ev.device_id,),
            ).fetchone()
        was_suppressed = row is not None and row["final_state"] == UNREACHABLE

        # Don't announce recovery for an outage we never paged about (UNREACHABLE).
        if not was_suppressed:
            self._publish("tech", f"✅ Restored — {dev.name} ({dev.region})",
                          "Service back up", 3)

        def _do():
            with connect(self.cfg) as conn:
                # cancel any pending escalations for this device's just-closed outage
                conn.execute(
                    "UPDATE escalations SET executed_at = ? WHERE executed_at IS NULL"
                    " AND outage_id IN (SELECT id FROM outages WHERE device_id = ?"
                    " AND resolved_at IS NOT NULL)",
                    (ts, ev.device_id),
                )
                status = "suppressed" if was_suppressed else "sent"
                self._log(conn, None, ev.device_id, recipient, status, "restored", ts)
                conn.commit()

        write_with_retry(_do)

    def _record(self, device_id, recipient, status, payload, ts) -> None:
        def _do():
            with connect(self.cfg) as conn:
                oid = self._open_outage_id(conn, device_id)
                self._log(conn, oid, device_id, recipient, status, payload, ts)
                conn.commit()
        write_with_retry(_do)

    def _send_owner(self, title: str, body: str, ts: str, priority: int) -> None:
        res = self._publish("owner", title, body, priority)

        def _do():
            with connect(self.cfg) as conn:
                self._log(conn, None, None, self.topic_owner,
                          "sent" if res.ok else "failed", title, ts)
                conn.commit()
        write_with_retry(_do)

    # -- escalation sweeper, called once per cycle --
    def sweep(self, now_ts: str) -> None:
        with connect(self.cfg) as conn:
            due = conn.execute(
                "SELECT e.id, e.kind, o.id outage_id, o.device_id, o.acknowledged_at,"
                " o.resolved_at FROM escalations e JOIN outages o ON o.id = e.outage_id"
                " WHERE e.executed_at IS NULL AND e.due_at <= ?",
                (now_ts,),
            ).fetchall()

        for row in due:
            settled = row["resolved_at"] is not None or row["acknowledged_at"] is not None
            if not settled:
                self._fire_escalation(row["kind"], row["device_id"], now_ts)
            # mark executed regardless (settled ones are simply cancelled)
            def _mark(eid=row["id"]):
                with connect(self.cfg) as conn:
                    conn.execute("UPDATE escalations SET executed_at = ? WHERE id = ?",
                                 (now_ts, eid))
                    conn.commit()
            write_with_retry(_mark)

    def _fire_escalation(self, kind: str, device_id: int, ts: str) -> None:
        dev = self.engine.meta[device_id]
        if kind == "realert":
            recipient = self.topic_tech
            self._publish("tech", f"⏰ STILL DOWN — {dev.name}",
                          "No acknowledgement yet", self._priority(dev.criticality))
            payload = "realert"
        else:  # escalate_to_owner
            recipient = self.topic_owner
            self._publish("owner", f"⚠️ ESCALATION — {dev.name} ({dev.region})",
                          "Unacknowledged outage", 5)
            payload = "escalate_to_owner"
        self._record(device_id, recipient, "sent", payload, ts)


def acknowledge_outage(outage_id: int, by: str, cfg: Config = CONFIG) -> bool:
    """Mark an outage acknowledged — this is what stops the escalation ladder.
    In production this is wired to an ack action; here it's also a CLI."""
    def _do():
        with connect(cfg) as conn:
            cur = conn.execute(
                "UPDATE outages SET acknowledged_at = COALESCE(acknowledged_at,"
                " datetime('now')), acknowledged_by = ? WHERE id = ? AND resolved_at IS NULL",
                (by, outage_id),
            )
            conn.commit()
            return cur.rowcount > 0
    return write_with_retry(_do)
