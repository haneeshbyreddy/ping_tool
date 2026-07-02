"""Central-side SNMP port folding (CLAUDE.md item 1, done) + per-port bandwidth (CLAUDE.md
item 3's SNMP-bandwidth follow-up).

Mirrors the old single-box `egress/ports.py` (`PortMonitor`) one-for-one, ported onto
`CentralStore`'s org-scoped `switch_ports` table + an org's operator ntfy topic
instead of a fixed per-process `Config`. Three product rules carried over verbatim for
port STATUS:

  * **Monitored-only.** Only a port an operator has ticked `monitored` (dashboard
    inventory API — discovery alone lands every port `monitored=0`) can ever alarm.
  * **Admin-down stays silent.** The alarm condition is `PortStatus.is_down()`,
    imported and reused as-is from `ingress/snmp.py` — never re-derive the
    admin/oper predicate on this side of the wire; that would risk the two sides
    drifting on what "down" means.
  * **One alarm, not two.** A monitored port-down never opens or pages its own
    outage. If it `feeds_device_id` a device with a currently open outage, it folds
    in — stamping `outages.root_cause` (via `CentralStore.stamp_outage_cause`,
    COALESCE, never clobbering an operator's own post-mortem) and sending ONE
    operator heads-up naming the fold. If the fed device has no open outage yet,
    it's a leading indicator: still just an operator heads-up, and it STILL never
    opens an outage — the FSM (`central/engine.py`) owns outages, exclusively, even
    here. A port with no `feeds_device_id` pages for the switch itself.

Bandwidth (throughput) is an ORTHOGONAL soft signal on the same walk: `PortStatus`
already carries the 64-bit octet counters + link speed (`ingress/snmp.py`); this module
diffs two walks' counters via `throughput_bps` into a live in/out rate and alarms when a
MONITORED port's rate falls below an operator-assigned per-port threshold
(`bw_threshold_mbps`/`bw_direction`, `None` threshold = no bandwidth alarm for that
port) for `cfg.snmp_bw_consecutive` consecutive walks — its OWN streak, because traffic
is burstier than link state. Only judged on a port that is genuinely up (a down/
admin-down port legitimately carries ~0 and its own alarm already owns that story) —
never a competing "it's also slow" page on a dead port.

Flap suppression is in-row (`down_streak`/`alarm`, `bw_low_streak`/`bw_alarm`),
restart-safe like the FSM's own `device_states`. Both pages are operator-only, gated by
`cfg.snmp_alerts`/`cfg.snmp_bw_alerts` respectively; the `switch_ports` row is always
written regardless — there is no escalation ladder here, unlike the ICMP outage ladder
in `central/dispatch.py`.
"""
from __future__ import annotations

from dataclasses import dataclass

from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse
from wisp.ingress.snmp import PortStatus, throughput_bps


@dataclass(frozen=True)
class PortEvent:
    """What one sync produced, for the caller's log (mirrors the old edge's
    daemon-log line)."""
    device_id: int
    if_index: int
    kind: str                  # 'down' | 'up' | 'bw_low' | 'bw_ok'
    port_label: str
    folded_into: int | None    # the fed device's outage was enriched (device_id) or None


def _label(p: PortStatus) -> str:
    base = p.if_name or f"if{p.if_index}"
    return f"{base} ({p.if_alias})" if p.if_alias else base


def _to_port_status(raw: dict) -> PortStatus | None:
    """One wire-format port reading (a dict off `POST /report`'s `ports` key) ->
    `PortStatus`, so `.is_down()` stays the single source of truth for the alarm
    predicate on both sides of the wire. `None` if `if_index` is missing/unparsable —
    a malformed reading is dropped, not crashed on."""
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
    """A wire/DB octet counter (text or int) back to a Python int (Counter64 can
    exceed 2**63, so it's kept as arbitrary-precision on both sides, never a fixed-
    width type)."""
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _dt_seconds(prev_ts: str | None, cur_ts: str) -> float:
    """Seconds between two ISO8601 stamps (reusing `core/analytics._parse` for the
    mixed 'T'/space + offset formats central and the edge both produce). 0.0 with no
    prior stamp, so `throughput_bps` returns None for a port's first-ever sample."""
    if not prev_ts:
        return 0.0
    try:
        return (_parse(cur_ts) - _parse(prev_ts)).total_seconds()
    except (ValueError, TypeError):
        return 0.0


def _bw_below(in_bps: float | None, out_bps: float | None, threshold_bps: float,
             direction: str) -> bool | None:
    """Is the watched rate below the threshold? `None` when the needed rate(s) aren't
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


class CentralPortMonitor:
    """One instance per report; `sync_device` is called once per switch that reported
    ports that cycle (`central/server.py:_report`, after the ICMP cycle has already
    committed — so `store.open_outage_id` reflects THIS cycle's outages, not last
    cycle's). `device_id`/`feeds_device_id` are `org_devices` ids; the caller is
    responsible for checking `device_id` belongs to `org_id` (via the org's own
    `EngineRegistry` meta) before this ever runs — this class trusts its input."""

    def __init__(self, store, org_id: str, notifier, cfg: Config = CONFIG) -> None:
        self.store = store
        self.org_id = org_id
        self.notifier = notifier
        self.cfg = cfg

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
                alarm = prior_alarm   # mid-confirmation: hold the prior verdict
            since = (ts if (alarm and not prior_alarm)
                    else (prior["alarm_since"] if (prior and alarm) else None))

            # --- throughput from the counter delta vs the previous walk ---------
            dt = _dt_seconds(prior["counters_at"] if prior else None, ts)
            in_bps = throughput_bps(_to_int(prior["in_octets"]) if prior else None,
                                    p.in_octets, dt)
            out_bps = throughput_bps(_to_int(prior["out_octets"]) if prior else None,
                                     p.out_octets, dt)

            # --- low-bandwidth detection (its own flap-suppressed alarm) ---------
            threshold = prior["bw_threshold_mbps"] if prior else None
            direction = (prior["bw_direction"] if prior else None) or "either"
            prior_bw_streak = prior["bw_low_streak"] if prior else 0
            prior_bw_alarm = bool(prior["bw_alarm"]) if prior else False
            # Only judge throughput on a monitored, genuinely-up port — a down/
            # admin-down port legitimately carries ~0 and its own alarm already owns
            # that story; don't double-page low-bw on a dead port.
            bw_eligible = (monitored and threshold is not None
                          and p.oper_status == "up" and not down)
            below = (_bw_below(in_bps, out_bps, threshold * 1e6, direction)
                    if bw_eligible else None)
            if below is True:
                bw_streak = prior_bw_streak + 1
            elif below is False:
                bw_streak = 0
            else:   # not evaluable yet — hold while eligible, else reset
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

            self.store.upsert_switch_port(
                self.org_id, device_id, p.if_index, p.if_name, p.if_alias,
                p.admin_status, p.oper_status, p.last_change, streak, alarm, since, ts,
                bw=(p.in_octets, p.out_octets, ts, in_bps, out_bps, bw_streak, bw_alarm,
                   bw_since))

            if alarm != prior_alarm:   # oper-status edge
                ev = (self._on_down(device_id, p, feeds, ts) if alarm
                     else self._on_up(device_id, p, feeds, ts))
                events.append(ev)
            if bw_alarm and not prior_bw_alarm:                # entered low-bandwidth
                events.append(self._on_bw_low(device_id, p, feeds, in_bps, out_bps,
                                              threshold, direction, ts))
            elif prior_bw_alarm and not bw_alarm and bw_eligible:  # genuine recovery
                events.append(self._on_bw_ok(device_id, p, feeds, in_bps, out_bps, ts))
            # else: bw cleared because the port stopped being eligible (went down /
            # admin-down) — clear the badge SILENTLY; the down alarm owns that story.
        return events

    # -- detection edges --
    def _on_down(self, device_id: int, p: PortStatus, feeds: int | None, ts: str) -> PortEvent:
        switch = self._name(device_id)
        label = _label(p)
        folded_into = None
        if feeds is not None:
            fed_name = self._name(feeds)
            oid = self.store.open_outage_id(self.org_id, feeds)
            if oid is not None:
                # FOLD: enrich the existing outage with the physical cause instead of
                # raising a separate alarm. ICMP/the FSM still owns the outage record.
                self.store.stamp_outage_cause(
                    self.org_id, oid, f"Port {label} down (SNMP) -> {fed_name}")
                folded_into = feeds
                self._page(f"\U0001f50c Port down — {fed_name}",
                          f"{switch}: monitored port {label} is down (SNMP). This is the "
                          f"physical cause of the {fed_name} outage.",
                          device_id, oid, "PORT_DOWN", ts)
            else:
                # Leading indicator: the uplink port dropped but the fed device has no
                # open outage (yet). SNMP never opens one — just a heads-up.
                self._page(f"\U0001f50c Uplink port down — {fed_name} at risk",
                          f"{switch}: monitored port {label} feeding {fed_name} is down "
                          f"(SNMP). {fed_name} is not yet reporting DOWN.",
                          device_id, None, "PORT_DOWN", ts)
        else:
            self._page(f"\U0001f50c Port down — {switch}",
                      f"{switch}: monitored port {label} is down (SNMP).",
                      device_id, None, "PORT_DOWN", ts)
        return PortEvent(device_id, p.if_index, "down", label, folded_into)

    def _on_up(self, device_id: int, p: PortStatus, feeds: int | None, ts: str) -> PortEvent:
        switch = self._name(device_id)
        label = _label(p)
        where = f" -> {self._name(feeds)}" if feeds is not None else ""
        self._page(f"✅ Port restored — {switch}",
                  f"{switch}: monitored port {label}{where} is back up (SNMP).",
                  device_id, None, "PORT_RESTORED", ts)
        return PortEvent(device_id, p.if_index, "up", label, feeds)

    # -- bandwidth edges (operator heads-up; NOT folded into an outage — the port
    #    still pings "up", this is a traffic anomaly, not a hard down) --
    def _on_bw_low(self, device_id: int, p: PortStatus, feeds: int | None,
                   in_bps: float | None, out_bps: float | None, threshold: float,
                   direction: str, ts: str) -> PortEvent:
        switch = self._name(device_id)
        label = _label(p)
        where = f" -> {self._name(feeds)}" if feeds is not None else ""
        self._page(f"\U0001f4c9 Low bandwidth — {switch}",
                  f"{switch}: monitored port {label}{where} throughput fell below "
                  f"{threshold:g} Mbps ({direction}). Now in {_fmt_rate(in_bps)} / "
                  f"out {_fmt_rate(out_bps)}.",
                  device_id, None, "PORT_BW_LOW", ts, enabled=self.cfg.snmp_bw_alerts)
        return PortEvent(device_id, p.if_index, "bw_low", label, None)

    def _on_bw_ok(self, device_id: int, p: PortStatus, feeds: int | None,
                  in_bps: float | None, out_bps: float | None, ts: str) -> PortEvent:
        switch = self._name(device_id)
        label = _label(p)
        self._page(f"\U0001f4c8 Bandwidth recovered — {switch}",
                  f"{switch}: monitored port {label} throughput is back above "
                  f"threshold. Now in {_fmt_rate(in_bps)} / out {_fmt_rate(out_bps)}.",
                  device_id, None, "PORT_BW_OK", ts, enabled=self.cfg.snmp_bw_alerts)
        return PortEvent(device_id, p.if_index, "bw_ok", label, None)

    # -- glue --
    def _name(self, device_id: int) -> str:
        dev = self.store.get_org_device(self.org_id, device_id)
        return dev["name"] if dev else f"#{device_id}"

    def _page(self, title: str, body: str, device_id: int, outage_id: int | None,
              payload: str, ts: str, *, enabled: bool | None = None) -> None:
        """Operator-only heads-up, gated by `enabled` (defaults to `cfg.snmp_alerts` for
        the port-status pages; bandwidth pages pass `cfg.snmp_bw_alerts` explicitly —
        state is always written by the caller regardless of either gate). Not part of
        the owner/operator/tech escalation ladder — a port-down either folds into an
        outage that ladder already covers, or stands alone as a one-shot heads-up with
        no ladder of its own; same for a bandwidth edge."""
        gate = self.cfg.snmp_alerts if enabled is None else enabled
        topic = self.store.org_role_topic(self.org_id, "operator")
        if gate and topic:
            res = self.notifier.send(topic, title, body, 3)
            status = "sent" if res.ok else "failed"
        else:
            status = "suppressed"
        self.store.log_alert(self.org_id, outage_id, device_id, self.notifier.channel,
                             topic, status, payload, ts)
