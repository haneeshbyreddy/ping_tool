"""Phase B — central alerting: the same policy as `egress/notifiers.AlertDispatcher`,
ported to run against `CentralStore`'s tenant-scoped tables and an org's three role
topics instead of the edge's single-tenant `Config.ntfy_topic_*`.

Deliberately NOT a subclass or a shared base with the edge's `AlertDispatcher` — the two
have different DB layers (`wisp.database.client.connect`+`write_with_retry` vs
`CentralStore`'s own connection/lock) and different topic sources (fixed `Config` fields
vs per-org DB rows), so sharing code would mean threading DB-shape differences through
every method. Same escalation ladder, same dedupe rule, same wording — copy once, keep
both readable, per the engine's own "keep the byte-identical path simple" preference over
a forced abstraction.

Scope (Phase B v1): the core outage ladder (open/resolve/recategorize, hourly all-hands
escalation, acknowledge) plus uplink/canary handling (dormant until an edge actually
reports a canary result — see central/engine.py). The soft-signal tiers (`perf_sweep`,
`redundancy_sweep`, SNMP folding) are NOT ported yet — they need trailing sample history
central doesn't store in Phase B v1 (`poll_results`-equivalent). Deferred, not forgotten.

Network sends happen OUTSIDE any DB write, same discipline as the edge: a slow ntfy call
never holds `CentralStore._write_lock`.
"""
from __future__ import annotations

from datetime import datetime, timedelta

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
    def __init__(self, store, tenant_id: str, engine: MonitorEngine, notifier,
                cfg: Config = CONFIG) -> None:
        self.store = store
        self.tenant_id = tenant_id
        self.engine = engine
        self.notifier = notifier
        self.cfg = cfg

    def _topic(self, role: str) -> str | None:
        return self.store.org_role_topic(self.tenant_id, role)

    def _publish(self, role: str, title: str, body: str, priority: int) -> NotifyResult:
        """Send to a role's channel, with a copy to the operator channel (operators get
        full visibility). A role with no topic configured yet is a soft no-op — the badge
        state (outage/alert_log rows) is still written by the caller regardless."""
        primary = self._topic(role)
        if not primary:
            return NotifyResult(False, f"no {role} channel configured")
        res = self.notifier.send(primary, title, body, priority)
        operator = self._topic("operator")
        if role != "operator" and operator and operator != primary:
            self.notifier.send(operator, title, body, priority)
        return res

    def _broadcast(self, title: str, body: str, priority: int) -> NotifyResult:
        """Send to all three role channels once each — the recurring all-hands
        escalation. Blank/duplicate topics are de-duped."""
        topics = list(dict.fromkeys(t for t in (self._topic(r) for r in _ROLES) if t))
        if not topics:
            return NotifyResult(False, "no channel configured")
        primary = NotifyResult(False, "no channel configured")
        for i, topic in enumerate(topics):
            res = self.notifier.send(topic, title, body, priority)
            if i == 0:
                primary = res
        return primary

    def _log(self, outage_id, device_id, recipient, status, payload, ts) -> None:
        self.store.log_alert(self.tenant_id, outage_id, device_id, self.notifier.channel,
                             recipient, status, payload, ts)

    def _record(self, device_id, recipient, status, payload, ts) -> None:
        oid = self.store.open_outage_id(self.tenant_id, device_id)
        self._log(oid, device_id, recipient, status, payload, ts)

    # -- public API: called once per report, after engine.py's process_report --
    def dispatch(self, events: list[Event], ts: str) -> None:
        for ev in events:
            if isinstance(ev, OutageOpened):
                self._on_open(ev, ts)
            elif isinstance(ev, OutageRecategorized):
                if ev.state == DOWN:   # promotion UNREACHABLE -> real DOWN: treat as fresh
                    self._on_open(OutageOpened(ev.device_id, DOWN), ts)
            elif isinstance(ev, OutageResolved):
                self._on_resolved(ev, ts)
            elif isinstance(ev, UplinkDown):
                self._send_owner("🚨 UPLINK_DOWN", "Our internet is down — local alerts "
                                 "frozen", ts, 5, payload="UPLINK_DOWN")
            elif isinstance(ev, UplinkRestored):
                self._send_owner("✅ Uplink restored", "Monitoring resumed", ts, 3,
                                 payload="UPLINK_RESTORED")

    def _on_open(self, ev: OutageOpened, ts: str) -> None:
        dev = self.engine.meta[ev.device_id]
        if ev.state == UNREACHABLE:
            self._record(ev.device_id, self._topic("operator"), "suppressed",
                         "UNREACHABLE (parent down)", ts)
            return

        oid = self.store.open_outage_id(self.tenant_id, ev.device_id)
        if oid is None:
            return   # apply_events already ran; shouldn't happen, but don't page a ghost
        recipient = self._topic("owner")
        if self.store.already_paged(oid):
            self._log(oid, ev.device_id, recipient, "suppressed",
                      "already paged this outage", ts)
            return

        title = f"🔴 DOWN — {dev.name} ({dev.region})"
        body = f"No ping response from {dev.ip_address}"
        res = self._publish("owner", title, body, _DOWN_PRIORITY)
        self._log(oid, ev.device_id, recipient, "sent" if res.ok else "failed", body, ts)
        self.store.schedule_escalation(self.tenant_id, oid, "hourly",
                                       _plus_minutes(ts, self.cfg.escalate_every_min))

    def _on_resolved(self, ev: OutageResolved, ts: str) -> None:
        dev = self.engine.meta[ev.device_id]
        recipient = self._topic("operator")
        was_suppressed = self.store.last_resolved_state(
            self.tenant_id, ev.device_id) == UNREACHABLE

        if not was_suppressed:
            self._broadcast(f"✅ Restored — {dev.name} ({dev.region})",
                            "Service back up", 3)

        self.store.cancel_pending_escalations(self.tenant_id, ev.device_id, ts)
        self._log(None, ev.device_id, recipient,
                  "suppressed" if was_suppressed else "sent", "restored", ts)

    def _send_owner(self, title: str, body: str, ts: str, priority: int, *,
                    payload: str | None = None) -> None:
        res = self._publish("owner", title, body, priority)
        logged = payload if payload is not None else title
        self._log(None, None, self._topic("owner"), "sent" if res.ok else "failed",
                  logged, ts)

    # -- escalation sweeper: called once per report, for THIS tenant's due rows --
    def sweep(self, now_ts: str) -> None:
        for row in self.store.due_escalations(self.tenant_id, now_ts):
            if row["resolved_at"] is not None or row["kind"] != "hourly":
                self.store.mark_escalation_executed(row["id"], now_ts)
                continue
            self._fire_hourly(row, now_ts)
            # reschedule from *now*, not the old due_at, so a quiet stretch of reports
            # doesn't burst-catch-up escalations once they resume.
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
        ack = (f"Acknowledged by {row['acknowledged_by']}."
               if row["acknowledged_by"] else "Not yet acknowledged.")
        self._broadcast(
            f"⏰ STILL DOWN ({elapsed}) — {dev.name} ({dev.region})",
            f"{dev.name} ({dev.ip_address}) has been down for {elapsed}.\n{ack}", 5)
        self._record(row["device_id"], self._topic("owner"), "sent",
                     f"hourly escalation ({elapsed})", ts)

    # -- acknowledge (stops the escalation ladder; recovery is what cancels it) --
    def acknowledge(self, outage_id: int, by: str) -> bool:
        return self.store.acknowledge_outage(self.tenant_id, outage_id, by)
