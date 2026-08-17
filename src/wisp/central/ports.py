from __future__ import annotations

from dataclasses import dataclass

from wisp.central import history
from wisp.central.notify_policy import AlertRouter
from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse
from wisp.ingress.snmp import PortStatus, throughput_bps

STALE_S = 900

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

def _if_index(raw: dict) -> int | None:
    try:
        return int(raw.get("if_index"))
    except (TypeError, ValueError):
        return None

def _to_port_status(raw: dict, if_index: int, prior: dict | None) -> PortStatus:
    # An ABSENT key means that ifTable cell never arrived (a budget-bounded walk
    # dropped the column); a key PRESENT with None arrived empty and is
    # authoritative — that is what clears an alias somebody deleted on the box.
    def held(key: str):
        if key in raw:
            return raw[key]
        return prior[key] if prior else None
    return PortStatus(
        if_index=if_index,
        if_name=held("if_name"),
        if_alias=held("if_alias"),
        admin_status=str(raw.get("admin_status") or "unknown"),
        oper_status=str(raw.get("oper_status") or "unknown"),
        last_change=held("last_change"),
        in_octets=_to_int(raw.get("in_octets")),
        out_octets=_to_int(raw.get("out_octets")),
        speed_bps=_to_int(raw.get("speed_bps")),
    )

def _has_counters(raw: dict, p: PortStatus) -> bool:
    # Atomic on purpose: counters_at is ONE stamp for both directions, so taking
    # one direction's octets while holding the other's makes the next delta
    # divide by the wrong dt.
    return ("in_octets" in raw and "out_octets" in raw
            and p.in_octets is not None and p.out_octets is not None)

def _counter_regression(prior: dict | None, p: PortStatus) -> bool:
    # A counter reading BACKWARDS is a glitch, not a measurement. throughput_bps
    # already refuses the negative delta, so the glitch sweep itself published
    # nothing — what leaked was the BASELINE: the low value was stored, and the
    # next normal read subtracted against it and reported the port's whole
    # lifetime counter as one interval (NLK-OLT EPON0/3 at 121.85 Gb/s on a
    # 1 Gb/s PON, which the busy-hour panel then averaged). Either direction
    # condemns the pair, because counters_at is ONE stamp for both.
    if prior is None:
        return False
    for key, cur in (("in_octets", p.in_octets), ("out_octets", p.out_octets)):
        stored = _to_int(prior[key])
        if stored is not None and cur is not None and cur < stored:
            return True
    return False

def _baseline_stale(prior: dict | None, ts: str) -> bool:
    # The escape hatch for a genuine reboot: past STALE_S nobody has refreshed
    # the baseline, so holding it protects nothing and would strand the port
    # publishing no rate for ever. Adopt the lower value, skip the gap, resume
    # next sweep. This is also what bounds the hold after a bogus HIGH reading —
    # every honest read after one looks like a regression.
    if prior is None:
        return True
    at = prior["counters_at"]
    if not at:
        return True
    return _dt_seconds(at, ts) > STALE_S

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

def _bw_bound(hit: bool | None, eligible: bool, prior_streak: int,
              prior_alarm: bool, prior_since: str | None, consecutive: int,
              ts: str) -> tuple[int, bool, str | None]:
    if hit is True:
        streak = prior_streak + 1
    elif hit is False:
        streak = 0
    else:
        streak = prior_streak if eligible else 0
    if not eligible:
        alarm = False
    elif streak >= consecutive:
        alarm = True
    elif streak == 0:
        alarm = False
    else:
        alarm = prior_alarm
    since = (ts if (alarm and not prior_alarm)
            else (prior_since if alarm else None))
    return streak, alarm, since

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
        hist_rows: list[tuple] = []
        port_rows: list[dict] = []
        for raw in raw_ports:
            if_index = _if_index(raw)
            if if_index is None:
                continue
            prior = existing.get(if_index)
            p = _to_port_status(raw, if_index, prior)
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

            fresh_counters = _has_counters(raw, p)
            threshold = prior["bw_threshold_mbps"] if prior else None
            max_threshold = prior["bw_max_mbps"] if prior else None
            direction = (prior["bw_direction"] if prior else None) or "either"
            prior_bw_alarm = bool(prior["bw_alarm"]) if prior else False
            prior_bw_high_alarm = bool(prior["bw_high_alarm"]) if prior else False
            bw_eligible = (monitored and threshold is not None
                          and p.oper_status == "up" and not down)
            high_eligible = (monitored and max_threshold is not None
                            and p.oper_status == "up" and not down)
            # A backwards counter is treated as a counter-LESS sweep: the stored
            # baseline is kept and no rate is measured, so the next good read
            # measures correctly across the longer interval. Only the LOW side is
            # guarded — a suspiciously high reading is still adopted outright,
            # which is what let TMG-OLT self-heal in two sweeps.
            regressed = fresh_counters and _counter_regression(prior, p)
            rebooted = regressed and _baseline_stale(prior, ts)
            measured = fresh_counters and not regressed
            if measured:
                dt = _dt_seconds(prior["counters_at"] if prior else None, ts)
                in_octets, out_octets, counters_at = p.in_octets, p.out_octets, ts
                in_bps = throughput_bps(_to_int(prior["in_octets"]) if prior else None,
                                        p.in_octets, dt)
                out_bps = throughput_bps(_to_int(prior["out_octets"]) if prior else None,
                                         p.out_octets, dt)
                bw_streak, bw_alarm, bw_since = _bw_bound(
                    _bw_below(in_bps, out_bps, threshold * 1e6, direction)
                    if bw_eligible else None,
                    bw_eligible, prior["bw_low_streak"] if prior else 0,
                    prior_bw_alarm, prior["bw_alarm_since"] if prior else None,
                    cfg.snmp_bw_consecutive, ts)
                bw_high_streak, bw_high_alarm, bw_high_since = _bw_bound(
                    _bw_above(in_bps, out_bps, max_threshold * 1e6, direction)
                    if high_eligible else None,
                    high_eligible, prior["bw_high_streak"] if prior else 0,
                    prior_bw_high_alarm,
                    prior["bw_high_alarm_since"] if prior else None,
                    cfg.snmp_bw_consecutive, ts)
            else:
                # The walk dropped the counter columns, or the pair read
                # backwards. The stored octets are the baseline the NEXT
                # complete pair is measured against — wiping them is why a rate
                # needs two consecutive complete walks and a big OLT never got
                # one. A held rate is still a claim about now,
                # so it expires; the alarm bounds got no rate evidence this sweep
                # (hit=None holds an eligible streak), but eligibility rides oper
                # status, which IS current — a port that went down still clears.
                if rebooted:
                    in_octets, out_octets, counters_at = p.in_octets, p.out_octets, ts
                else:
                    in_octets = prior["in_octets"] if prior else None
                    out_octets = prior["out_octets"] if prior else None
                    counters_at = prior["counters_at"] if prior else None
                # Judged on the PRIOR stamp, never the one just adopted: a
                # reboot's gap must not resurrect a rate that had already expired.
                base_at = prior["counters_at"] if prior else None
                still_now = (base_at is not None
                             and _dt_seconds(base_at, ts) <= STALE_S)
                in_bps = prior["in_bps"] if still_now else None
                out_bps = prior["out_bps"] if still_now else None
                bw_streak, bw_alarm, bw_since = _bw_bound(
                    None, bw_eligible, prior["bw_low_streak"] if prior else 0,
                    prior_bw_alarm, prior["bw_alarm_since"] if prior else None,
                    cfg.snmp_bw_consecutive, ts)
                bw_high_streak, bw_high_alarm, bw_high_since = _bw_bound(
                    None, high_eligible, prior["bw_high_streak"] if prior else 0,
                    prior_bw_high_alarm,
                    prior["bw_high_alarm_since"] if prior else None,
                    cfg.snmp_bw_consecutive, ts)

            port_rows.append({
                "if_index": p.if_index, "if_name": p.if_name,
                "if_alias": p.if_alias, "admin_status": p.admin_status,
                "oper_status": p.oper_status, "last_change": p.last_change,
                "down_streak": streak, "alarm": alarm, "alarm_since": since,
                "ts": ts,
                "bw": (in_octets, out_octets, counters_at, in_bps, out_bps,
                       bw_streak, bw_alarm, bw_since, bw_high_streak,
                       bw_high_alarm, bw_high_since)})
            if history.port_eligible(prior):
                # A held rate is not a fresh measurement — the historian would
                # draw one walk's number as a run of samples.
                hist_rows.append((p.if_index,
                                  in_bps if measured else None,
                                  out_bps if measured else None,
                                  p.oper_status == "up"))

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
        self.store.upsert_switch_ports_many(self.org_id, device_id, port_rows)
        history.record_ports(self.store, cfg, self.org_id, device_id, ts,
                             hist_rows)
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
        self.router.emit(
            payload,
            title=title, body=body, priority=3, ts=ts, device_id=device_id,
            outage_id=outage_id, gate=gate, cooldown_min=0)
