"""ONU-roster hygiene alerts — the paging shell around pure onuroster math.

Transition-only, like ports/optics/ponalert: a fresh verdict pages the operator
once, a verdict that stays put on the next walk stays silent, and the page
clears when the condition resolves. Two independent checks share one sweep:

  * per-PON ONU cap  — a PON that has reached its ONU limit pages "at capacity".
  * redundant MAC    — a MAC on ≥ 2 ONU slots pages "duplicate ONU MAC".

Never opens an outage (SNMP-derived facts don't); state rows are written even
when the gates (`cfg.onu_limit_alerts` / `cfg.onu_dup_mac_alerts`) are off so the
dashboard can still render them. Runs off the optics fold in `/report` — the
roster only changes when a walk lands, so that IS the right cadence.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from wisp.central import onuroster
from wisp.config import CONFIG, Config

log = logging.getLogger(__name__)


def _slot(m: dict) -> str:
    onu = m.get("onu_id")
    return (f"{m.get('device_name') or '?'} PON {m.get('pon_port') or '?'}"
            f" ONU {onu if onu is not None else '?'}"
            f" ({m.get('state') or 'unknown'})")


class OnuRosterAlerter:

    def __init__(self, store, org_id: str, notifier, cfg: Config = CONFIG) -> None:
        self.store = store
        self.org_id = org_id
        self.notifier = notifier
        self.cfg = cfg

    def sweep(self, ts: str) -> None:
        rows = self.store.org_onu_rows(self.org_id)
        now = datetime.now(timezone.utc)
        self._sweep_capacity(rows, now, ts)
        self._sweep_dup_mac(rows, now, ts)

    # --- per-PON ONU cap -------------------------------------------------------

    def _limits(self) -> dict[int, int]:
        default = self.cfg.onu_pon_limit
        out: dict[int, int] = {}
        for d in self.store.list_org_devices(self.org_id):
            override = d.get("onu_pon_limit")
            out[d["id"]] = int(override) if override is not None else default
        return out

    def _sweep_capacity(self, rows: list[dict], now: datetime, ts: str) -> None:
        limits = self._limits()
        faults = onuroster.capacity_faults(
            rows, now, lambda dev_id: limits.get(dev_id, self.cfg.onu_pon_limit))
        prior = self.store.pon_capacity_states(self.org_id)
        current = {(f.device_id, f.pon_port): f for f in faults}

        for key, f in current.items():
            was = prior.get(key)
            fresh = not (was and was["active"])
            self.store.upsert_pon_capacity_state(
                self.org_id, key[0], key[1], onus=f.onus, active=True,
                since=(ts if fresh or not was else was["since"]) or ts, ts=ts)
            if fresh:
                self._page(
                    f"\U0001f534 PON at capacity: {f.device_name} PON {f.pon_port}",
                    f"{f.onus}/{f.limit} ONUs registered",
                    f.device_id, ts, "ONU_LIMIT", gate=self.cfg.onu_limit_alerts)

        # Clearing needs a FRESH walk that actually shows the PON below its
        # limit. A stale OLT is skipped by the math, so its faults vanish from
        # `current` — freeze those (skip = no verdict, the ponfault rule) or a
        # slow C-Data agent turns every stall into a page/clear storm.
        fresh_devs = onuroster.fresh_device_ids(rows, now)
        for key, was in prior.items():
            if key in current or not was["active"]:
                continue
            if key[0] not in fresh_devs:
                continue
            self.store.upsert_pon_capacity_state(
                self.org_id, key[0], key[1], onus=0, active=False, since=None, ts=ts)
            name = self._name(key[0])
            self._page(f"✅ PON below capacity: {name} PON {key[1]}",
                       "", key[0], ts, "ONU_LIMIT",
                       gate=self.cfg.onu_limit_alerts)

    # --- redundant MAC ---------------------------------------------------------

    def _sweep_dup_mac(self, rows: list[dict], now: datetime, ts: str) -> None:
        dups = onuroster.duplicate_macs(rows, now)
        prior = self.store.onu_dup_mac_states(self.org_id)
        current = {d.mac: d for d in dups}
        # Staleness-blind view of the same rosters: a MAC absent from `current`
        # but still duplicated here only "cleared" because an OLT's walk went
        # stale. Freeze those — clearing (and re-paging when the walk returns)
        # is exactly the storm this fleet already produced once.
        shadow = {d.mac for d in onuroster.duplicate_macs(rows, now, stale_s=None)}

        for mac, d in current.items():
            was = prior.get(mac)
            # Pages fire only for a LIVE conflict — ≥2 slots online at once
            # (clone/loop). C-Data reg tables keep every slot an ONU ever
            # occupied, so a duplicate with dead members is history, not a
            # fault: state is written (dashboard), the operator's phone stays
            # quiet. Field census 2026-07-14: 178 duplicates, 2 live.
            live = d.online_members >= 2
            was_live = bool(was and was["active"]
                            and (was["online_members"] or 0) >= 2)
            fresh = not (was and was["active"])
            self.store.upsert_onu_dup_mac_state(
                self.org_id, mac, members=len(d.members),
                online_members=d.online_members, active=True,
                since=(ts if fresh or not was else was["since"]) or ts, ts=ts)
            if live and not was_live:
                where = "; ".join(_slot(m) for m in d.members)
                self._page(
                    f"⚠️ Duplicate ONU MAC: {mac}",
                    f"Online on {d.online_members} of {len(d.members)} slots: {where}",
                    d.members[0]["device_id"], ts, "ONU_DUP_MAC",
                    gate=self.cfg.onu_dup_mac_alerts)
            elif was_live and not live:
                self._page(
                    f"✅ Duplicate MAC no longer live: {mac}", "",
                    d.members[0]["device_id"], ts, "ONU_DUP_MAC",
                    gate=self.cfg.onu_dup_mac_alerts)

        for mac, was in prior.items():
            if mac in current or not was["active"]:
                continue
            if mac in shadow:
                continue  # absence explained by a stale walk — no verdict
            was_live = (was["online_members"] or 0) >= 2
            self.store.upsert_onu_dup_mac_state(
                self.org_id, mac, members=0, online_members=0, active=False,
                since=None, ts=ts)
            if was_live:
                self._page(
                    f"✅ Duplicate MAC cleared: {mac}", "",
                    None, ts, "ONU_DUP_MAC", gate=self.cfg.onu_dup_mac_alerts)

    # --- shared plumbing (mirrors ponalert._page) ------------------------------

    def _name(self, device_id: int) -> str:
        dev = self.store.get_org_device(self.org_id, device_id)
        return dev["name"] if dev else f"#{device_id}"

    def _page(self, title: str, body: str, device_id: int | None, ts: str,
              payload: str, *, gate: bool) -> None:
        topic = self.store.org_role_topic(self.org_id, "operator")
        if gate and topic:
            res = self.notifier.send(topic, title, body, 3)
            status = "sent" if res.ok else "failed"
        else:
            status = "suppressed"
        self.store.log_alert(self.org_id, None, device_id, self.notifier.channel,
                             topic, status, payload, ts)
