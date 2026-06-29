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
from wisp.core.analytics import _parse
from wisp.database.client import connect, write_with_retry
from wisp.ingress.snmp import PortStatus, throughput_bps

# Watched-direction options for the low-bandwidth alarm (operator picks per port — "for an
# ISP, whichever is important" for that link). 'either' alarms if in OR out drops below.
BW_DIRECTIONS = ("in", "out", "either", "total")


@dataclass(frozen=True)
class PortEvent:
    """What a sync produced, for the daemon log + the tests."""
    device_id: int
    if_index: int
    kind: str                  # 'down' | 'up' | 'bw_low' | 'bw_ok'
    port_label: str
    folded_into: int | None    # the fed device's outage was enriched (device_id) or None


def _port_label(p: PortStatus) -> str:
    base = p.if_name or f"if{p.if_index}"
    return f"{base} ({p.if_alias})" if p.if_alias else base


def _to_int(raw) -> int | None:
    """A stored TEXT octet counter back to int (Counter64 can exceed 2**63, so it's kept
    as text in the DB and as an arbitrary-precision Python int here)."""
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _dt_seconds(prev_ts: str | None, cur_ts: str) -> float:
    """Seconds between two ISO8601 stamps (reusing analytics._parse for the mixed
    'T'/space + offset formats). 0.0 if there's no prior stamp — throughput_bps then
    returns None for the first sample of a port."""
    if not prev_ts:
        return 0.0
    try:
        return (_parse(cur_ts) - _parse(prev_ts)).total_seconds()
    except (ValueError, TypeError):
        return 0.0


def _bw_below(in_bps: float | None, out_bps: float | None,
              threshold_bps: float, direction: str) -> bool | None:
    """Is the watched rate below the threshold? None when the needed rate(s) aren't
    available yet (first sample / counter reset) so the caller can HOLD rather than
    falsely trip or clear. 'either' trips if any available direction is below."""
    if direction == "in":
        return None if in_bps is None else in_bps < threshold_bps
    if direction == "out":
        return None if out_bps is None else out_bps < threshold_bps
    if direction == "total":
        if in_bps is None or out_bps is None:
            return None
        return (in_bps + out_bps) < threshold_bps
    vals = [v for v in (in_bps, out_bps) if v is not None]   # 'either'
    if not vals:
        return None
    return any(v < threshold_bps for v in vals)


def _fmt_rate(bps: float | None) -> str:
    if bps is None:
        return "—"
    mbps = bps / 1e6
    return f"{mbps / 1000:.2f} Gbps" if mbps >= 1000 else f"{mbps:.1f} Mbps"


def _fmt_rates(in_bps: float | None, out_bps: float | None) -> str:
    return f"in {_fmt_rate(in_bps)} / out {_fmt_rate(out_bps)}"


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

            # --- throughput from the counter delta vs the previous walk ---------
            dt = _dt_seconds(prior["counters_at"] if prior else None, ts)
            in_bps = throughput_bps(
                _to_int(prior["in_octets"]) if prior else None, p.in_octets, dt)
            out_bps = throughput_bps(
                _to_int(prior["out_octets"]) if prior else None, p.out_octets, dt)

            # --- low-bandwidth detection (its own flap-suppressed alarm) ---------
            threshold = prior["bw_threshold_mbps"] if prior else None
            direction = (prior["bw_direction"] if prior else None) or "either"
            prior_bw_streak = prior["bw_low_streak"] if prior else 0
            prior_bw_alarm = bool(prior["bw_alarm"]) if prior else False
            # Only judge throughput on a monitored port with a threshold that is genuinely
            # UP — a down/admin-down port legitimately carries ~0, and its outage/down
            # alarm already owns that story (don't double-page low-bw on a dead port).
            bw_eligible = (monitored and threshold is not None
                           and p.oper_status == "up" and not down)
            below = (_bw_below(in_bps, out_bps, threshold * 1e6, direction)
                     if bw_eligible else None)
            if below is True:
                bw_streak = prior_bw_streak + 1
            elif below is False:
                bw_streak = 0
            else:   # not evaluable (no rate yet) — hold while eligible, else reset
                bw_streak = prior_bw_streak if bw_eligible else 0
            if not bw_eligible:
                bw_alarm = False
            elif bw_streak >= cfg.snmp_bw_consecutive:
                bw_alarm = True
            elif bw_streak == 0:
                bw_alarm = False
            else:
                bw_alarm = prior_bw_alarm   # mid-confirmation: hold
            bw_since = (ts if (bw_alarm and not prior_bw_alarm)
                        else (prior["bw_alarm_since"] if (prior and bw_alarm) else None))

            self._upsert_port(device_id, p, streak, alarm, since,
                              (in_bps, out_bps, p.speed_bps, bw_streak, bw_alarm, bw_since), ts)

            if alarm != prior_alarm:   # oper-status edge
                ev = (self._on_port_down(device_id, p, feeds, ts)
                      if alarm else self._on_port_up(device_id, p, feeds, ts))
                events.append(ev)
            if bw_alarm and not prior_bw_alarm:           # entered low-bandwidth
                events.append(self._on_bw_low(device_id, p, feeds, in_bps, out_bps,
                                              threshold, direction, ts))
            elif prior_bw_alarm and not bw_alarm and bw_eligible:   # genuine recovery
                events.append(self._on_bw_ok(device_id, p, feeds, in_bps, out_bps, ts))
            # else: bw cleared because the port is no longer eligible (it went down /
            # admin-down) — clear the badge SILENTLY; the down alarm owns that story.
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

    # -- bandwidth edges (operator heads-up; NOT folded into an outage — the port still
    #    pings "up", this is a traffic anomaly, not a hard down) --
    def _on_bw_low(self, device_id, p: PortStatus, feeds, in_bps, out_bps,
                   threshold, direction, ts: str) -> PortEvent:
        switch = self._device_name(device_id)
        label = _port_label(p)
        where = f" -> {self._device_name(feeds)}" if feeds is not None else ""
        self._page(
            f"📉 Low bandwidth — {switch}",
            f"{switch}: monitored port {label}{where} throughput fell below "
            f"{threshold:g} Mbps ({direction}). Now {_fmt_rates(in_bps, out_bps)}.",
            device_id, None, "PORT_BW_LOW", ts, enabled=self.cfg.snmp_bw_alerts)
        return PortEvent(device_id, p.if_index, "bw_low", label, None)

    def _on_bw_ok(self, device_id, p: PortStatus, feeds, in_bps, out_bps, ts: str) -> PortEvent:
        switch = self._device_name(device_id)
        label = _port_label(p)
        self._page(
            f"📈 Bandwidth recovered — {switch}",
            f"{switch}: monitored port {label} throughput is back above threshold. "
            f"Now {_fmt_rates(in_bps, out_bps)}.",
            device_id, None, "PORT_BW_OK", ts, enabled=self.cfg.snmp_bw_alerts)
        return PortEvent(device_id, p.if_index, "bw_ok", label, None)

    # -- DB glue --
    def _upsert_port(self, device_id, p: PortStatus, streak, alarm, since, bw, ts) -> None:
        """Discover/refresh one port. Operator-set fields (monitored, feeds_device_id,
        bw_threshold_mbps, bw_direction) are NOT touched here — only the discovered status,
        the flap-suppressed alarm states, and the bandwidth counters/rates. `bw` is the
        tuple (in_bps, out_bps, speed_bps, bw_low_streak, bw_alarm, bw_alarm_since)."""
        in_bps, out_bps, speed_bps, bw_streak, bw_alarm, bw_since = bw
        # Raw counters are stored as TEXT (Counter64 can exceed SQLite's signed-64 range)
        # so the next walk can diff them; counters_at is the stamp the delta is measured from.
        in_oct = None if p.in_octets is None else str(p.in_octets)
        out_oct = None if p.out_octets is None else str(p.out_octets)

        def _do():
            with connect(self.cfg) as conn:
                conn.execute(
                    "INSERT INTO switch_ports (device_id, if_index, if_name, if_alias,"
                    " admin_status, oper_status, last_change, down_streak, alarm,"
                    " alarm_since, in_octets, out_octets, counters_at, in_bps, out_bps,"
                    " if_speed_bps, bw_low_streak, bw_alarm, bw_alarm_since, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(device_id, if_index) DO UPDATE SET"
                    " if_name=excluded.if_name, if_alias=excluded.if_alias,"
                    " admin_status=excluded.admin_status, oper_status=excluded.oper_status,"
                    " last_change=excluded.last_change, down_streak=excluded.down_streak,"
                    " alarm=excluded.alarm, alarm_since=excluded.alarm_since,"
                    " in_octets=excluded.in_octets, out_octets=excluded.out_octets,"
                    " counters_at=excluded.counters_at, in_bps=excluded.in_bps,"
                    " out_bps=excluded.out_bps, if_speed_bps=excluded.if_speed_bps,"
                    " bw_low_streak=excluded.bw_low_streak, bw_alarm=excluded.bw_alarm,"
                    " bw_alarm_since=excluded.bw_alarm_since, updated_at=excluded.updated_at",
                    (device_id, p.if_index, p.if_name, p.if_alias, p.admin_status,
                     p.oper_status, p.last_change, streak, 1 if alarm else 0, since,
                     in_oct, out_oct, ts, in_bps, out_bps, speed_bps,
                     bw_streak, 1 if bw_alarm else 0, bw_since, ts),
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

    def _page(self, title, body, device_id, outage_id, payload, ts, enabled=None) -> None:
        """Page the OPERATOR once + log to alert_log. Operator-only, like the perf and
        redundancy soft-signal tiers — a port-down is not its own escalation track; when
        it folds into a device outage, that outage's ICMP path already pages owner+tech.
        Gated by the relevant alerts flag — port status by WISP_SNMP_ALERTS, bandwidth by
        WISP_SNMP_BW_ALERTS (passed via `enabled`). The switch_ports state is always
        written regardless of the gate."""
        enabled = self.cfg.snmp_alerts if enabled is None else enabled
        sent_status = "suppressed"
        if enabled:
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
