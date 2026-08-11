from __future__ import annotations

import logging
from datetime import datetime, timezone

from wisp.central import onuroster, ponfault
from wisp.central.notify_policy import AlertRouter
from wisp.config import CONFIG, Config

log = logging.getLogger(__name__)


def _fmt_km(m: int | None) -> str:
    return "?" if m is None else f"{m / 1000:.2f} km"


class PonFaultAlerter:

    def __init__(self, store, org_id: str, notifier, cfg: Config = CONFIG) -> None:
        self.store = store
        self.org_id = org_id
        self.notifier = notifier
        self.cfg = cfg
        self.router = AlertRouter(store, org_id, notifier, cfg)

    def sweep(self, ts: str) -> None:
        rows = self.store.org_onu_rows(self.org_id)
        dists = ponfault.passive_distances(
            self.store.list_org_devices(self.org_id),
            self.store.list_link_routes(self.org_id))
        faults = ponfault.evaluate_org(rows, datetime.now(timezone.utc),
                                       passive_dists=dists,
                                       witness_macs=self.store.onu_place_macs(
                                           self.org_id))
        prior = self.store.pon_fault_states(self.org_id)

        current: dict[tuple[int, str], ponfault.PonFault] = {
            (f.device_id, f.pon_port or "?"): f for f in faults}

        for key, f in current.items():
            was = prior.get(key)
            fresh = not (was and was["active"])
            self.store.upsert_pon_fault_state(
                self.org_id, key[0], key[1], kind=f.kind, dark=f.dark,
                active=True,
                since=(f.since if fresh or not was else was["since"]) or ts, ts=ts)
            if fresh and f.kind == "fiber":
                where = (f"{_fmt_km(f.cut_low_m)} to {_fmt_km(f.cut_high_m)}"
                         if f.cut_high_m is not None else "unknown distance")
                suspect = f" · suspect {f.suspect}" if f.suspect else ""
                if f.evidence == "witness":
                    n = f.witness_dark
                    head = f"✂️ Fiber cut CONFIRMED: {f.device_name} PON {f.pon_port or '?'}"
                    why = (f" · {n} power-backed reference ONU"
                           f"{'' if n == 1 else 's'} dark")
                else:
                    head = f"✂️ Suspected fiber cut: {f.device_name} PON {f.pon_port or '?'}"
                    why = ""
                self._page(
                    head,
                    f"{f.dark}/{f.onus_total} ONUs dark · {where} from OLT"
                    f"{suspect}{why}",
                    f.device_id, ts)

        fresh_devs = onuroster.fresh_device_ids(rows, datetime.now(timezone.utc))
        for key, was in prior.items():
            if key in current or not was["active"]:
                continue
            if key[0] not in fresh_devs:
                continue
            self.store.upsert_pon_fault_state(
                self.org_id, key[0], key[1], kind=was["kind"], dark=0,
                active=False, since=None, ts=ts)
            if was["kind"] == "fiber":
                name = self._name(key[0])
                self._page(f"✅ PON recovered: {name} PON {key[1]}", "", key[0], ts,
                           kind="PON_RECOVERED")

    def _name(self, device_id: int) -> str:
        dev = self.store.get_org_device(self.org_id, device_id)
        return dev["name"] if dev else f"#{device_id}"

    def _page(self, title: str, body: str, device_id: int, ts: str,
              kind: str = "PON_FAULT") -> None:
        self.router.emit(
            kind,
            title=title, body=body, priority=3, ts=ts, device_id=device_id,
            gate=self.cfg.pon_fault_alerts)
