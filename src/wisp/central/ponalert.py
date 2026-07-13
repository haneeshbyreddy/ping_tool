"""PON fault heads-up alerts — the paging shell around pure ponfault math.

Transition-only, like ports/optics: a fresh FIBER verdict pages the operator
once, a verdict that stays put on the next walk stays silent, and the page
clears when the PON comes back. POWER verdicts are recorded but never page —
the whole point of the classification is NOT waking a splicing crew for the
DISCOM (the ICMP ladder still owns any actual device outage).

Never opens an outage (SNMP-derived facts don't); state rows are written even
when `cfg.pon_fault_alerts` is off so the dashboard can still render them.
Runs off the optics fold in `/report` — fault input only changes when a walk
lands, so that IS the right cadence.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from wisp.central import ponfault
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

    def sweep(self, ts: str) -> None:
        rows = self.store.org_onu_rows(self.org_id)
        dists = ponfault.passive_distances(
            self.store.list_org_devices(self.org_id),
            self.store.list_link_routes(self.org_id))
        faults = ponfault.evaluate_org(rows, datetime.now(timezone.utc),
                                       passive_dists=dists)
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
                where = (f"between {_fmt_km(f.cut_low_m)} and {_fmt_km(f.cut_high_m)}"
                         if f.cut_high_m is not None else "at an unknown distance")
                suspect = f" Suspect: {f.suspect}." if f.suspect else ""
                self._page(
                    f"✂️ Suspected fiber cut — {f.device_name} PON {f.pon_port or '?'}",
                    f"{f.dark} of {f.onus_total} ONUs dropped (LOS). Cut likely "
                    f"{where} from the OLT, by ranging (optical path — slack "
                    f"included).{suspect}", f.device_id, ts)

        for key, was in prior.items():
            if key in current or not was["active"]:
                continue
            self.store.upsert_pon_fault_state(
                self.org_id, key[0], key[1], kind=was["kind"], dark=0,
                active=False, since=None, ts=ts)
            if was["kind"] == "fiber":
                name = self._name(key[0])
                self._page(f"✅ PON recovered — {name} PON {key[1]}",
                           f"{name} PON {key[1]}: the mass ONU drop has cleared.",
                           key[0], ts)

    def _name(self, device_id: int) -> str:
        dev = self.store.get_org_device(self.org_id, device_id)
        return dev["name"] if dev else f"#{device_id}"

    def _page(self, title: str, body: str, device_id: int, ts: str) -> None:
        topic = self.store.org_role_topic(self.org_id, "operator")
        if self.cfg.pon_fault_alerts and topic:
            res = self.notifier.send(topic, title, body, 3)
            status = "sent" if res.ok else "failed"
        else:
            status = "suppressed"
        self.store.log_alert(self.org_id, None, device_id, self.notifier.channel,
                             topic, status, "PON_FAULT", ts)
