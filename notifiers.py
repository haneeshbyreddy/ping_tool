"""Layers 4 & 5 — alert dispatch.

Engine events (OutageOpened/Resolved/Uplink…) become messages routed to the
region technician, with a two-step escalation to the owner if nobody acks.

  * Notifier   — the channel interface. MockNotifier (default, no deps, records
                 sends for tests) + Ntfy/Telegram senders (httpx, lazy import).
  * AlertDispatcher — the policy: who gets told, anti-spam dedupe, and the
                 DB-derived escalation timers (restart-safe) plus their sweeper.

Network sends happen OUTSIDE any DB transaction; only the alert_log / escalation
bookkeeping touches the database (through the retry helper), so a slow API call
never holds a write lock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from config import CONFIG, Config
from db import connect, write_with_retry
from state_machine import (
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


# --- Channels ---------------------------------------------------------------
class MockNotifier:
    """Records every send and (optionally) prints it. The default channel for
    the no-hardware dev build and the one tests assert against."""

    channel = "mock"

    def __init__(self, *, quiet: bool = False) -> None:
        self.sent: list[dict] = []
        self.quiet = quiet

    def send(self, recipient: str, title: str, body: str, priority: int) -> NotifyResult:
        self.sent.append(
            {"recipient": recipient, "title": title, "body": body, "priority": priority}
        )
        if not self.quiet:
            print(f"      →[mock:{recipient} p{priority}] {title} — {body}")
        return NotifyResult(True)


class NtfyNotifier:
    channel = "ntfy"

    def __init__(self, cfg: Config = CONFIG) -> None:
        self.base = cfg.ntfy_base_url.rstrip("/")

    def send(self, recipient: str, title: str, body: str, priority: int) -> NotifyResult:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            return NotifyResult(False, f"httpx missing: {exc}")
        try:
            # ntfy: recipient is the topic; priority 1..5 maps directly.
            resp = httpx.post(
                f"{self.base}/{recipient}",
                content=body.encode("utf-8"),
                headers={"Title": title, "Priority": str(max(1, min(5, priority)))},
                timeout=10.0,
            )
            resp.raise_for_status()
            return NotifyResult(True)
        except Exception as exc:  # network/HTTP errors must not crash the loop
            return NotifyResult(False, str(exc))


class TelegramNotifier:
    channel = "telegram"

    def __init__(self, cfg: Config = CONFIG) -> None:
        self.token = cfg.telegram_bot_token

    def send(self, recipient: str, title: str, body: str, priority: int) -> NotifyResult:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            return NotifyResult(False, f"httpx missing: {exc}")
        if not self.token:
            return NotifyResult(False, "no telegram token configured")
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": recipient, "text": f"*{title}*\n{body}",
                      "parse_mode": "Markdown"},
                timeout=10.0,
            )
            resp.raise_for_status()
            return NotifyResult(True)
        except Exception as exc:
            return NotifyResult(False, str(exc))


def build_notifier(cfg: Config = CONFIG):
    if cfg.notifier == "ntfy":
        return NtfyNotifier(cfg)
    if cfg.notifier == "telegram":
        return TelegramNotifier(cfg)
    return MockNotifier()


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
        self.owner = cfg.owner_telegram_chat_id or "OWNER"

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
            self._record(ev.device_id, dev.technician_phone or "tech", "suppressed",
                         "UNREACHABLE (parent down)", ts)
            return
        recipient = dev.technician_phone or "tech"
        cause = ev.inferred_cause or "unknown"
        title = f"🔴 DOWN — {dev.name} ({dev.region})"
        body = (f"{'⚡POWER' if 'Power' in cause else '🔧LINK'} · ~{dev.customer_count} "
                f"customers · ₹{dev.base_revenue_impact:.0f}/hr · crit {dev.criticality}")

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

        res = self.notifier.send(recipient, title, body, self._priority(dev.criticality))

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
        recipient = dev.technician_phone or "tech"

        with connect(self.cfg) as conn:
            row = conn.execute(
                "SELECT final_state FROM outages WHERE device_id = ?"
                " AND resolved_at IS NOT NULL ORDER BY id DESC LIMIT 1",
                (ev.device_id,),
            ).fetchone()
        was_suppressed = row is not None and row["final_state"] == UNREACHABLE

        # Don't announce recovery for an outage we never paged about (UNREACHABLE).
        if not was_suppressed:
            self.notifier.send(recipient, f"✅ Restored — {dev.name} ({dev.region})",
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
        res = self.notifier.send(self.owner, title, body, priority)

        def _do():
            with connect(self.cfg) as conn:
                self._log(conn, None, None, self.owner,
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
            recipient = dev.technician_phone or "tech"
            self.notifier.send(recipient, f"⏰ STILL DOWN — {dev.name}",
                               "No acknowledgement yet", self._priority(dev.criticality))
            payload = "realert"
        else:  # escalate_to_owner
            recipient = self.owner
            self.notifier.send(recipient, f"⚠️ ESCALATION — {dev.name} ({dev.region})",
                               f"Unacknowledged outage, ~{dev.customer_count} customers", 5)
            payload = "escalate_to_owner"
        self._record(device_id, recipient, "sent", payload, ts)


def acknowledge_outage(outage_id: int, by: str, cfg: Config = CONFIG) -> bool:
    """Mark an outage acknowledged — this is what stops the escalation ladder.
    In production this is wired to a Telegram /ack button; here it's also a CLI."""
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
