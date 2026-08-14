from __future__ import annotations

import logging
from datetime import datetime, timezone

from wisp.central import onuroster
from wisp.central.notify_policy import AlertRouter
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
        self.router = AlertRouter(store, org_id, notifier, cfg)

    def sweep(self, ts: str) -> None:
        rows = self.store.org_onu_rows(self.org_id)
        now = datetime.now(timezone.utc)
        self._sweep_capacity(rows, now, ts)
        self._sweep_dup_mac(rows, now, ts)


    def _limits(self) -> dict[int, int]:
        default = self.cfg.onu_pon_limit
        out: dict[int, int] = {}
        for dev_id, override in self.store.org_device_pon_limits(self.org_id).items():
            out[dev_id] = int(override) if override is not None else default
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


    def _sweep_dup_mac(self, rows: list[dict], now: datetime, ts: str) -> None:
        dups = onuroster.duplicate_macs(rows, now)
        prior = self.store.onu_dup_mac_states(self.org_id)
        current = {d.mac: d for d in dups}
        shadow = {d.mac for d in onuroster.duplicate_macs(rows, now, stale_s=None)}

        for mac, d in current.items():
            was = prior.get(mac)
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
                continue
            was_live = (was["online_members"] or 0) >= 2
            self.store.upsert_onu_dup_mac_state(
                self.org_id, mac, members=0, online_members=0, active=False,
                since=None, ts=ts)
            if was_live:
                self._page(
                    f"✅ Duplicate MAC cleared: {mac}", "",
                    None, ts, "ONU_DUP_MAC", gate=self.cfg.onu_dup_mac_alerts)


    def _name(self, device_id: int) -> str:
        dev = self.store.get_org_device(self.org_id, device_id)
        return dev["name"] if dev else f"#{device_id}"

    def _page(self, title: str, body: str, device_id: int | None, ts: str,
              payload: str, *, gate: bool) -> None:
        self.router.emit(
            payload,
            title=title, body=body, priority=3, ts=ts, device_id=device_id,
            gate=gate)
