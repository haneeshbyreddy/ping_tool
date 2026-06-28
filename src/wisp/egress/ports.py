"""SNMP port detection + alerting (graph topology Part B).

The SNMP sibling of `AlertDispatcher`: it takes the `PortStatus` list a poll of one
switch produced, persists each port (discovery + live status) into `switch_ports`, and
decides — with the same flap-suppression discipline the ICMP path uses — when a
**monitored** uplink/infra port has confirmably gone down.

Two product rules from the plan are baked in here:

  * **Admin-down is silent.** A port whose ifAdminStatus is down was shut on purpose;
    only oper=down while admin=up is the alarm condition (`PortStatus.is_down`).
  * **A monitored port-down folds into the device it feeds — it is NOT a competing
    alarm** (decision #3). If the port has a `feeds_device_id` and that device already
    has an open outage, the port-down *enriches* that outage (stamps the physical cause)
    instead of opening its own. ICMP stays the outage owner; SNMP confirms/enriches.

Like the perf/redundancy tiers this is a soft signal: the `switch_ports` row (badge +
flap-suppressed `alarm` state) is always written; the operator page fires only on an
enter/leave edge and is gated by `WISP_SNMP_ALERTS`. There is no escalation ladder.

State (`down_streak`/`alarm`/`alarm_since`) lives in the row, so a daemon restart
mid-flap never loses the streak or re-pages a still-down port.
"""
from __future__ import annotations

from dataclasses import dataclass

from wisp.config import CONFIG, Config
from wisp.database.client import connect, write_with_retry
from wisp.ingress.snmp import PortStatus


@dataclass(frozen=True)
class PortEvent:
    """What a sync produced, for the daemon log + the tests."""
    device_id: int
    if_index: int
    kind: str                  # 'down' | 'up'
    port_label: str
    folded_into: int | None    # the fed device's outage was enriched (device_id) or None


def _port_label(p: PortStatus) -> str:
    base = p.if_name or f"if{p.if_index}"
    return f"{base} ({p.if_alias})" if p.if_alias else base


class PortMonitor:
    def __init__(self, notifier, cfg: Config = CONFIG) -> None:
        self.notifier = notifier
        self.cfg = cfg
        self.topic_operator = cfg.ntfy_topic_operator

    # -- public API: called by the daemon's SNMP task, once per switch --
    def sync_device(self, device_id: int, ports: list[PortStatus], ts: str) -> list[PortEvent]:
        """Persist every port for one switch and alarm/clear monitored ports on an edge.
        Returns the edge events (down/up) for the daemon log."""
        cfg = self.cfg
        events: list[PortEvent] = []
        with connect(cfg) as conn:
            existing = {
                r["if_index"]: r for r in conn.execute(
                    "SELECT * FROM switch_ports WHERE device_id=?", (device_id,))
            }
        for p in ports:
            prior = existing.get(p.if_index)
            monitored = bool(prior["monitored"]) if prior else False
            feeds = prior["feeds_device_id"] if prior else None
            prior_streak = prior["down_streak"] if prior else 0
            prior_alarm = bool(prior["alarm"]) if prior else False

            down = monitored and p.is_down()
            streak = (prior_streak + 1) if down else 0
            if streak >= cfg.snmp_down_consecutive:
                alarm = True
            elif streak == 0:
                alarm = False
            else:
                alarm = prior_alarm   # mid-confirmation: hold
            since = (ts if (alarm and not prior_alarm)
                     else (prior["alarm_since"] if (prior and alarm) else None))

            self._upsert_port(device_id, p, streak, alarm, since, ts)

            if alarm != prior_alarm:   # an edge
                ev = (self._on_port_down(device_id, p, feeds, ts)
                      if alarm else self._on_port_up(device_id, p, feeds, ts))
                events.append(ev)
        return events

    # -- detection edges --
    def _on_port_down(self, device_id, p: PortStatus, feeds, ts: str) -> PortEvent:
        switch = self._device_name(device_id)
        label = _port_label(p)
        folded_into = None
        if feeds is not None:
            fed_name = self._device_name(feeds)
            oid = self._open_outage_id(feeds)
            if oid is not None:
                # FOLD: enrich the existing outage with the physical cause instead of
                # raising a separate alarm (decision #3). Stamp root_cause only if the
                # operator hasn't logged one yet — never clobber a post-mortem.
                self._stamp_cause(oid, f"Port {label} down (SNMP) -> {fed_name}")
                folded_into = feeds
                self._page(f"🔌 Port down — {fed_name}",
                           f"{switch}: monitored port {label} is down (SNMP). This is the "
                           f"physical cause of the {fed_name} outage.",
                           device_id, oid, "PORT_DOWN", ts)
            else:
                # Leading indicator: the uplink port dropped but the fed device has no
                # open outage yet. ICMP owns outages — page a heads-up, don't open one.
                self._page(f"🔌 Uplink port down — {fed_name} at risk",
                           f"{switch}: monitored port {label} feeding {fed_name} is down "
                           f"(SNMP). {fed_name} is not yet ICMP-down.",
                           device_id, None, "PORT_DOWN", ts)
        else:
            self._page(f"🔌 Port down — {switch}",
                       f"{switch}: monitored port {label} is down (SNMP).",
                       device_id, None, "PORT_DOWN", ts)
        return PortEvent(device_id, p.if_index, "down", label, folded_into)

    def _on_port_up(self, device_id, p: PortStatus, feeds, ts: str) -> PortEvent:
        switch = self._device_name(device_id)
        label = _port_label(p)
        where = f" -> {self._device_name(feeds)}" if feeds is not None else ""
        self._page(f"✅ Port restored — {switch}",
                   f"{switch}: monitored port {label}{where} is back up (SNMP).",
                   device_id, None, "PORT_RESTORED", ts)
        return PortEvent(device_id, p.if_index, "up", label,
                         feeds if feeds is not None else None)

    # -- DB glue --
    def _upsert_port(self, device_id, p: PortStatus, streak, alarm, since, ts) -> None:
        """Discover/refresh one port. Operator-set fields (monitored, feeds_device_id) are
        NOT touched here — only the discovered status + flap-suppressed alarm state."""
        def _do():
            with connect(self.cfg) as conn:
                conn.execute(
                    "INSERT INTO switch_ports (device_id, if_index, if_name, if_alias,"
                    " admin_status, oper_status, last_change, down_streak, alarm,"
                    " alarm_since, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(device_id, if_index) DO UPDATE SET"
                    " if_name=excluded.if_name, if_alias=excluded.if_alias,"
                    " admin_status=excluded.admin_status, oper_status=excluded.oper_status,"
                    " last_change=excluded.last_change, down_streak=excluded.down_streak,"
                    " alarm=excluded.alarm, alarm_since=excluded.alarm_since,"
                    " updated_at=excluded.updated_at",
                    (device_id, p.if_index, p.if_name, p.if_alias, p.admin_status,
                     p.oper_status, p.last_change, streak, 1 if alarm else 0, since, ts),
                )
                conn.commit()
        write_with_retry(_do)

    def _open_outage_id(self, device_id: int) -> int | None:
        with connect(self.cfg) as conn:
            row = conn.execute(
                "SELECT id FROM outages WHERE device_id=? AND resolved_at IS NULL"
                " ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
        return row["id"] if row else None

    def _stamp_cause(self, outage_id: int, cause: str) -> None:
        def _do():
            with connect(self.cfg) as conn:
                conn.execute(
                    "UPDATE outages SET root_cause = COALESCE(root_cause, ?)"
                    " WHERE id=? AND resolved_at IS NULL", (cause, outage_id))
                conn.commit()
        write_with_retry(_do)

    def _device_name(self, device_id: int) -> str:
        with connect(self.cfg) as conn:
            row = conn.execute(
                "SELECT name FROM devices WHERE id=?", (device_id,)).fetchone()
        return row["name"] if row else f"#{device_id}"

    def _page(self, title, body, device_id, outage_id, payload, ts) -> None:
        """Page the OPERATOR once + log to alert_log. Operator-only, like the perf and
        redundancy soft-signal tiers — a port-down is not its own escalation track; when
        it folds into a device outage, that outage's ICMP path already pages owner+tech.
        Gated by WISP_SNMP_ALERTS — the switch_ports state is always written regardless."""
        sent_status = "suppressed"
        if self.cfg.snmp_alerts:
            res = self.notifier.send(self.topic_operator, title, body, 3)
            sent_status = "sent" if res.ok else "failed"

        def _do():
            with connect(self.cfg) as conn:
                conn.execute(
                    "INSERT INTO alert_log (outage_id, device_id, channel, recipient,"
                    " sent_at, status, payload) VALUES (?,?,?,?,?,?,?)",
                    (outage_id, device_id, self.notifier.channel, self.topic_operator,
                     ts, sent_status, payload),
                )
                conn.commit()
        write_with_retry(_do)
