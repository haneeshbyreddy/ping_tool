from __future__ import annotations

import logging
from datetime import timedelta

from wisp.config import CONFIG, Config
from wisp.core.analytics import _parse

log = logging.getLogger(__name__)

SEV_OK = "ok"
SEV_WARN = "warn"
SEV_CRIT = "crit"

_REF_MAX_AGE_DAYS = 7

def _severity(rx_dbm: float | None, state: str | None,
              warn_dbm: float, crit_dbm: float) -> str:
    if state != "online" or rx_dbm is None:
        return SEV_OK
    if rx_dbm <= crit_dbm:
        return SEV_CRIT
    if rx_dbm <= warn_dbm:
        return SEV_WARN
    return SEV_OK

def _ack_active(ack_until: str | None, ts: str) -> bool:
    if not ack_until:
        return False
    try:
        return _parse(ack_until) > _parse(ts)
    except (ValueError, TypeError):
        return False

def _next_ref(prior_ref: float | None, prior_ref_at: str | None,
              rx_dbm: float | None, ts: str) -> tuple[float | None, str | None]:
    if rx_dbm is None:
        return prior_ref, prior_ref_at
    if prior_ref is None or not prior_ref_at:
        return rx_dbm, ts
    try:
        if _parse(ts) - _parse(prior_ref_at) > timedelta(days=_REF_MAX_AGE_DAYS):
            return rx_dbm, ts
    except (ValueError, TypeError):
        return rx_dbm, ts
    return prior_ref, prior_ref_at

class CentralOpticsMonitor:

    def __init__(self, store, org_id: str, notifier, cfg: Config = CONFIG) -> None:
        self.store = store
        self.org_id = org_id
        self.notifier = notifier
        self.cfg = cfg

    def _thresholds(self, device_id: int) -> tuple[float, float]:
        dev = self.store.get_org_device(self.org_id, device_id) or {}
        warn = dev.get("optical_warn_dbm")
        crit = dev.get("optical_crit_dbm")
        warn = self.cfg.optical_warn_dbm if warn is None else float(warn)
        crit = self.cfg.optical_crit_dbm if crit is None else float(crit)
        return warn, crit

    def sync_device(self, device_id: int, raw_onus: list[dict], ts: str) -> None:
        warn_dbm, crit_dbm = self._thresholds(device_id)
        prior = {r["onu_key"]: r for r in
                 self.store.list_onu_optics(self.org_id, device_id)}

        total = online = warn_count = crit_count = crit_unacked = 0
        for raw in raw_onus:
            onu_key = str(raw.get("onu_key") or "").strip()
            if not onu_key:
                continue
            total += 1
            rx = _to_float(raw.get("rx_dbm"))
            state = str(raw.get("state") or "unknown")
            if state == "online":
                online += 1
            sev = _severity(rx, state, warn_dbm, crit_dbm)
            if sev == SEV_WARN:
                warn_count += 1
            elif sev == SEV_CRIT:
                crit_count += 1

            prev = prior.get(onu_key)
            ref, ref_at = _next_ref(
                prev["rx_ref_dbm"] if prev else None,
                prev["rx_ref_at"] if prev else None, rx, ts)
            ack_until = prev["ack_until"] if prev else None
            if sev == SEV_CRIT and not _ack_active(ack_until, ts):
                crit_unacked += 1

            self.store.upsert_onu_optics(
                self.org_id, device_id, onu_key,
                pon_port=raw.get("pon_port"), onu_id=_to_int(raw.get("onu_id")),
                name=(raw.get("name") or None), serial=(raw.get("serial") or None),
                state=state, rx_dbm=rx, tx_dbm=_to_float(raw.get("tx_dbm")),
                olt_rx_dbm=_to_float(raw.get("olt_rx_dbm")),
                distance_m=_to_int(raw.get("distance_m")),
                rx_ref_dbm=ref, rx_ref_at=ref_at, severity=sev, ts=ts)

        self._update_badge(device_id, total, online, warn_count, crit_count,
                           crit_unacked, ts)

    def _update_badge(self, device_id: int, total: int, online: int, warn_count: int,
                      crit_count: int, crit_unacked: int, ts: str) -> None:
        prior = self.store.get_olt_optics(self.org_id, device_id)
        was_alarm = bool(prior["alarm"]) if prior else False
        alarm = crit_unacked > 0
        since = (ts if (alarm and not was_alarm)
                 else (prior["alarm_since"] if (prior and alarm) else None))
        self.store.upsert_olt_optics(
            self.org_id, device_id, onus_total=total, onus_online=online,
            warn_count=warn_count, crit_count=crit_count, alarm=alarm,
            alarm_since=since, ts=ts)
        if alarm and not was_alarm:
            self._page(device_id,
                       f"\U0001f53b Optical critical — {self._name(device_id)}",
                       f"{self._name(device_id)}: {crit_unacked} ONU(s) below the "
                       f"critical Rx-power floor. Subscribers on those drops are at risk "
                       f"of losing sync — check the ODN / splitters.", ts)
        elif was_alarm and not alarm and crit_count == 0:
            self._page(device_id,
                       f"✅ Optical recovered — {self._name(device_id)}",
                       f"{self._name(device_id)}: no ONUs remain below the critical "
                       f"Rx-power floor.", ts)

    def _name(self, device_id: int) -> str:
        dev = self.store.get_org_device(self.org_id, device_id)
        return dev["name"] if dev else f"#{device_id}"

    def _page(self, device_id: int, title: str, body: str, ts: str) -> None:
        topic = self.store.org_role_topic(self.org_id, "operator")
        if self.cfg.optical_alerts and topic:
            res = self.notifier.send(topic, title, body, 3)
            status = "sent" if res.ok else "failed"
        else:
            status = "suppressed"
        self.store.log_alert(self.org_id, None, device_id, self.notifier.channel,
                             topic, status, "OPTICAL_CRIT", ts)

def _to_float(raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None

def _to_int(raw) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None
