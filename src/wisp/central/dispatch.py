from __future__ import annotations

from datetime import datetime, timedelta

from wisp.central.notify_policy import AlertRouter
from wisp.config import CONFIG, Config
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
from wisp.egress.notifiers import NotifyResult

_DOWN_PRIORITY = 4
_ROLES = ("owner", "operator", "tech")

def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)

def _plus_minutes(ts: str, minutes: int) -> str:
    return (_parse(ts) + timedelta(minutes=minutes)).isoformat(timespec="seconds")

class CentralAlertDispatcher:
    def __init__(self, store, org_id: str, engine: MonitorEngine, notifier,
                cfg: Config = CONFIG) -> None:
        self.store = store
        self.org_id = org_id
        self.engine = engine
        self.notifier = notifier
        self.cfg = cfg
        self.router = AlertRouter(store, org_id, notifier, cfg)

    def _topic(self, role: str) -> str | None:
        return self.store.org_role_topic(self.org_id, role)

    def _publish(self, role: str, title: str, body: str, priority: int) -> NotifyResult:
        primary = self._topic(role)
        if not primary:
            return NotifyResult(False, f"no {role} channel configured")
        res = self.notifier.send(primary, title, body, priority)
        operator = self._topic("operator")
        if role != "operator" and operator and operator != primary:
            self.notifier.send(operator, title, body, priority)
        return res

    def _broadcast(self, title: str, body: str, priority: int) -> NotifyResult:
        topics = list(dict.fromkeys(t for t in (self._topic(r) for r in _ROLES) if t))
        if not topics:
            return NotifyResult(False, "no channel configured")
        primary = NotifyResult(False, "no channel configured")
        for i, topic in enumerate(topics):
            res = self.notifier.send(topic, title, body, priority)
            if i == 0:
                primary = res
        return primary

    def _log(self, outage_id, device_id, recipient, status, payload, ts,
             kind=None) -> None:
        self.store.log_alert(self.org_id, outage_id, device_id, self.notifier.channel,
                             recipient, status, payload, ts, kind=kind)

    def _record(self, device_id, recipient, status, payload, ts, kind=None) -> None:
        oid = self.store.open_outage_id(self.org_id, device_id)
        self._log(oid, device_id, recipient, status, payload, ts, kind=kind)

    def dispatch(self, events: list[Event], ts: str) -> None:
        for ev in events:
            if isinstance(ev, OutageOpened):
                self._on_open(ev, ts)
            elif isinstance(ev, OutageRecategorized):
                if ev.state == DOWN:
                    self._on_open(OutageOpened(ev.device_id, DOWN), ts)
            elif isinstance(ev, OutageResolved):
                self._on_resolved(ev, ts)
            elif isinstance(ev, UplinkDown):
                self._send_owner("🚨 UPLINK_DOWN", "Local alerts frozen", ts, 5,
                                 payload="UPLINK_DOWN", kind="UPLINK_DOWN")
            elif isinstance(ev, UplinkRestored):
                self._send_owner("✅ Uplink restored", "Monitoring resumed", ts, 3,
                                 payload="UPLINK_RESTORED", kind="UPLINK_RESTORED")

    def _on_open(self, ev: OutageOpened, ts: str) -> None:
        dev = self.engine.meta[ev.device_id]
        if ev.state == UNREACHABLE:
            self._record(ev.device_id, self._topic("operator"), "suppressed",
                         "UNREACHABLE (parent down)", ts, kind="UNREACHABLE")
            return

        oid = self.store.open_outage_id(self.org_id, ev.device_id)
        if oid is None:
            return
        recipient = self._topic("owner")
        if self.store.already_paged(oid):
            self._log(oid, ev.device_id, recipient, "suppressed",
                      "already paged this outage", ts, kind="DEVICE_DOWN")
            return

        title = f"🔴 DOWN: {dev.name} ({dev.region})"
        body = dev.ip_address
        res = self._publish("owner", title, body, _DOWN_PRIORITY)
        self._log(oid, ev.device_id, recipient, "sent" if res.ok else "failed", body,
                  ts, kind="DEVICE_DOWN")
        self.store.schedule_escalation(self.org_id, oid, "hourly",
                                       _plus_minutes(ts, self.cfg.escalate_every_min))

    def _on_resolved(self, ev: OutageResolved, ts: str) -> None:
        dev = self.engine.meta[ev.device_id]
        recipient = self._topic("operator")
        was_suppressed = self.store.last_resolved_state(
            self.org_id, ev.device_id) == UNREACHABLE

        if not was_suppressed:
            self._broadcast(f"✅ Restored: {dev.name} ({dev.region})", "", 3)

        self.store.cancel_pending_escalations(self.org_id, ev.device_id, ts)
        self._log(None, ev.device_id, recipient,
                  "suppressed" if was_suppressed else "sent", "restored", ts,
                  kind="DEVICE_RESTORED")

    def _send_owner(self, title: str, body: str, ts: str, priority: int, *,
                    payload: str | None = None, kind: str | None = None) -> None:
        res = self._publish("owner", title, body, priority)
        logged = payload if payload is not None else title
        self._log(None, None, self._topic("owner"), "sent" if res.ok else "failed",
                  logged, ts, kind=kind)

    def sweep(self, now_ts: str) -> None:
        for row in self.store.due_escalations(self.org_id, now_ts):
            if row["resolved_at"] is not None or row["kind"] != "hourly":
                self.store.mark_escalation_executed(row["id"], now_ts)
                continue
            self._fire_hourly(row, now_ts)
            self.store.reschedule_escalation(
                row["id"], _plus_minutes(now_ts, self.cfg.escalate_every_min))

    @staticmethod
    def _fmt_elapsed(started_at: str, now_ts: str) -> str:
        secs = max(0, int((_parse(now_ts) - _parse(started_at)).total_seconds()))
        h, rem = divmod(secs, 3600)
        m = rem // 60
        if h and m:
            return f"{h}h {m}m"
        return f"{h}h" if h else f"{m}m"

    def _fire_hourly(self, row: dict, ts: str) -> None:
        dev = self.engine.meta.get(row["device_id"])
        if dev is None:
            return
        elapsed = self._fmt_elapsed(row["started_at"], ts)
        ack = (f"acked by {row['acknowledged_by']}"
               if row["acknowledged_by"] else "unacked")
        # The initial DOWN already pushed to the phone; the hourly re-nag folds
        # into the digest (kept off the push tier by operator choice) so a long
        # outage resurfaces once an hour without buzzing all night.
        self.router.emit(
            "HOURLY_ESCALATION", topic=self._topic("operator"),
            title=f"⏰ STILL DOWN ({elapsed}): {dev.name} ({dev.region})",
            body=f"{dev.ip_address} · {ack}", priority=5, ts=ts,
            outage_id=row["outage_id"], device_id=row["device_id"],
            payload=f"hourly escalation ({elapsed})")

    def acknowledge(self, outage_id: int, by: str) -> bool:
        return self.store.acknowledge_outage(self.org_id, outage_id, by)
