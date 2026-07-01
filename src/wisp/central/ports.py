"""Central-side SNMP port folding (plan.md's "what's next" item 1 — the one piece of
the old single-box tool's soft-signal tier that IS ported to central, unlike perf
baseline/redundancy which need trailing-sample history central doesn't have yet).

Mirrors the old single-box `egress/ports.py` (`PortMonitor`) one-for-one, ported onto
`CentralStore`'s tenant-scoped `switch_ports` table + an org's operator ntfy topic
instead of a fixed per-process `Config`. Three product rules carried over verbatim:

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

Flap suppression is in-row (`down_streak`/`alarm`), restart-safe like the FSM's own
`device_states`: a monitored port needs `cfg.snmp_down_consecutive` consecutive down
walks to alarm, and a down-then-up blip never pages. The operator page is gated by
`cfg.snmp_alerts`; the `switch_ports` row is always written regardless (same
discipline as the old edge's `WISP_SNMP_ALERTS`) — there is no escalation ladder here,
unlike the ICMP outage ladder in `central/dispatch.py`.
"""
from __future__ import annotations

from dataclasses import dataclass

from wisp.config import CONFIG, Config
from wisp.ingress.snmp import PortStatus


@dataclass(frozen=True)
class PortEvent:
    """What one sync produced, for the caller's log (mirrors the old edge's
    daemon-log line)."""
    device_id: int
    if_index: int
    kind: str                  # 'down' | 'up'
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
    )


class CentralPortMonitor:
    """One instance per report; `sync_device` is called once per switch that reported
    ports that cycle (`central/server.py:_report`, after the ICMP cycle has already
    committed — so `store.open_outage_id` reflects THIS cycle's outages, not last
    cycle's). `device_id`/`feeds_device_id` are `org_devices` ids; the caller is
    responsible for checking `device_id` belongs to `tenant_id` (via the tenant's own
    `EngineRegistry` meta) before this ever runs — this class trusts its input."""

    def __init__(self, store, tenant_id: str, notifier, cfg: Config = CONFIG) -> None:
        self.store = store
        self.tenant_id = tenant_id
        self.notifier = notifier
        self.cfg = cfg

    def sync_device(self, device_id: int, raw_ports: list[dict], ts: str) -> list[PortEvent]:
        cfg = self.cfg
        existing = {r["if_index"]: r for r in
                   self.store.list_switch_ports(self.tenant_id, device_id)}
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

            self.store.upsert_switch_port(
                self.tenant_id, device_id, p.if_index, p.if_name, p.if_alias,
                p.admin_status, p.oper_status, p.last_change, streak, alarm, since, ts)

            if alarm != prior_alarm:   # an edge
                ev = (self._on_down(device_id, p, feeds, ts) if alarm
                     else self._on_up(device_id, p, feeds, ts))
                events.append(ev)
        return events

    # -- detection edges --
    def _on_down(self, device_id: int, p: PortStatus, feeds: int | None, ts: str) -> PortEvent:
        switch = self._name(device_id)
        label = _label(p)
        folded_into = None
        if feeds is not None:
            fed_name = self._name(feeds)
            oid = self.store.open_outage_id(self.tenant_id, feeds)
            if oid is not None:
                # FOLD: enrich the existing outage with the physical cause instead of
                # raising a separate alarm. ICMP/the FSM still owns the outage record.
                self.store.stamp_outage_cause(
                    self.tenant_id, oid, f"Port {label} down (SNMP) -> {fed_name}")
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

    # -- glue --
    def _name(self, device_id: int) -> str:
        dev = self.store.get_org_device(self.tenant_id, device_id)
        return dev["name"] if dev else f"#{device_id}"

    def _page(self, title: str, body: str, device_id: int, outage_id: int | None,
              payload: str, ts: str) -> None:
        """Operator-only heads-up, gated by `cfg.snmp_alerts` (state is always written
        by the caller regardless). Not part of the owner/operator/tech escalation
        ladder — a port-down either folds into an outage that ladder already covers,
        or stands alone as a one-shot heads-up with no ladder of its own."""
        topic = self.store.org_role_topic(self.tenant_id, "operator")
        if self.cfg.snmp_alerts and topic:
            res = self.notifier.send(topic, title, body, 3)
            status = "sent" if res.ok else "failed"
        else:
            status = "suppressed"
        self.store.log_alert(self.tenant_id, outage_id, device_id, self.notifier.channel,
                             topic, status, payload, ts)
