from __future__ import annotations

from dataclasses import dataclass

from wisp.central.notify_policy import AlertRouter
from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse
from wisp.ingress.snmp import PortStatus, throughput_bps

@dataclass(frozen=True)
class PortEvent:
    device_id: int
    if_index: int
    kind: str
    port_label: str
    folded_into: int | None

def _label(p: PortStatus) -> str:
    base = p.if_name or f"if{p.if_index}"
    return f"{base} ({p.if_alias})" if p.if_alias else base

def _to_port_status(raw: dict) -> PortStatus | None:
    try:
        if_index = int(raw.get("if_index"))
    except (TypeError, ValueError):
        return None
    return PortStatus(
        if_index=if_index,
        if_name=raw.get("if_name"),
        if_alias=raw.get("if_alias"),
        admin_status=str(raw.get("admin_status") or "unknown"),
        oper_status=str(raw.get("oper_status") or "unknown"),
        last_change=raw.get("last_change"),
        in_octets=_to_int(raw.get("in_octets")),
        out_octets=_to_int(raw.get("out_octets")),
        speed_bps=_to_int(raw.get("speed_bps")),
    )

def _to_int(raw) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None

def _dt_seconds(prev_ts: str | None, cur_ts: str) -> float:
    if not prev_ts:
        return 0.0
    try:
        return (_parse(cur_ts) - _parse(prev_ts)).total_seconds()
    except (ValueError, TypeError):
        return 0.0

def _bw_below(in_bps: float | None, out_bps: float | None, threshold_bps: float,
             direction: str) -> bool | None:
    if direction == "in":
        return None if in_bps is None else in_bps < threshold_bps
    if direction == "out":
        return None if out_bps is None else out_bps < threshold_bps
    if direction == "total":
        if in_bps is None or out_bps is None:
            return None
        return (in_bps + out_bps) < threshold_bps
    vals = [v for v in (in_bps, out_bps) if v is not None]
    if not vals:
        return None
    return any(v < threshold_bps for v in vals)

def _bw_above(in_bps: float | None, out_bps: float | None, max_bps: float,
             direction: str) -> bool | None:
    if direction == "in":
        return None if in_bps is None else in_bps > max_bps
    if direction == "out":
        return None if out_bps is None else out_bps > max_bps
    if direction == "total":
        if in_bps is None or out_bps is None:
            return None
        return (in_bps + out_bps) > max_bps
    vals = [v for v in (in_bps, out_bps) if v is not None]
    if not vals:
        return None
    return any(v > max_bps for v in vals)

def _fmt_rate(bps: float | None) -> str:
    if bps is None:
        return "—"
    mbps = bps / 1e6
    return f"{mbps / 1000:.2f} Gbps" if mbps >= 1000 else f"{mbps:.1f} Mbps"

class CentralPortMonitor:

    def __init__(self, store, org_id: str, notifier, cfg: Config = CONFIG) -> None:
        self.store = store
        self.org_id = org_id
        self.notifier = notifier
        self.cfg = cfg
        self.router = AlertRouter(store, org_id, notifier, cfg)

    def sync_device(self, device_id: int, raw_ports: list[dict], ts: str) -> list[PortEvent]:
        cfg = self.cfg
        existing = {r["if_index"]: r for r in
                   self.store.list_switch_ports(self.org_id, device_id)}
        events: list[PortEvent] = []
        for raw in raw_ports:
            p = _to_port_status(raw)
            if p is None:
                continue
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
                alarm = prior_alarm
            since = (ts if (alarm and not prior_alarm)
                    else (prior["alarm_since"] if (prior and alarm) else None))

            dt = _dt_seconds(prior["counters_at"] if prior else None, ts)
            in_bps = throughput_bps(_to_int(prior["in_octets"]) if prior else None,
                                    p.in_octets, dt)
            out_bps = throughput_bps(_to_int(prior["out_octets"]) if prior else None,
                                     p.out_octets, dt)

            threshold = prior["bw_threshold_mbps"] if prior else None
            direction = (prior["bw_direction"] if prior else None) or "either"
            prior_bw_streak = prior["bw_low_streak"] if prior else 0
            prior_bw_alarm = bool(prior["bw_alarm"]) if prior else False
            bw_eligible = (monitored and threshold is not None
                          and p.oper_status == "up" and not down)
            below = (_bw_below(in_bps, out_bps, threshold * 1e6, direction)
                    if bw_eligible else None)
            if below is True:
                bw_streak = prior_bw_streak + 1
            elif below is False:
                bw_streak = 0
            else:
                bw_streak = prior_bw_streak if bw_eligible else 0
            if not bw_eligible:
                bw_alarm = False
            elif bw_streak >= cfg.snmp_bw_consecutive:
                bw_alarm = True
            elif bw_streak == 0:
                bw_alarm = False
            else:
                bw_alarm = prior_bw_alarm
            bw_since = (ts if (bw_alarm and not prior_bw_alarm)
                       else (prior["bw_alarm_since"] if (prior and bw_alarm) else None))

            max_threshold = prior["bw_max_mbps"] if prior else None
            prior_bw_high_streak = prior["bw_high_streak"] if prior else 0
            prior_bw_high_alarm = bool(prior["bw_high_alarm"]) if prior else False
            high_eligible = (monitored and max_threshold is not None
                            and p.oper_status == "up" and not down)
            above = (_bw_above(in_bps, out_bps, max_threshold * 1e6, direction)
                    if high_eligible else None)
            if above is True:
                bw_high_streak = prior_bw_high_streak + 1
            elif above is False:
                bw_high_streak = 0
            else:
                bw_high_streak = prior_bw_high_streak if high_eligible else 0
            if not high_eligible:
                bw_high_alarm = False
            elif bw_high_streak >= cfg.snmp_bw_consecutive:
                bw_high_alarm = True
            elif bw_high_streak == 0:
                bw_high_alarm = False
            else:
                bw_high_alarm = prior_bw_high_alarm
            bw_high_since = (ts if (bw_high_alarm and not prior_bw_high_alarm)
                            else (prior["bw_high_alarm_since"]
                                 if (prior and bw_high_alarm) else None))

            self.store.upsert_switch_port(
                self.org_id, device_id, p.if_index, p.if_name, p.if_alias,
                p.admin_status, p.oper_status, p.last_change, streak, alarm, since, ts,
                bw=(p.in_octets, p.out_octets, ts, in_bps, out_bps, bw_streak, bw_alarm,
                   bw_since, bw_high_streak, bw_high_alarm, bw_high_since))

            if alarm != prior_alarm:
                ev = (self._on_down(device_id, p, feeds, ts) if alarm
                     else self._on_up(device_id, p, feeds, ts))
                events.append(ev)
            if bw_alarm and not prior_bw_alarm:
                events.append(self._on_bw_low(device_id, p, feeds, in_bps, out_bps,
                                              threshold, direction, ts))
            elif prior_bw_alarm and not bw_alarm and bw_eligible:
                events.append(self._on_bw_ok(device_id, p, feeds, in_bps, out_bps, ts))
            if bw_high_alarm and not prior_bw_high_alarm:
                events.append(self._on_bw_high(device_id, p, feeds, in_bps, out_bps,
                                               max_threshold, direction, ts))
            elif prior_bw_high_alarm and not bw_high_alarm and high_eligible:
                events.append(self._on_bw_normal(device_id, p, feeds, in_bps, out_bps, ts))
        return events

    def _on_down(self, device_id: int, p: PortStatus, feeds: int | None, ts: str) -> PortEvent:
        switch = self._name(device_id)
        label = _label(p)
        folded_into = None
        if feeds is not None:
            fed_name = self._name(feeds)
            oid = self.store.open_outage_id(self.org_id, feeds)
            if oid is not None:
                self.store.stamp_outage_cause(
                    self.org_id, oid, f"Port {label} down (SNMP) -> {fed_name}")
                folded_into = feeds
                self._page(f"\U0001f50c Port down: {fed_name}",
                          f"{switch} port {label}",
                          device_id, oid, "PORT_DOWN", ts)
            else:
                self._page(f"\U0001f50c Uplink port down: {fed_name} at risk",
                          f"{switch} port {label}",
                          device_id, None, "PORT_DOWN", ts)
        else:
            self._page(f"\U0001f50c Port down: {switch}",
                      f"Port {label}",
                      device_id, None, "PORT_DOWN", ts)
        return PortEvent(device_id, p.if_index, "down", label, folded_into)

    def _on_up(self, device_id: int, p: PortStatus, feeds: int | None, ts: str) -> PortEvent:
        switch = self._name(device_id)
        label = _label(p)
        self._page(f"✅ Port restored: {switch}",
                  f"Port {label}",
                  device_id, None, "PORT_RESTORED", ts)
        return PortEvent(device_id, p.if_index, "up", label, feeds)

    def _on_bw_low(self, device_id: int, p: PortStatus, feeds: int | None,
                   in_bps: float | None, out_bps: float | None, threshold: float,
                   direction: str, ts: str) -> PortEvent:
        switch = self._name(device_id)
        label = _label(p)
        self._page(f"\U0001f4c9 Low bandwidth: {switch}",
                  f"Port {label}: in {_fmt_rate(in_bps)} / out {_fmt_rate(out_bps)} "
                  f"(< {threshold:g} Mbps)",
                  device_id, None, "PORT_BW_LOW", ts, enabled=self.cfg.snmp_bw_alerts)
        return PortEvent(device_id, p.if_index, "bw_low", label, None)

    def _on_bw_ok(self, device_id: int, p: PortStatus, feeds: int | None,
                  in_bps: float | None, out_bps: float | None, ts: str) -> PortEvent:
        switch = self._name(device_id)
        label = _label(p)
        self._page(f"\U0001f4c8 Bandwidth recovered: {switch}",
                  f"Port {label}: in {_fmt_rate(in_bps)} / out {_fmt_rate(out_bps)}",
                  device_id, None, "PORT_BW_OK", ts, enabled=self.cfg.snmp_bw_alerts)
        return PortEvent(device_id, p.if_index, "bw_ok", label, None)

    def _on_bw_high(self, device_id: int, p: PortStatus, feeds: int | None,
                    in_bps: float | None, out_bps: float | None, max_mbps: float,
                    direction: str, ts: str) -> PortEvent:
        switch = self._name(device_id)
        label = _label(p)
        self._page(f"\U0001f4c8 High bandwidth: {switch}",
                  f"Port {label}: in {_fmt_rate(in_bps)} / out {_fmt_rate(out_bps)} "
                  f"(> {max_mbps:g} Mbps)",
                  device_id, None, "PORT_BW_HIGH", ts, enabled=self.cfg.snmp_bw_alerts)
        return PortEvent(device_id, p.if_index, "bw_high", label, None)

    def _on_bw_normal(self, device_id: int, p: PortStatus, feeds: int | None,
                      in_bps: float | None, out_bps: float | None, ts: str) -> PortEvent:
        switch = self._name(device_id)
        label = _label(p)
        self._page(f"\U0001f4c9 Bandwidth normalized: {switch}",
                  f"Port {label}: in {_fmt_rate(in_bps)} / out {_fmt_rate(out_bps)}",
                  device_id, None, "PORT_BW_NORMAL", ts, enabled=self.cfg.snmp_bw_alerts)
        return PortEvent(device_id, p.if_index, "bw_normal", label, None)

    def _name(self, device_id: int) -> str:
        dev = self.store.get_org_device(self.org_id, device_id)
        return dev["name"] if dev else f"#{device_id}"

    def _page(self, title: str, body: str, device_id: int, outage_id: int | None,
              payload: str, ts: str, *, enabled: bool | None = None) -> None:
        gate = self.cfg.snmp_alerts if enabled is None else enabled
        # Port up/down AND bandwidth crossings are per-if_index; a device-level
        # cooldown would swallow a second port dropping (or saturating) on the
        # same switch. All are already streak- and transition-gated, so no
        # cooldown — matters now that bandwidth PUSHes immediately, not via digest.
        self.router.emit(
            payload, topic=self.store.org_role_topic(self.org_id, "operator"),
            title=title, body=body, priority=3, ts=ts, device_id=device_id,
            outage_id=outage_id, gate=gate, cooldown_min=0)
